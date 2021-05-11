#!/usr/bin/env python3
import logging
from typing import Type, Sequence, Tuple, Callable, Optional, Any, Dict, TypeVar, Union, Iterable
from pathlib import Path
from dataclasses import dataclass

import pandas as pd
import torch


logger = logging.getLogger(__name__)

Model = TypeVar('Model')
"""An object which the Deployer can apply to a test set."""


@dataclass
class Run:
    """A collection of data to train or test a model."""
    directory: Path
    """The directory to save data in for this run."""
    target: str
    """The name of the target to train or deploy on."""
    train_df: Optional[pd.DataFrame] = None
    """A dataframe mapping tiles to be used for training to their targets.

    It contains at least the following columns:
    - tile_path: Path
    - is_valid: bool:  whether the tile should be used for validation (e.g. for early stopping).
    - At least one target column with the name saved in the run's `target`.
    """
    test_df: Optional[pd.DataFrame] = None
    """A dataframe mapping tiles used for testing to their targets.

    It contains at least the following columns:
    - tile_path: Path
    """


RunGetter = Callable[..., Sequence[Run]]
"""A function which creates a series of runs."""

Trainer = Callable[..., Model]
"""A function which trains a model.

Required kwargs:
    train_df: TrainDF:  A dataset specifying which tiles to train on.
    target_label: str:  The label to train on.
    result_dir:  A folder to write intermediate results to.

Returns:
    The trained model.
"""

Deployer = Callable[..., pd.DataFrame]
"""A function which deployes a model.

Required kwargs:
    model: Model:  The model to test on.
    target_label: str:  The name to be given to the result column.
    test_df: TestDF:  A dataframe specifying which tiles to deploy the model on.
    result_dir:  A folder to write intermediate results to.

Returns:
    `test_df`, but with at least an additional column for the target predictions.
"""


@dataclass
class Coordinator:
    """Defines how an experiment is to be performed."""
    get: RunGetter
    """A function which generates runs."""
    train: Optional[Trainer] = None
    """A function which trains a model for each of the runs."""
    deploy: Optional[Deployer] = None
    """A function which deploys a trained model to a test set, yielding predictions."""
    evaluate: Optional[Callable] = None
    """A function which takes a model's predictions and calculates metrics, creates graphs, etc."""


PathLike = Union[str, Path]


def do_experiment(*,
        project_dir: PathLike,
        mode: Coordinator,
        model_path: Optional[PathLike] = None,
        save_models: bool = True,
        **kwargs) -> None:
    """Runs an experiement.
    
    Args:
        project_dir: The directory to save project data in.
        mode: how to perform the training / testing process.
        save_models: whether or not to save the resulting models.
    """

    project_dir = Path(project_dir)
    project_dir.mkdir(exist_ok=True)

    # add logfile handler
    file_handler = logging.FileHandler(project_dir/'logfile')
    file_handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s: %(levelname)s: %(name)s: %(message)s')
    file_handler.setFormatter(formatter)
    logging.getLogger().addHandler(file_handler)

    logger.info('Getting runs')
    runs = mode.get(project_dir=project_dir, **kwargs)
    save_run_files_(runs)

    for run in runs:
        logger.info(f'Starting run {run.directory}')
        
        model = (train_(train=mode.train, exp=run, save_models=save_models, **kwargs)
                 if mode.train and run.train_df is not None
                 else None)

        preds_df = (deploy_(deploy=mode.deploy, model=model, run=run, model_path=model_path,
                            **kwargs)
                    if mode.deploy and run.test_df is not None
                    else None)

    if mode.evaluate:
        logger.info('Evaluating')
        preds_df = mode.evaluate(project_dir, **kwargs)


def save_run_files_(runs: Iterable[Run]) -> None:
    for exp in runs:
        exp.directory.mkdir(exist_ok=True, parents=True)
        if exp.train_df is not None and \
                not (training_set_path := exp.directory/'training_set.csv.zip').exists():
            exp.train_df.to_csv(training_set_path, index=False, compression='zip')
        if exp.test_df is not None and \
                not (testing_set_path := exp.directory/'testing_set.csv.zip').exists():
            exp.test_df.to_csv(testing_set_path, index=False, compression='zip')


def train_(train: Trainer, exp: Run, save_models: bool, **kwargs) -> Model:
    model_path = exp.directory/'model.pt'
    if model_path.exists():
        logger.warning(f'{model_path} already exists, using old model!')
        return torch.load(model_path)

    logger.info('Starting training')
    model = train(target_label=exp.target,
                  train_df=exp.train_df,
                  result_dir=exp.directory,
                  **kwargs)
    if save_models:
        torch.save(model, model_path)

    return model


def deploy_(deploy: Deployer, model: Optional[Model], run: Run, model_path: Optional[PathLike],
        **kwargs) -> pd.DataFrame:
    preds_path = run.directory/'predictions.csv.zip'
    if preds_path.exists():
        logger.warning(f'{preds_path} already exists, using old predictions!')
        return pd.read_csv(preds_path)

    if not model:
        logger.info('Loading model')
        model = torch.load(model_path or run.directory/'model.pt')

    logger.info('Getting predictions')
    preds_df = deploy(model=model,
                      target_label=run.target,
                      test_df=run.test_df,
                      result_dir=run.directory,
                      **kwargs)
    preds_df.to_csv(preds_path, index=False, compression='zip')

    return preds_df