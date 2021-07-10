#!/usr/bin/env python3

import logging
from typing import Iterable, Optional, Callable, Sequence, Tuple, Iterator
from pathlib import Path
from multiprocessing import Manager, Process
from multiprocessing.pool import ThreadPool
from multiprocessing.synchronize import Semaphore
from functools import partial, lru_cache

import pandas as pd
import torch

from ._train import train
from ._deploy import deploy
from .utils import Lazy
from .metrics import Metric
from .types import *


__all__ = ['do_experiment']


logger = logging.getLogger(__name__)


def do_experiment(
        project_dir: PathLike,
        get: RunGetter,
        train: Trainer = train,
        deploy: Deployer = deploy,
        num_concurrent_runs: int = 4,
        devices = [torch.cuda.current_device()],
        evaluator_groups: Sequence[Iterable[Metric]] = []) -> None:
    """Runs an experiement.

    Args:
        project_dir:  The directory to save project data in.
        get:  A function which generates runs.
        train:  A function training a model for a specific run.
        deploy:  A function deploying a trained model.
        num_concurrent_runs:  The maximum amount of runs to do at the same time.
            Useful for multi-GPU systems.
        devices:  The devices to use for training.
        evaluator_groups:  TODO
    """
    project_dir = Path(project_dir)
    project_dir.mkdir(exist_ok=True, parents=True)

    # add logfile handler
    file_handler = logging.FileHandler(project_dir/'logfile')
    file_handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s: %(levelname)s: %(name)s: %(message)s')
    file_handler.setFormatter(formatter)
    logging.getLogger().addHandler(file_handler)

    logger.info('Getting runs')

    with Manager() as manager:
        # semaphores which tell us which GPUs still have resources free
        # each gpu is a assumed to have the same capabilities
        capacities = [
            manager.Semaphore(max(1, (num_concurrent_runs+len(devices)-1)//len(devices)))  # type: ignore
            for _ in devices]
        run_args = ({'run': run, 'train': train, 'deploy': deploy, 'devices': devices,
                     'capacities': capacities}
                     for run in get(project_dir=project_dir))

        # We use a ThreadPool which starts processes so our launched processes are:
        #  1. Terminated after each training run so we don't leak resources
        #  2. We can spawn more processes in the launched subprocesses (not possible with Pool)
        with ThreadPool(num_concurrent_runs or 1) as pool:
            # only use pool if we actually want to run multiple runs in parallel
            runs = (pool.imap(_do_run_wrapper, run_args, chunksize=1) if num_concurrent_runs >= 1
                    else (_do_run_wrapper(args, spawn_process=False) for args in run_args))
            runs = (run for run in runs if run is not None)
            _evaluate_runs(
                runs, project_dir=project_dir, evaluator_groups=evaluator_groups) # type: ignore


def _evaluate_runs(
        runs: Iterator[Run], project_dir: Path, evaluator_groups: Sequence[Iterable[Callable]]) \
        -> None:
    """Calls evaluation functions for each run.

    Args:
        runs:  An iterator over the already completed runs.  This iterator has
            to traverse the runs in-order.
        project_dir:  The root directory of the experiment.
        evaluator_groups:  A sequence of collections of evaluation functions.

    TODO a more detailed description

    Assume we have the evaluator groups `[A, B, C]`.  Then the the evaluator
    groups will be invoked as follows:

        root/a/b
        root/a/c   -> C(b)
        root/a/d   -> C(c)
        root/e/f   -> C(d), B(a)
        root/e/g/h -> C(f)
        root/e/g/i
        root/e/j   -> C(g)
                   -> C(j), B(e), A(root)

    where B(a) means that all the evaluation functions in evaluator group B will
    be invoked on run a.
    """
    last_run = None

    for run in runs:
        run_dir_rel = run.directory.relative_to(project_dir)

        if last_run:
            first_differing_level = \
                next(i for i, (old, new) in enumerate(zip(last_run_dir_rel.parts,
                                                            run_dir_rel.parts))
                        if old != new)
            paths_and_evaluator_groups = list(zip([*reversed(last_run_dir_rel.parents),
                                                    last_run_dir_rel],
                                                    evaluator_groups))
            _run_evaluators(
                last_run.target, project_dir,
                paths_and_evaluator_groups[first_differing_level+1:])
        last_run, last_run_dir_rel = run, run_dir_rel
    if last_run:
        paths_and_evaluator_groups = list(zip([*reversed(run_dir_rel.parents), run_dir_rel],
                                                evaluator_groups))
        _run_evaluators(run.target, project_dir, paths_and_evaluator_groups)


def _run_evaluators(
        target_label: str, project_dir: Path,
        paths_and_evaluator_groups: Sequence[Tuple[Path, Iterable[Metric]]]):
    for path, evaluators in reversed(paths_and_evaluator_groups):
        logger.info(f'Evaluating {path}')
        eval_dir = project_dir/path
        if not evaluators:
            continue

        preds_df = Lazy(partial(_get_preds_df, result_dir=eval_dir))

        #TODO rewrite this functionally (its nothing but a reduce operation)
        stats_df = None
        for evaluate in evaluators:
            if (df := evaluate(target_label, preds_df, eval_dir)) is not None:
                if stats_df is None:
                    stats_df = df
                else:
                    # make sure the two dfs have the same column level
                    levels = max(stats_df.columns.nlevels, df.columns.nlevels)
                    stats_df = _raise_df_column_level(stats_df, levels)
                    df = _raise_df_column_level(df, levels)
                    stats_df = stats_df.join(df)
        if stats_df is not None:
            stats_df.to_csv(eval_dir/'stats.csv')


def _raise_df_column_level(df, level):
    if df.columns.empty:
        columns = pd.MultiIndex.from_product([[]] * level)
    elif isinstance(df.columns, pd.MultiIndex):
        columns = pd.MultiIndex.from_tuples([col + (None,)*(level-df.columns.nlevels)
                                             for col in df.columns])
    else:
        columns = pd.MultiIndex.from_tuples([(col,) + (None,)*(level-df.columns.nlevels)
                                             for col in df.columns])

    return pd.DataFrame(df.values, index=df.index, columns=columns)


@lru_cache(maxsize=4)
def _get_preds_df(result_dir: Path) -> pd.DataFrame:
    # load predictions
    if (preds_path := result_dir/'predictions.csv.zip').exists():
        preds_df = pd.read_csv(preds_path)
    else:
        # create an accumulated predictions df if there isn't one already
        dfs = []
        for df_path in result_dir.glob('**/predictions.csv.zip'):
            df = pd.read_csv(df_path)
            # column which tells us which subset these predictions are from
            #TODO do this for each directory level from result_dir to df_path
            df[f'subset_{result_dir.name}'] = df_path.name
            dfs.append(df)

        preds_df = pd.concat(dfs)
        preds_df.to_csv(preds_path, index=False, compression='zip')

    return preds_df


def _do_run(
        run: Run, train: Trainer, deploy: Deployer, devices: Iterable,
        capacities: Iterable[Semaphore] = []) \
        -> None:
    logger = logging.getLogger(str(run.directory))
    logger.info(f'Starting run')

    for device, capacity in zip(devices, capacities):
        # search for a free gpu
        if not capacity.acquire(blocking=False): continue   # type: ignore
        try:
            with torch.cuda.device(device):
                learn = train(run)
                deploy(learn, run)
                break
        finally: capacity.release()
    else:
        raise RuntimeError('Could not find a free GPU!')


def _do_run_wrapper(kwargs, spawn_process: bool = True) -> Optional[Run]:
    """Starts a new process to train a model."""
    run = kwargs['run']
    # Starting a new process guarantees that the allocaded CUDA resources will
    # be released upon completion of training.
    if spawn_process:
        p = Process(target=_do_run, kwargs=kwargs)
        p.start()
        p.join()
        if p.exitcode == 0:
            return run
        else:
            return None
    else:
        _do_run(**kwargs)
        return run


def _load_learner_to_device(fname, device=None):
    """Loads a learner to a specific device."""
    device = torch.device(device or torch.cuda.current_device())
    res = torch.load(fname, map_location=device)
    res.dls.device = device
    if hasattr(res, 'to_fp32'): res = res.to_fp32()
    return res