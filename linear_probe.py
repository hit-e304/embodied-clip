import os
import pickle

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader
import pytorch_lightning as pl
import torchmetrics.functional as MF

from constants import target_objects, max_forward_steps


class THOREmbeddingsDataset(Dataset):
    # embedding_type: 'rn50_imagenet_conv', 'rn50_imagenet_avgpool', 'clip_conv', 'clip_attnpool', 'clip_avgpool'
    # prediction_type: 'object_presence', 'object_presence_grid', 'valid_moves_forward', 'pickupable_objects'
    def __init__(self, data_dir, split, embedding_type, prediction_type):
        if prediction_type == 'pickupable_objects':
            image_features = torch.load(os.path.join(data_dir, f"image_features.pt"))
            data = pickle.load(open(os.path.join(data_dir, f"{split}.pkl"), 'rb'))
            self.embeddings = []
            self.predictions = []

            for image, obj, pickupable in data:
                self.embeddings.append(image_features[image][embedding_type])
                self.predictions.append((
                    obj,
                    torch.tensor(pickupable, dtype=int)
                ))
        else:
            data = torch.load(os.path.join(data_dir, f"{split}.pt"))

            if prediction_type == 'valid_moves_forward_cls':
                prediction_type = 'valid_moves_forward'

            self.embeddings = []
            self.predictions = []
            for scene_name, frames in data.items():
                for frame_features in frames:
                    self.embeddings.append(frame_features[embedding_type])
                    self.predictions.append(frame_features[prediction_type])

    def __getitem__(self, index):
        return self.embeddings[index], self.predictions[index]

    def __len__(self):
        return len(self.embeddings)


class THOREmbeddingsDataModule(pl.LightningDataModule):

    def __init__(self, data_dir, embedding_type, prediction_type, batch_size=1, num_workers=0):
        super().__init__()
        self.save_hyperparameters()

    def setup(self, stage=None):
        self.train_dataset = THOREmbeddingsDataset(
            self.hparams.data_dir, 'train',
            self.hparams.embedding_type, self.hparams.prediction_type
        )
        self.val_dataset = THOREmbeddingsDataset(
            self.hparams.data_dir, 'val',
            self.hparams.embedding_type, self.hparams.prediction_type
        )
        self.test_dataset = THOREmbeddingsDataset(
            self.hparams.data_dir, 'test',
            self.hparams.embedding_type, self.hparams.prediction_type
        )

    def train_dataloader(self):
        return DataLoader(
            self.train_dataset, batch_size=self.hparams.batch_size, shuffle=True,
            num_workers=int(0.8 * self.hparams.num_workers)
        )

    def val_dataloader(self):
        return DataLoader(
            self.val_dataset, batch_size=self.hparams.batch_size, shuffle=False,
            num_workers=int(0.2 * self.hparams.num_workers)
        )

    def test_dataloader(self):
        return DataLoader(
            self.test_dataset, batch_size=self.hparams.batch_size, shuffle=False,
            num_workers=int(self.hparams.num_workers)
        )


