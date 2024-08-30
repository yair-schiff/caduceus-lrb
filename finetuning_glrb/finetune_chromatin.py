import os
from functools import partial
from os import path as osp
import re
import torch
import torch.nn as nn
import numpy as np
from torch.utils.data import DataLoader
import wandb
import pytorch_lightning as pl
from pytorch_lightning.loggers import WandbLogger
from pytorch_lightning.callbacks import EarlyStopping, ModelCheckpoint
from transformers import AutoModelForMaskedLM, AutoModel, AutoTokenizer, AutoConfig
from datasets import load_dataset, load_from_disk
from sklearn.metrics import accuracy_score, precision_recall_curve, auc, roc_auc_score
from transformers import DefaultDataCollator
from src.utils.train import get_logger
from caduceus.tokenization_caduceus import CaduceusTokenizer
from finetuning_glrb.utils import (
    fsspec_exists,
    get_last_embedding_dimension
)

# Logger setup
log = get_logger(__name__)

# Constants for the window size in base pairs
WINDOW_SIZE_BP = 200

def tokenize_variants(examples, tokenizer, max_length: int):
    """
    Tokenize sequence.

    Args:
        examples: A batch of items from the dataset.
        tokenizer: AutoTokenizer instance.
        max_length: Maximum length for tokenization.

    Returns:
        dict with tokenized input IDs.
    """
    seq_tokenized = tokenizer.batch_encode_plus(
        examples["sequence"],
        add_special_tokens=False,
        return_attention_mask=False,
        max_length=max_length,
        truncation=True,
    )
    return {
        "ref_input_ids": seq_tokenized["input_ids"]
    }

def recast_chromosome(examples):
    """
    Recast chromosome to integer format.

    Returns:
        dict with chromosome recast as integers.
    """
    return {
        "chromosome": -1 if examples["chromosome"] in ["X","Y"] else int(examples["chromosome"])
    }

class MLP_ChromatineFeatures(nn.Module):
    """
    MLP model for predicting chromatin features.

    Args:
        input_size: Input size for the linear layer.
        hidden_size: Hidden layer size.
        output_size: Output size for the linear layer.
    """
    def __init__(self, input_size, hidden_size, output_size):
        super(MLP_ChromatineFeatures, self).__init__()
        self.fc1 = nn.Linear(input_size, hidden_size)
        self.sp1 = nn.Softplus()
        self.fc2 = nn.Linear(hidden_size, output_size)

    def forward(self, x):
        return self.fc2(self.sp1(self.fc1(x)))

