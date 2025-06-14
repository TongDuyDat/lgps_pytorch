import importlib
import os
import sys
import torch
import torch.nn as nn
import torch.optim as optim
from tqdm import tqdm
from torch.utils.data import DataLoader
from benmark import benchmark_model
from data import DataBenchmark
from loss_functions.loss import CombinedLoss
from loss_functions.metrics import SegmentationMetrics
from models import GanModel, Generator, DiscriminatorWithConvCRF
from models.e_lra import DiscriminatorWithLRA
from utils import check_loss_nan
import logging
import csv
from datetime import datetime

import warnings

warnings.filterwarnings("ignore", message="No handlers found:.*Skipped")


class GANTrainer:
    def __init__(
        self, model, data_train, data_val, batch_size=None, config_path=None, names=None
    ):
        self.load_config(config_path)
        if batch_size is not None:
            self.config.batch_size = batch_size
        self.model = model
        self.data_train = data_train
        self.data_val = data_val
        self.names = names
        self._setup_training()
        self._setup_logging()

    def _setup_logging(self):
        """Setup logging and CSV writer for metrics"""
        # Create log directory
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if self.names is not None:
            timestamp = self.names + "_" + timestamp
        self.log_dir = os.path.join(self.config.checkpoint_dir, f"logs_{timestamp}")
        os.makedirs(self.log_dir, exist_ok=True)

        # Setup logger
        self.logger = logging.getLogger("GANTrainer")
        self.logger.setLevel(logging.INFO)
        log_file = os.path.join(self.log_dir, "training.log")
        file_handler = logging.FileHandler(log_file)
        stream_handler = logging.StreamHandler()
        formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        file_handler.setFormatter(formatter)
        stream_handler.setFormatter(formatter)
        self.logger.addHandler(file_handler)
        self.logger.addHandler(stream_handler)

        # Setup CSV file for metrics
        self.csv_file = os.path.join(self.log_dir, "metrics.csv")
        self.csv_fields = [
            "epoch",
            "train_g_loss",
            "train_d_loss",
            "val_loss",
            "mean_iou",
            "recall",
            "precision",
            "accuracy",
            "dice",
            "f2",
            "train_mean_iou",
            "train_precision",
            "train_recall",
            "train_f2",
            "train_accuracy",
            "train_dice",
        ]
        with open(self.csv_file, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=self.csv_fields)
            writer.writeheader()

    def train(self):
        """Main training loop"""
        self.model.to(self.config.device)
        self.logger.info("Starting training...")
        for epoch in range(self.config.num_epochs):
            train_g_loss, train_d_loss, train_log = self.train_one_epoch()
            val_loss, logs = self.validate()

            # Log to console and file
            log_message = (
                f"Epoch [{epoch + 1}/{self.config.num_epochs}] - "
                f"Train G Loss: {train_g_loss:.4f}, Train D Loss: {train_d_loss:.4f}, "
                f"Val Loss: {val_loss:.4f}, "
                f"Mean IoU: {logs['mean_iou']:.4f}, Dice: {logs['dice']:.4f}, "
                f"Recall: {logs['recall']:.4f}, Precision: {logs['precision']:.4f}, "
                f"Accuracy: {logs['accuracy']:.4f}"
                f"F2: {logs['f2']:.4f}"
            )
            self.logger.info(log_message)

            # Save metrics to CSV
            self._log_to_csv(
                epoch + 1, train_g_loss, train_d_loss, val_loss, logs, train_log
            )

            # Save best model
            is_best = False
            if val_loss < self.best_val_loss:
                self.best_val_loss = val_loss
                is_best = True
            self.save_model(proj_name=self.log_dir, is_best=is_best)
            # Learning rate scheduling
            self.scheduler_G.step()
            self.scheduler_D.step()

    def _log_to_csv(
        self, epoch, train_g_loss, train_d_loss, val_loss, logs, train_logs
    ):
        """Save metrics to CSV file"""
        metrics = {
            "epoch": epoch,
            "train_g_loss": train_g_loss,
            "train_d_loss": train_d_loss,
            "val_loss": val_loss,
            # Added training metrics
            "train_mean_iou": train_logs["mean_iou"],
            "train_recall": train_logs["recall"],
            "train_precision": train_logs["precision"],
            "train_accuracy": train_logs["accuracy"],
            "train_dice": train_logs["dice"],
            "train_f2": train_logs["f2"],
            # Added val_metrics
            "val_loss": val_loss,
            "mean_iou": logs["mean_iou"],
            "recall": logs["recall"],
            "precision": logs["precision"],
            "accuracy": logs["accuracy"],
            "dice": logs["dice"],
            "f2": logs["f2"],
        }
        with open(self.csv_file, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=self.csv_fields)
            writer.writerow(metrics)

    def train_one_epoch(self):
        """Training logic for one epoch"""
        self.model.train()
        total_g_loss = 0
        total_d_loss = 0
        metrics = {
            "mean_iou": 0,
            "recall": 0,
            "precision": 0,
            "accuracy": 0,
            "dice": 0,
            "f2": 0,
        }
        with tqdm(self.train_loader, unit="batch") as pbar:
            for data, targets in pbar:
                data = data.to(self.config.device).to(torch.float32)
                targets = targets.to(self.config.device).to(torch.float32)
                # Train generator
                g_loss, fake_mask = self.train_generator(data, targets)
                if check_loss_nan(g_loss):
                    self.logger.error(
                        "NaN detected in generator loss. Stopping training."
                    )
                    raise ValueError("NaN in generator loss")
                # Train discriminator
                d_loss = self.train_discriminator(data, fake_mask, targets)
                if check_loss_nan(d_loss):
                    self.logger.error(
                        "NaN detected in discriminator loss. Stopping training."
                    )
                    raise ValueError("NaN in discriminator loss")
                metric = self.metrics.update(fake_mask, targets)
                metric = self.metrics.compute()
                self.metrics.reset()
                for key in metrics:
                    if key in metric:
                        metrics[key] += metric[key]
                pbar.update(1)
                logs = {
                    "mean_iou": metrics["mean_iou"] / pbar.n,
                    "recall": metrics["recall"] / pbar.n,
                    "precision": metrics["precision"] / pbar.n,
                    "accuracy": metrics["accuracy"] / pbar.n,
                    "dice": metrics["dice"] / pbar.n,
                    "f2": metrics["f2"] / pbar.n,
                }
                total_d_loss += d_loss
                total_g_loss += g_loss
                pbar.set_postfix(g_loss=g_loss, d_loss=d_loss, **logs)

        avg_g_loss = total_g_loss / len(self.train_loader)
        avg_d_loss = total_d_loss / len(self.train_loader)
        return avg_g_loss, avg_d_loss, logs

    def train_discriminator(self, data, mask_fakes, targets):
        """Train discriminator one step"""
        self.optimizer_D.zero_grad()
        mask_fakes = mask_fakes.detach()
        real_output = self.model.discriminator(data, targets)
        fake_output = self.model.discriminator(data, mask_fakes)
        real_labels = torch.ones_like(real_output) * 0.9
        fake_labels = torch.zeros_like(fake_output)
        d_real_loss = self.discriminator_loss(real_output, real_labels)
        if check_loss_nan(d_real_loss):
            self.logger.error("NaN detected in d_real_loss")
            raise ValueError("NaN in d_real_loss")
        d_fake_loss = self.discriminator_loss(fake_output, fake_labels)
        if check_loss_nan(d_fake_loss):
            self.logger.error("NaN detected in d_fake_loss")
            raise ValueError("NaN in d_fake_loss")
        d_loss = d_real_loss + d_fake_loss
        d_loss.backward()
        self.optimizer_D.step()
        return d_loss.item()

    def train_generator(self, data, targets):
        """Train generator one step"""
        self.optimizer_G.zero_grad()
        fake_masks = self.model.generate(data)
        g_seg_loss = self.generator_loss(fake_masks, targets)
        if check_loss_nan(g_seg_loss):
            self.logger.error("NaN detected in g_seg_loss")
            raise ValueError("NaN in g_seg_loss")
        g_seg_loss.backward()
        self.optimizer_G.step()
        return g_seg_loss.item(), fake_masks

    @torch.no_grad()
    def validate(self):
        """Validation loop"""
        self.model.eval()
        total_val_loss = 0
        metrics = {
            "mean_iou": 0,
            "recall": 0,
            "precision": 0,
            "accuracy": 0,
            "dice": 0,
            "f2": 0,
        }
        with tqdm(self.val_loader, desc="Validating", leave=False) as pbar:
            for data, targets in self.val_loader:
                data, targets = data.to(self.config.device), targets.to(
                    self.config.device
                )
                data, targets = data.to(torch.float32), targets.to(torch.float32)
                outputs = self.model.generate(data)
                combined_loss = self.generator_loss(outputs, targets)
                if check_loss_nan(combined_loss):
                    self.logger.error("NaN detected in val_loss")
                    raise ValueError("NaN in val_loss")
                total_val_loss += combined_loss.item()
                metric = self.metrics.update(outputs, targets)
                metric = self.metrics.compute()
                self.metrics.reset()
                for key in metrics:
                    if key in metric:
                        metrics[key] += metric[key]
                pbar.update(1)
                logs = {
                    "val_loss": combined_loss.item() / pbar.n,
                    "mean_iou": metrics["mean_iou"] / pbar.n,
                    "recall": metrics["recall"] / pbar.n,
                    "precision": metrics["precision"] / pbar.n,
                    "accuracy": metrics["accuracy"] / pbar.n,
                    "dice": metrics["dice"] / pbar.n,
                    "f2": metrics["f2"] / pbar.n,
                }
                pbar.set_postfix(logs)
        avg_val_loss = total_val_loss / len(self.val_loader)
        logs = {key: metrics[key] / len(self.val_loader) for key in metrics}
        logs["val_loss"] = avg_val_loss
        return avg_val_loss, logs

    def _setup_training(self):
        """Setup optimizers, schedulers, and loss functions"""
        self.best_val_loss = float("inf")
        self.optimizer_G = optim.Adam(
            self.model.generator.parameters(),
            lr=self.config.lr_generator,
            betas=(self.config.beta1, self.config.beta2),
        )
        self.optimizer_D = optim.Adam(
            self.model.discriminator.parameters(),
            lr=self.config.lr_discriminator,
            betas=(self.config.beta1, self.config.beta2),
        )
        self.scheduler_G = optim.lr_scheduler.StepLR(
            self.optimizer_G,
            step_size=self.config.lr_decay_step,
            gamma=self.config.lr_decay_gamma,
        )
        self.scheduler_D = optim.lr_scheduler.StepLR(
            self.optimizer_D,
            step_size=self.config.lr_decay_step,
            gamma=self.config.lr_decay_gamma,
        )
        self.generator_loss = CombinedLoss()
        self.discriminator_loss = nn.BCELoss()
        self.metrics = SegmentationMetrics(
            num_classes=2, device="cuda", iou_foreground_only=True
        )
        self.train_loader = DataLoader(
            self.data_train, batch_size=self.config.batch_size, shuffle=True
        )
        self.val_loader = DataLoader(
            self.data_val, batch_size=self.config.batch_size, shuffle=False
        )

    def load_config(self, config_path):
        module_name = config_path.split("/")[-1].replace(".py", "")
        spec = importlib.util.spec_from_file_location(module_name, config_path)
        if spec is None:
            raise ImportError(f"Không thể tải file từ {config_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        GANTrainingConfig = getattr(module, "GANTrainingConfig")
        self.config = GANTrainingConfig()

    def save_model(self, proj_name, is_best=False):
        weights_dir = self.config.checkpoint_dir
        if self.log_dir is not None:
            weights_dir = os.path.join(self.log_dir, "weights")
        self.weights_dir = weights_dir
        os.makedirs(weights_dir, exist_ok=True)
        last_model_path = f"{weights_dir}/last_model.pth"
        self.model.save_checkpoint(last_model_path)
        self.logger.info(f"Saved model checkpoint at {last_model_path}")
        if is_best:
            best_model_path = f"{weights_dir}/best_gan_model.pth"
            self.model.save_best_checkpoint(best_model_path)
            self.logger.info(f"Saved best model checkpoint at {best_model_path}")


if __name__ == "__main__":
    # import argparse

    # device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    # parser = argparse.ArgumentParser(
    #     description="Train GAN model for image segmentation"
    # )
    # parser.add_argument("--data", default="data/configs/CVC-ClinicDB.py")
    # parser.add_argument("--config", default="configs/train_config.py")
    # parser.add_argument("--batch-size", type=int, default=16)
    # args = parser.parse_args()

    # dataset_config_path = args.data
    # trainer_config_path = args.config
    # batch_size = args.batch_size

    # model = GanModel(
    #     generator=Generator(input_shape=(3, 256, 256)),
    #     discriminator=DiscriminatorWithLRA(4),
    #     model_name="GAN",
    #     version="DiscriminatorWithLRA",
    #     description="GAN for image segmentation with LRA",
    # )

    # datasets = DataBenchmark(config_path=dataset_config_path, phase="train")
    # print(len(datasets))
    # train_dataset, val_dataset = datasets.split_data(train_ratio=0.8, seed=42)
    # print(len(train_dataset), len(val_dataset))
    # trainer = GANTrainer(
    #     model=model,
    #     data_train=train_dataset,
    #     data_val=val_dataset,
    #     batch_size=batch_size,
    #     config_path=trainer_config_path,
    # )
    # trainer.train()
    # benchmark_model(
    #    f'{trainer.weights_dir}/best_gan_model.pth',
    #    dataset_configs=[{"config_path": dataset_config_path, "name": "Dataset"}],
    #    phase="val",
    #    batch_size=batch_size,
    #    model_class=model,
    #    threshold=0.5,
    #    verbose=True,
    #    output_csv=f'{trainer.weights_dir}/benchmark_results.csv',
    #    plot_output=f'{trainer.weights_dir}/benchmark_plot.png'
    # )
    #+++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++#
    #++++++++++++++Train GAN model with 5-fold cross-validation for each dataset++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++#
    #+++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++#
    # import argparse
    # import torch
    # from sklearn.model_selection import KFold
    # from torch.utils.data import Subset

    # device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    # parser = argparse.ArgumentParser(
    #     description="Train GAN model with 5-fold cross-validation for each dataset"
    # )
    # parser.add_argument(
    #     "--data",
    #     nargs="+",
    #     default=["data/configs/CVC-ClinicDB.py", "data/configs/kvasir-seg.py"],
    #     help="List of dataset config paths (e.g., CVC-ClinicDB, Kvasir)",
    # )
    # parser.add_argument("--config", default="configs/train_config.py")
    # parser.add_argument("--batch-size", type=int, default=16)
    # args = parser.parse_args()

    # trainer_config_path = args.config
    # batch_size = args.batch_size

    # # Define dataset configurations
    # dataset_configs = [
    #     {"config_path": path, "name": path.split("/")[-1].split(".")[0]}
    #     for path in args.data
    # ]

    # for dataset_config in dataset_configs:
    #     dataset_name = dataset_config["name"]
    #     print(f"\n=== Processing dataset: {dataset_name} ===")

    #     # Load the dataset
    #     datasets = DataBenchmark(
    #         config_path=dataset_config["config_path"], phase="train"
    #     )
    #     print(f"Dataset {dataset_name} size: {len(datasets)}")
    #     # Initialize 5-fold cross-validation
    #     kfold = KFold(n_splits=5, shuffle=True, random_state=42)
    #     fold_idx = 1

    #     for train_indices, test_indices in kfold.split(datasets):
    #         print(f"\n=== Training fold {fold_idx}/5 for dataset: {dataset_name} ===")
    #         # Create train and validation subsets for the current fold
    #         train_datasets = datasets.subset(train_indices, phase="train")
    #         train_dataset, val_dataset = train_datasets.split_data(
    #             train_ratio=0.8, seed=42
    #         )
    #         test_dataset = datasets.subset(test_indices, phase="val")
    #         print(
    #             f"Fold {fold_idx} - Train split: {len(train_dataset)}, Val split: {len(val_dataset)}, Test split: {len(test_dataset)}"
    #         )

    #         # Initialize a new GAN model for each fold
    #         model = GanModel(
    #             generator=Generator(input_shape=(3, 256, 256)),
    #             discriminator=DiscriminatorWithLRA(4),
    #             model_name="GAN",
    #             version="DiscriminatorWithLRA",
    #             description=f"GAN for image segmentation with LRA on {dataset_name} fold {fold_idx}",
    #         )

    #         # Initialize and train the GAN trainer
    #         trainer = GANTrainer(
    #             model=model,
    #             data_train=train_dataset,
    #             data_val=val_dataset,
    #             batch_size=batch_size,
    #             config_path=trainer_config_path,
    #             names=f"{dataset_name}_fold{fold_idx}",
    #         )
    #         trainer.train()

    #         # Benchmark the model for the current fold
    #         benchmark_model(
    #             f"{trainer.weights_dir}/best_gan_model.pth",
    #             dataset_configs=[
    #                 {
    #                     "config_path": dataset_config["config_path"],
    #                     "name": f"{dataset_name}_fold{fold_idx}",
    #                 }
    #             ],
    #             phase="val",
    #             batch_size=batch_size,
    #             model_class=model,
    #             datasets=[test_dataset],
    #             threshold=0.5,
    #             verbose=True,
    #             output_csv=f"{trainer.weights_dir}/benchmark_results_{dataset_name}_fold{fold_idx}.csv",
    #             plot_output=f"{trainer.weights_dir}/benchmark_plot_{dataset_name}_fold{fold_idx}.png",
    #         )
    #         print(f"=== Completed fold {fold_idx}/5 for dataset: {dataset_name} ===")
    #         fold_idx += 1

    #     print(
    #         f"=== Completed 5-fold cross-validation for dataset: {dataset_name} ===\n"
    #     )
    #+++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++#
    #++++++++++++++Train GAN model with protocol 3++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++#
    #+++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++#
    import argparse
    import torch
    import os

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    parser = argparse.ArgumentParser(
        description="Train GAN model with cross-dataset evaluation using dataset methods"
    )
    parser.add_argument(
        "--train-data",
        nargs="+",
        default=["data/configs/kvasir-seg.py", "data/configs/CVC-ClinicDB.py"],
        help="List of dataset config paths for training (e.g., Kvasir, CVC-ClinicDB)"
    )
    parser.add_argument(
        "--test-data",
        default="data/configs/ETIS-Larib.py",
        help="Dataset config path for testing (e.g., ETIS-Larib)"
    )
    parser.add_argument("--config", default="configs/train_config.py")
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--names", type=str, default=None)
    args = parser.parse_args()

    trainer_config_path = args.config
    batch_size = args.batch_size
    names = args.names
    # Load training datasets
    train_datasets = []
    for config_path in args.train_data:
        dataset = DataBenchmark(config_path=config_path, phase="train")
        train_datasets.append(dataset)
    print(f"Loaded training datasets: {[d.config_path.split('/')[-1] for d in train_datasets]}")

    # Merge training datasets
    combined_train_dataset = train_datasets[0]
    for dataset in train_datasets[1:]:
        combined_train_dataset = combined_train_dataset.merge_data(dataset)
    print(f"Combined training dataset size: {len(combined_train_dataset)}")

    # Split combined training dataset into train (80%) and val (20%)
    train_dataset, val_dataset = combined_train_dataset.split_data(train_ratio= 0.8, seed = 42)
    print(f"Train split: {len(train_dataset)}, Val split: {len(val_dataset)}")

    # Load test dataset
    test_dataset = DataBenchmark(config_path=args.test_data, phase="val")
    print(f"Loaded test dataset: {args.test_data.split('/')[-1]}, size: {len(test_dataset)}")

    # Initialize a new GAN model
    model = GanModel(
        generator=Generator(input_shape=(3, 256, 256)),
        discriminator=DiscriminatorWithLRA(4),
        model_name="GAN",
        version="DiscriminatorWithLRA",
        description=f"GAN for cross-dataset segmentation on {','.join(args.train_data)}"
    )

    # Initialize and train the GAN trainer
    trainer = GANTrainer(
        model=model,
        data_train=train_dataset,
        data_val=val_dataset,
        batch_size=batch_size,
        config_path=trainer_config_path,
        names=names
    )
    trainer.train()

    # Benchmark the model on the test set
    benchmark_model(
        f'{trainer.weights_dir}/best_gan_model.pth',
        dataset_configs=[{"config_path": args.test_data, "name": args.test_data.split('/')[-1].split('.')[0]}],
        datasets=[test_dataset],  # Use test_dataset directly
        phase="val",
        batch_size=batch_size,
        model_class=model,
        threshold=0.5,
        verbose=True,
        output_csv=f'{trainer.weights_dir}/benchmark_results_{names}.csv',
        plot_output=f'{trainer.weights_dir}/benchmark_plot_{names}.png'
    )

    print(f"=== Completed cross-dataset training and evaluation ===")