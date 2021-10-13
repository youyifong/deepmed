#TODO having a train / deploy split for this is kind of silly... Try getting rid of it somehow!

import logging
from fastai.layers import AdaptiveConcatPool2d

from fastai.losses import CrossEntropyLossFlat
from deepmed.utils import exists_and_has_size, factory
from typing import Callable, Optional
from fastai.data.block import DataBlock
from fastai.data.transforms import ColReader
from fastai.vision.data import ImageBlock
from fastai.vision.learner import cnn_learner
from torch import nn
from fastai.learner import Learner
from fastai.vision.augment import Resize
from fastai.vision.models import resnet18
import pandas as pd
from .types import GPUTask


def _extract(
        task: GPUTask, /,
        arch: Callable[[bool], nn.Module] = resnet18,
        tfms: Optional[Callable] = Resize(size=224),
        num_workers: int = 0) -> None:
    logger = logging.getLogger(str(task.path))

    if task.train_df is None:
        logger.warning('Cannot extract features: no training set given!')
        return None
    elif exists_and_has_size(features_path := task.path/'features.csv.zip'):
        logger.warning(f'{features_path} already exists, skipping deployment...')
        return pd.read_csv(features_path, low_memory=False)

    dblock = DataBlock(blocks=(ImageBlock),
                       get_x=ColReader('tile_path'),
                       batch_tfms=tfms)

    dls = dblock.dataloaders(task.train_df, num_workers=num_workers)

    learn = cnn_learner(dls, arch, n_out=2, path=task.path,
                        loss_func=CrossEntropyLossFlat())
    learn.model = nn.Sequential(learn.model[:-1], AdaptiveConcatPool2d())

    test_dl = learn.dls.test_dl(task.train_df)
    feats, _ = learn.get_preds(dl=test_dl, act=nn.Identity())
    feats = feats.squeeze()
    feat_no = feats.shape[-1]//2
    feat_df = pd.DataFrame(feats.squeeze(),
                           columns=[f'max_{i}' for i in range(feat_no)] \
                                  +[f'avg_{i}' for i in range(feat_no)])

    feat_df = pd.concat([task.train_df, feat_df], axis=1)
    feat_df.to_csv(features_path, compression='zip', index=False)

    return None


Extract = factory(_extract)
