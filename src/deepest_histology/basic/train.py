import shutil
from typing import Callable, Iterable
import logging
from pathlib import Path
from fastai.callback.tracker import TrackerCallback

import torch
import pandas as pd
from torch import nn

from fastai.vision.all import (
    Optimizer, Adam, Learner, DataBlock, ImageBlock, CategoryBlock, ColReader, ColSplitter,
    resnet18, cnn_learner, BalancedAccuracy, RocAucBinary, SaveModelCallback, EarlyStoppingCallback,
    CSVLogger, CrossEntropyLossFlat, aug_transforms)

from ..utils import log_defaults


@log_defaults
def train(target_label: str, train_df: pd.DataFrame, result_dir: Path,
          arch: Callable[[bool], nn.Module] = resnet18,
          batch_size: int = 64,
          max_epochs: int = 10,
          opt: Optimizer = Adam,
          lr: float = 2e-3,
          patience: int = 3,
          num_workers: int = 0,
          tfms: Callable = aug_transforms(
              flip_vert=True, max_rotate=360, max_zoom=1, max_warp=0, size=224),
          metrics: Iterable[Callable] = [BalancedAccuracy(), RocAucBinary()],
          monitor: str = 'valid_loss',
          logger = logging,
          **kwargs) -> Learner:

    dblock = DataBlock(blocks=(ImageBlock, CategoryBlock),
                       get_x=ColReader('tile_path'),
                       get_y=ColReader(target_label),
                       splitter=ColSplitter('is_valid'),
                       batch_tfms=tfms)
    dls = dblock.dataloaders(train_df, bs=batch_size, num_workers=num_workers)

    target_col_idx = train_df[~train_df.is_valid].columns.get_loc(target_label)

    logger.debug(f'Class counts in training set: {train_df[~train_df.is_valid].iloc[:, target_col_idx].value_counts()}')
    logger.debug(f'Class counts in validation set: {train_df[train_df.is_valid].iloc[:, target_col_idx].value_counts()}')

    counts = train_df[~train_df.is_valid].iloc[:, target_col_idx].value_counts()

    counts = torch.tensor([counts[k] for k in dls.vocab])
    weights = 1 - (counts / sum(counts))

    logger.info(f'{dls.vocab = }, {weights = }')

    learn = cnn_learner(
        dls, arch,
        path=result_dir,
        loss_func=CrossEntropyLossFlat(weight=weights.cuda()),
        metrics=metrics,
        opt_func=opt)

    cbs = [SaveModelCallback(monitor=monitor, with_opt=True, reset_on_fit=False),
           EarlyStoppingCallback(monitor=monitor, min_delta=0.001,
                               patience=patience, reset_on_fit=False),
           CSVLogger(append=True)]

    if (result_dir/'models'/'model.pth').exists():
        fit_from_checkpoint(learn, lr/2, max_epochs, cbs, monitor, logger)
    else:
        learn.fine_tune(epochs=max_epochs, base_lr=lr, cbs=cbs)

    learn.export()

    shutil.rmtree(result_dir/'models')

    return learn


def fit_from_checkpoint(
        learn: Learner, lr: float, max_epochs: int, cbs: Iterable[Callable], monitor: str, logger) \
        -> None:
    logger.info('Continuing from checkpoint...')
    learn.load('model')

    # get performance of checkpoint model
    best_scores = learn.validate()
    idx = learn.recorder.metric_names[2:].index(monitor)
    logger.info(f'Checkpoint\'s {monitor}: {best_scores[idx]}')

    # update tracker callback's high scores
    for cb in cbs:
        if isinstance(cb, TrackerCallback):
            cb.best = best_scores[idx]

    learn.unfreeze()
    learn.fit_one_cycle(max_epochs, slice(lr/100, lr), pct_start=.3, div=5.)