class LinearEncoder(pl.LightningModule):
    def __init__(self, embedding_type, prediction_type, batch_size, lr):
        super().__init__()
        self.save_hyperparameters()

        if prediction_type == 'object_presence_grid':
            assert embedding_type in ['rn50_imagenet_conv', 'clip_conv']
            self.model = nn.Sequential(
                nn.AdaptiveAvgPool2d(output_size=(3,3)),
                nn.Conv2d(2048, len(target_objects), kernel_size=1),
                nn.Flatten(start_dim=2),
                nn.Sigmoid()
            )

        elif prediction_type in ['object_presence', 'valid_moves_forward', 'valid_moves_forward_cls', 'pickupable_objects']:
            assert embedding_type in ['rn50_imagenet_avgpool', 'clip_avgpool', 'clip_attnpool']

            if embedding_type in ['rn50_imagenet_avgpool', 'clip_avgpool']:
                input_dim = 2048
            elif embedding_type == 'clip_attnpool':
                input_dim = 1024

            if prediction_type == 'object_presence':
                output_dim = len(target_objects)
                act_fn = nn.Sigmoid()
            elif prediction_type == 'valid_moves_forward':
                output_dim = 1
                act_fn = nn.ReLU()
            elif prediction_type == 'valid_moves_forward_cls':
                output_dim = max_forward_steps + 1
                act_fn = nn.Softmax()
            elif prediction_type == 'pickupable_objects':
                output_dim = 110
                act_fn = nn.Sigmoid()

            self.model = nn.Sequential(
                nn.Linear(input_dim, output_dim),
                act_fn
            )

        else: raise NotImplementedError()

    def forward(self, x):
        return self.model(x)

    def compute_loss(self, batch, eval=False):
        x, y = batch

        if self.hparams.prediction_type == 'object_presence_grid':
            y = y.flatten(start_dim=1)
        elif self.hparams.prediction_type == 'valid_moves_forward':
            y = y.unsqueeze(1)
        elif self.hparams.prediction_type == 'valid_moves_forward_cls':
            y[y > max_forward_steps] = max_forward_steps
            y = F.one_hot(y, num_classes=(max_forward_steps+1))
        elif self.hparams.prediction_type == 'pickupable_objects':
            obj_idx, pickupable = y
            obj_idx = obj_idx.tolist()

        y_pred = self.forward(x)

        if self.hparams.prediction_type == 'object_presence_grid':
            y_pred = y_pred.permute(0, 2, 1).flatten(start_dim=1)
        elif self.hparams.prediction_type == 'pickupable_objects':
            y_pred = y_pred[range(len(obj_idx)), obj_idx]

        # compute loss
        if self.hparams.prediction_type in ['object_presence', 'object_presence_grid', 'valid_moves_forward_cls']:
            loss = F.cross_entropy(y_pred, y.float())
        elif self.hparams.prediction_type == 'valid_moves_forward':
            loss = F.mse_loss(y_pred, y.float())
        elif self.hparams.prediction_type == 'pickupable_objects':
            loss = F.binary_cross_entropy(y_pred, pickupable.float())

        if eval is False:
            return loss

        # compute metrics
        metrics = {}
        if self.hparams.prediction_type in ['object_presence', 'object_presence_grid']:
            metrics['accuracy'] = MF.f1(y_pred, y)
        elif self.hparams.prediction_type == 'valid_moves_forward':
            metrics['accuracy'] = MF.mean_absolute_error(y_pred, y)
        elif self.hparams.prediction_type == 'valid_moves_forward_cls':
            metrics['accuracy'] = torch.mean(((y_pred > 0.5) == y)[y == 1].float())
        elif self.hparams.prediction_type == 'pickupable_objects':
            metrics['accuracy'] = ((y_pred > 0.5) == pickupable).float().mean()

        return loss, metrics

    def training_step(self, batch, batch_idx):
        loss = self.compute_loss(batch)
        self.log("train_loss", loss)
        return loss

    def validation_step(self, batch, batch_idx):
        loss, metrics = self.compute_loss(batch, eval=True)
        self.log("val_loss", loss)
        self.log("val_acc", metrics['accuracy'])
        return loss

    def test_step(self, batch, batch_idx):
        loss, metrics = self.compute_loss(batch, eval=True)
        self.log("test_loss", loss)
        self.log("test_acc", metrics['accuracy'])
        return loss

    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.parameters(), lr=self.hparams.lr)
        return optimizer


if __name__ == '__main__':
    pl.seed_everything(1)

    gpus = 1

    # embedding_type: 'rn50_imagenet_conv', 'rn50_imagenet_avgpool', 'clip_conv', 'clip_attnpool', 'clip_avgpool'
    # prediction_type: 'object_presence', 'object_presence_grid', 'valid_moves_forward', 'pickupable_objects'
    embedding_type = 'clip_avgpool'
    prediction_type, data_path = 'object_presence', os.path.expanduser('~/nfs/clip-embodied-ai/datasets/ithor_scenes')
    # prediction_type, data_path = 'pickupable_objects', os.path.expanduser('~/nfs/clip-embodied-ai/datasets/pickupable_objects')
    batch_size = 128
    lr = 0.001

    root_dir = os.path.expanduser('~/nfs/clip-embodied-ai/logs/linear_probe')
    experiment_name = f'{prediction_type}'
    experiment_version = f'{embedding_type}_bs{batch_size}_lr{lr}'

    logger = pl.loggers.TensorBoardLogger(root_dir, name=experiment_name, version=experiment_version)

    dm = THOREmbeddingsDataModule(
        data_path,
        embedding_type, prediction_type,
        batch_size=batch_size, num_workers=16
    )

    model = LinearEncoder(embedding_type, prediction_type, batch_size, lr)

    trainer = pl.Trainer(
        default_root_dir=root_dir,
        logger=logger,
        gpus=gpus,
        val_check_interval=0.5,
        max_epochs=250,
        callbacks=[
            pl.callbacks.ModelCheckpoint(
                monitor="val_loss",
                filename="{epoch:02d}-{val_loss:.2f}",
                mode="min"
            )
        ],
    )

    trainer.fit(model, dm)
    trainer.test(
        model=model,
        datamodule=dm,
        ckpt_path='best'
    )