class DNAModelForChromatineFeatures(nn.Module):
    """
    DNA Model for Chromatin Features prediction.

    Args:
        args: Arguments containing model configurations.
    """
    def __init__(self, args):
        super().__init__()
        self.rcps = args.rcps
        self.bp_per_token = args.bp_per_token
        self.config = AutoConfig.from_pretrained(args.model_name, trust_remote_code=True)

        # Load the appropriate backbone model based on the model name
        if "nucleotide-transformer" in args.model_name.lower():
            self.backbone = AutoModelForMaskedLM.from_pretrained(args.model_name, trust_remote_code=True).esm
        else:
            self.backbone = AutoModel.from_pretrained(args.model_name, trust_remote_code=True)
        
        print(f"MODEL LOADED: {self.backbone}")
        self.inner_dim = get_last_embedding_dimension(self.backbone,self.rcps)
        print(f"Inner dim found for the Foundation Model: {self.inner_dim}")
        self.head = MLP_ChromatineFeatures(input_size=self.inner_dim, hidden_size=2 * self.inner_dim, output_size=20)

    def forward(self, input_ids):
        embeds_out = self.backbone(input_ids)[0]
        num_channels = embeds_out.size(-1)
        window_size = WINDOW_SIZE_BP // self.bp_per_token // 2
        batch_size, seq_len, embedding_dim = embeds_out.shape

        if self.rcps:
            embeds = embeds_out[..., :num_channels // 2]
            expanded_indices = (
                torch.arange(-window_size, window_size + 1, device=embeds.device).unsqueeze(0).expand(batch_size, -1) + seq_len // 2
            )
            expanded_indices = torch.clamp(expanded_indices, 0, embeds.size(1) - 1)

            # Extract windowed embeddings for reference sequence
            tokens_window_ref = torch.gather(embeds, 1, expanded_indices.unsqueeze(-1).expand(-1, -1, embeds.size(2)))

            # Extract windowed embeddings for reverse complement sequence
            rc_embeds = embeds_out[..., num_channels // 2:].contiguous().flip(dims=[1, 2])
            expanded_indices = torch.arange(-window_size, window_size + 1, device=rc_embeds.device).unsqueeze(0).expand(batch_size, -1) + seq_len // 2
            expanded_indices = torch.clamp(expanded_indices, 0, rc_embeds.size(1) - 1)

            tokens_window_rc = torch.gather(rc_embeds, 1, expanded_indices.unsqueeze(-1).expand(-1, -1, rc_embeds.size(2)))

            # Average the embeddings over the sequence length dimension
            tokens_window_rc = tokens_window_rc.mean(dim=1)
            tokens_window_ref = tokens_window_ref.mean(dim=1)

            # Combine the Reference and RC-equivalent embeddings
            aggregated_embeds = tokens_window_rc + tokens_window_ref
            return self.head(aggregated_embeds)

        else:
            expanded_indices = torch.arange(-window_size, window_size + 1, device=embeds_out.device).unsqueeze(0).expand(batch_size, -1) + seq_len // 2
            expanded_indices = torch.clamp(expanded_indices, 0, embeds_out.size(1) - 1)

            # Extract windowed embeddings for reference sequence
            tokens_window_ref = torch.gather(embeds_out, 1, expanded_indices.unsqueeze(-1).expand(-1, -1, embeds_out.size(2))).mean(dim=1)
            return self.head(tokens_window_ref)

class Lit_ChromatinFeatures(pl.LightningModule):
    """
    PyTorch Lightning model for predicting chromatin features.

    Args:
        args: Arguments containing model and training configurations.
    """
    def __init__(self, args):
        super().__init__()
        self.save_hyperparameters(args)
        self.setup()
        
    def setup(self,stage=None):
        self.model = DNAModelForChromatineFeatures(self.hparams)
        self.criterion = nn.BCEWithLogitsLoss()
        self.validation_step_preds = []
        self.validation_step_labels = []
        self.training_step_preds = []
        self.training_step_labels = []

    def forward(self, ref_input_ids):
        return self.model(ref_input_ids)

    def training_step(self, batch, batch_idx):
        ref_input_ids = batch["ref_input_ids"]
        labels = batch["labels"].float()

        logits = self(ref_input_ids)
        loss = self.criterion(logits, labels)
        self.log('train_loss', loss, on_epoch=True, on_step=True, sync_dist=True)

        # Track predictions and labels for accuracy and F1 score
        preds = (torch.sigmoid(logits) > 0.5).float()  # Get predicted class labels
        self.training_step_preds.extend(preds.detach().flatten().cpu().float().numpy())
        self.training_step_labels.extend(labels.detach().flatten().cpu().float().numpy())

        return loss

    def validation_step(self, batch, batch_idx):
        ref_input_ids = batch["ref_input_ids"]
        labels = batch["labels"].float()

        logits = self(ref_input_ids)
        loss = self.criterion(logits, labels)
        self.log('val_loss', loss, on_epoch=True, on_step=True, sync_dist=True)

        # Track predictions and labels for accuracy and F1 score
        preds = (torch.sigmoid(logits) > 0.5).float()  # Get predicted class labels
        self.validation_step_preds.extend(preds.detach().flatten().cpu().float().numpy())
        self.validation_step_labels.extend(labels.detach().flatten().cpu().float().numpy())

        return loss
    
    def test_step(self, batch, batch_idx):
        ref_input_ids = batch["ref_input_ids"]
        labels = batch["labels"].float()

        logits = self(ref_input_ids)

        # Track predictions and labels for accuracy and F1 score
        preds = (torch.sigmoid(logits) > 0.5).float()  # Get predicted class labels
        self.validation_step_preds.extend(preds.detach().flatten().cpu().float().numpy())
        self.validation_step_labels.extend(labels.detach().flatten().cpu().float().numpy())

    def on_validation_epoch_end(self):
        # Calculate accuracy, AUPRC, and AUROC for validation
        val_accuracy = accuracy_score(self.validation_step_labels, self.validation_step_preds)
        precision, recall, _ = precision_recall_curve(self.validation_step_labels, self.validation_step_preds)
        val_auprc = auc(recall, precision)
        val_auroc = roc_auc_score(self.validation_step_labels, self.validation_step_preds)

        # Log validation metrics
        self.log("validation/accuracy", val_accuracy, on_epoch=True, prog_bar=True, logger=True, sync_dist=True)
        self.log("validation/AUPRC", val_auprc, on_epoch=True, prog_bar=True, logger=True, sync_dist=True)
        self.log("validation/AUROC", val_auroc, on_epoch=True, prog_bar=True, logger=True, sync_dist=True)

        self.validation_step_labels.clear()
        self.validation_step_preds.clear()
    
    def on_test_epoch_end(self):
        # Calculate accuracy, AUPRC, and AUROC for validation
        val_accuracy = accuracy_score(self.validation_step_labels, self.validation_step_preds)
        precision, recall, _ = precision_recall_curve(self.validation_step_labels, self.validation_step_preds)
        val_auprc = auc(recall, precision)
        val_auroc = roc_auc_score(self.validation_step_labels, self.validation_step_preds)

        # Log validation metrics
        self.log("test/accuracy", val_accuracy, on_epoch=True, prog_bar=True, logger=True, sync_dist=True)
        self.log("test/AUPRC", val_auprc, on_epoch=True, prog_bar=True, logger=True, sync_dist=True)
        self.log("test/AUROC", val_auroc, on_epoch=True, prog_bar=True, logger=True, sync_dist=True)

        self.validation_step_labels.clear()
        self.validation_step_preds.clear()

    def on_train_epoch_end(self):
        # Calculate accuracy, AUPRC, and AUROC for training
        train_accuracy = accuracy_score(self.training_step_labels, self.training_step_preds)
        precision, recall, _ = precision_recall_curve(self.training_step_labels, self.training_step_preds)
        train_auprc = auc(recall, precision)
        train_auroc = roc_auc_score(self.training_step_labels, self.training_step_preds)

        # Log training metrics
        self.log("train/accuracy", train_accuracy, on_epoch=True, prog_bar=True, logger=True, sync_dist=True)
        self.log("train/AUPRC", train_auprc, on_epoch=True, prog_bar=True, logger=True, sync_dist=True)
        self.log("train/AUROC", train_auroc, on_epoch=True, prog_bar=True, logger=True, sync_dist=True)

        self.training_step_labels.clear()
        self.training_step_preds.clear()

    def configure_optimizers(self):
        return torch.optim.AdamW(self.parameters(), lr=self.hparams.learning_rate)

class HistoneMarksDataModule(pl.LightningDataModule):
    """
    Data module for Histone Marks finetuning with PyTorch Lightning.

    Args:
        config: Configuration dictionary with data-related parameters.
    """
    def __init__(self, config):
        super().__init__()
        self.seq_len = config.seq_len
        self.bp_per_token = config.bp_per_token
        self.model_name = config.model_name
        self.train_batch_size = config.train_batch_size
        self.test_batch_size = config.test_batch_size
        self.num_workers = config.num_workers
        self.train_ratio = config.train_ratio
        self.eval_ratio = config.eval_ratio
        self.cache_dir = "./"
        self.dataset = None

        # Initialize the tokenizer
        if "caduceus" in self.model_name:
            self.tokenizer = CaduceusTokenizer(
                model_max_length=self.seq_len,
                add_special_tokens=False
            )
        else:
            self.tokenizer = AutoTokenizer.from_pretrained(self.model_name, trust_remote_code=True)

    def prepare_data(self):
        # Download and preprocess data if not already done
        if not fsspec_exists(self._get_preprocessed_cache_file()):
            return self._download_and_preprocess_data()
        else:
            return load_dataset(self._get_preprocessed_cache_file())

    def setup(self, stage=None):
        # Load the preprocessed dataset
        self.dataset = self.prepare_data()
        # self.dataset = load_from_disk(self._get_preprocessed_cache_file())

        # Split the dataset into train and validation sets
        self.test_dataset = self.dataset["test"]

    def train_dataloader(self):
        return DataLoader(
            self.train_dataset,
            batch_size=self.train_batch_size,
            collate_fn=DefaultDataCollator(return_tensors="pt"),
            num_workers=self.num_workers,
            pin_memory=True,
            shuffle=True
        )

    def val_dataloader(self):
        return DataLoader(
            self.val_dataset,
            batch_size=self.test_batch_size,
            collate_fn=DefaultDataCollator(return_tensors="pt"),
            num_workers=self.num_workers,
            pin_memory=True,
            shuffle=False
        )
    
    def test_dataloader(self):
        return DataLoader(
            self.test_dataset,
            batch_size=self.test_batch_size,
            collate_fn=DefaultDataCollator(return_tensors="pt"),
            num_workers=self.num_workers,
            pin_memory=True,
            shuffle=False
        )
    
    def _split_dataset(self,selected_validation_chromosome):
        log.warning(f"SPLITTING THE DATASET INTO TRAIN AND VAL SET, VAL SET BEING CHROMOSOME {selected_validation_chromosome}")
        self.train_dataset = self.dataset["train"].filter(
            lambda examples: [c != selected_validation_chromosome for c in examples["chromosome"]],
            # lambda example: example["chromosome"] != selected_validation_chromosome,
            keep_in_memory=True,
            batched=True,
            batch_size=1000,
            num_proc=os.cpu_count()
        )
        self.val_dataset = self.dataset["train"].filter(
            lambda examples: [c == selected_validation_chromosome for c in examples["chromosome"]],
            # lambda example: example["chromosome"] == selected_validation_chromosome,
            keep_in_memory=True,
            batched=True,
            batch_size=1000,
            num_proc=os.cpu_count()
        )
        self.validation_chromosome = selected_validation_chromosome

    def _get_preprocessed_cache_file(self):
        self.cache_dir = osp.join(
            "./", "data", "InstaDeepAI___genomics-long-range-benchmark",
            "chromatin_features_histone_marks", f"seqlen{self.seq_len}"
        )
        cache_file = os.path.join(self.cache_dir, "caduceus_char_token_preprocessed")
        return re.sub(r"=", "_", cache_file)

    def _download_and_preprocess_data(self):
        log.warning("Downloading and preprocessing data...")
        dataset = load_dataset(
            "InstaDeepAI/genomics-long-range-benchmark",
            task_name="chromatin_features_histone_marks",
            sequence_length=self.seq_len,
            subset=True,
            load_from_cache=False,
            trust_remote_code=True,
            num_proc=os.cpu_count()
        )
        try:
            del dataset["validation"]  # Remove empty validation split if it exists
        except KeyError:
            pass

        # Process data: filter sequences with too many 'N's, recast chromosomes, and tokenize
        dataset = dataset.filter(
            lambda example: example["sequence"].count('N') < 0.005 * self.seq_len,
            desc="Filter N's",
        )
        dataset = dataset.map(
            recast_chromosome,
            remove_columns=["chromosome"],
            desc="Recast chromosome",
        )
        dataset = dataset.map(
            partial(tokenize_variants, tokenizer=self.tokenizer, max_length=self.seq_len//self.bp_per_token),
            batch_size=1000,
            batched=True,
            remove_columns=["sequence"],
            desc="Tokenize",
            num_proc=os.cpu_count()
        )

        # Save processed dataset to disk
        return dataset
        # dataset.save_to_disk(self._get_preprocessed_cache_file())
        log.warning("Data downloaded and preprocessed successfully.")

class DNAAccessibilityDataModule(pl.LightningDataModule):
    """
    Data module for DNA Accessibility finetuning with PyTorch Lightning.

    Args:
        config: Configuration dictionary with data-related parameters.
    """
    def __init__(self, config):
        super().__init__()
        self.seq_len = config.seq_len
        self.bp_per_token = config.bp_per_token
        self.model_name = config.model_name
        self.train_batch_size = config.train_batch_size
        self.test_batch_size = config.test_batch_size
        self.num_workers = config.num_workers
        self.train_ratio = config.train_ratio
        self.eval_ratio = config.eval_ratio
        self.cache_dir = "./"
        self.dataset = None

        # Initialize the tokenizer
        if "caduceus" in self.model_name:
            self.tokenizer = CaduceusTokenizer(
                model_max_length=self.seq_len,
                add_special_tokens=False
            )
        else:
            self.tokenizer = AutoTokenizer.from_pretrained(self.model_name, trust_remote_code=True)

    def prepare_data(self):
        # Download and preprocess data if not already done
        if not fsspec_exists(self._get_preprocessed_cache_file()):
            return self._download_and_preprocess_data()
        else:
            return load_dataset(self._get_preprocessed_cache_file())

    def setup(self, stage=None):
        # Load the preprocessed dataset
        self.dataset = self.prepare_data()
        # self.dataset = load_from_disk(self._get_preprocessed_cache_file())

        # Split the dataset into train and validation sets
        self.test_dataset = self.dataset["test"]


    def train_dataloader(self):
        return DataLoader(
            self.train_dataset,
            batch_size=self.train_batch_size,
            collate_fn=DefaultDataCollator(return_tensors="pt"),
            num_workers=self.num_workers,
            pin_memory=True,
            shuffle=True
        )

    def val_dataloader(self):
        return DataLoader(
            self.val_dataset,
            batch_size=self.test_batch_size,
            collate_fn=DefaultDataCollator(return_tensors="pt"),
            num_workers=self.num_workers,
            pin_memory=True,
            shuffle=False
        )
    
    def test_dataloader(self):
        return DataLoader(
            self.test_dataset,
            batch_size=self.test_batch_size,
            collate_fn=DefaultDataCollator(return_tensors="pt"),
            num_workers=self.num_workers,
            pin_memory=True,
            shuffle=False
        )
    
    def _split_dataset(self,selected_validation_chromosome):
        log.warning(f"SPLITTING THE DATASET INTO TRAIN AND VAL SET, VAL SET BEING CHROMOSOME {selected_validation_chromosome}")
        self.train_dataset = self.dataset["train"].filter(
            lambda examples: [c != selected_validation_chromosome for c in examples["chromosome"]],
            # lambda example: example["chromosome"] != selected_validation_chromosome,
            keep_in_memory=True,
            batched=True,
            batch_size=1000,
            num_proc=os.cpu_count()
        )
        self.val_dataset = self.dataset["train"].filter(
            lambda examples: [c == selected_validation_chromosome for c in examples["chromosome"]],
            # lambda example: example["chromosome"] == selected_validation_chromosome,
            keep_in_memory=True,
            batched=True,
            batch_size=1000,
            num_proc=os.cpu_count()
        )
        self.validation_chromosome = selected_validation_chromosome

    def _get_preprocessed_cache_file(self):
        self.cache_dir = osp.join(
            "./", "data", "InstaDeepAI___genomics-long-range-benchmark",
            "chromatin_features_dna_accessibility", f"seqlen{self.seq_len}"
        )
        cache_file = os.path.join(self.cache_dir, "caduceus_char_token_preprocessed")
        return re.sub(r"=", "_", cache_file)

    def _download_and_preprocess_data(self):
        log.warning("Downloading and preprocessing data...")
        dataset = load_dataset(
            "InstaDeepAI/genomics-long-range-benchmark",
            task_name="chromatin_features_dna_accessibility",
            sequence_length=self.seq_len,
            subset=True,
            load_from_cache=False,
            trust_remote_code=True,
            num_proc=os.cpu_count()
        )
        try:
            del dataset["validation"]  # Remove empty validation split if it exists
        except KeyError:
            pass

        # Process data: filter sequences with too many 'N's, recast chromosomes, and tokenize
        dataset = dataset.filter(
            lambda example: example["sequence"].count('N') < 0.005 * self.seq_len,
            desc="Filter N's",
        )
        dataset = dataset.map(
            recast_chromosome,
            remove_columns=["chromosome"],
            desc="Recast chromosome",
        )
        dataset = dataset.map(
            partial(tokenize_variants, tokenizer=self.tokenizer, max_length=self.seq_len//self.bp_per_token),
            batch_size=1000,
            batched=True,
            remove_columns=["sequence"],
            desc="Tokenize",
            num_proc=os.cpu_count()
        )

        # Save processed dataset to disk
        return dataset
        # dataset.save_to_disk(self._get_preprocessed_cache_file())
        log.warning("Data downloaded and preprocessed successfully.")


def finetune_histone_marks(args):
    """
    Main function to start finetuning on Histone Marks with PyTorch Lightning.

    Args:
        args: Command line arguments or configuration dictionary.
    """
    wandb.login(key=args.wandb_api_key)
    data_module = HistoneMarksDataModule(args)
    data_module.setup()

    np.random.seed(0)
    candidates = np.unique(data_module.dataset["train"]["chromosome"])
    held_chromosomes = np.random.choice(candidates,5,replace = False)

    for idx,val_chromosome in enumerate(held_chromosomes):
        wandb_logger = WandbLogger(
        name=f"{args.name_wb}-{args.seq_len}-fold-{idx+1}",
        project="Histone Marks",
        log_model=True  # Automatically log model checkpoints
    )
        data_module._split_dataset(val_chromosome)

        model = Lit_ChromatinFeatures(args)


        checkpoint_callback = ModelCheckpoint(
            dirpath=f"{args.save_dir}/checkpoints",
            filename=f"best-checkpoint-on-chromosome{val_chromosome}",
            save_top_k=1,
            verbose=True,
            monitor="val_loss",
            mode="min"
        )

        nb_device = "1" if "nucleotide-transformer" in args.model_name.lower() else "auto"

        # Set up the PyTorch Lightning Trainer
        trainer = pl.Trainer(
            max_epochs=args.num_epochs,
            devices=args.num_devices,
            accelerator="cuda",
            logger=wandb_logger,
            callbacks=[checkpoint_callback],
            log_every_n_steps=1,
            limit_train_batches=args.train_ratio,
            limit_val_batches=args.eval_ratio,
            val_check_interval=args.log_interval,
            gradient_clip_val=1.0,
            precision="bf16",
            accumulate_grad_batches=args.accumulate_grad_batches,
            num_sanity_val_steps=0
        )

        # Start the training process
        trainer.fit(model, data_module)
        trainer.test(model,data_module,ckpt_path=f"./{args.save_dir}/checkpoints/best-checkpoint-on-chromosome{val_chromosome}.ckpt")

        # Finish the current WandB run
        wandb.finish()

def finetune_dna_accessibility(args):
    """
    Main function to start finetunning on DNA Accessibility with PyTorch Lightning.

    Args:
        args: Command line arguments or configuration dictionary.
    """
    wandb.login(key=args.wandb_api_key)
    data_module = DNAAccessibilityDataModule(args)
    data_module.setup()
    np.random.seed(0)
    candidates = np.unique(data_module.dataset["train"]["chromosome"])
    held_chromosomes = np.random.choice(candidates,5,replace = False)

    for idx,val_chromosome in enumerate(held_chromosomes):

        wandb_logger = WandbLogger(
            name=f"{args.name_wb}-{args.seq_len}-fold-{idx+1}",
            project="DNA Accessibility",
            log_model=True  # Automatically log model checkpoints
        )

        data_module._split_dataset(val_chromosome)


        model = Lit_ChromatinFeatures(args)

        checkpoint_callback = ModelCheckpoint(
            dirpath=f"{args.save_dir}/checkpoints",
            filename=f"best-checkpoint-on-chromosome{val_chromosome}",
            save_top_k=1,
            verbose=True,
            monitor="val_loss",
            mode="min"
        )

        nb_device = "1" if "nucleotide-transformer" in args.model_name.lower() else "auto"

        # Set up the PyTorch Lightning Trainer
        trainer = pl.Trainer(
            max_epochs=args.num_epochs,
            devices=args.num_devices,
            accelerator="cuda",
            logger=wandb_logger,
            callbacks=[checkpoint_callback],
            log_every_n_steps=1,
            limit_train_batches=args.train_ratio,
            limit_val_batches=args.eval_ratio,
            val_check_interval=args.log_interval,
            gradient_clip_val=1.0,
            precision="bf16",
            accumulate_grad_batches=args.accumulate_grad_batches,
            num_sanity_val_steps=0
        )

        # Start the training process
        trainer.fit(model, data_module)
        trainer.test(model,data_module,ckpt_path=f"./{args.save_dir}/checkpoints/best-checkpoint-on-chromosome{val_chromosome}.ckpt")

        # Finish the current WandB run
        wandb.finish()
