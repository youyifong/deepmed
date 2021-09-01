#!/usr/bin/env python3

import logging
from typing import Mapping, Union, Optional
from pathlib import Path
from multiprocessing import Manager, Process
from multiprocessing.pool import ThreadPool

from ._train import train
from ._deploy import deploy
from .types import *


__all__ = ['do_experiment']


logger = logging.getLogger(__name__)


def do_experiment(
        project_dir: PathLike,
        get: RunGetter,
        train: Trainer = train,
        deploy: Deployer = deploy,
        num_concurrent_runs: Optional[int] = None,
        devices: Mapping[Union[str, int], int] = {0: 4},
        logfile: Optional[str] = 'logfile'
        ) -> None:
    """Runs an experiement.

    Args:
        project_dir:  The directory to save project data in.
        get:  A function which generates runs.
        train:  A function training a model for a specific run.
        deploy:  A function deploying a trained model.
        num_concurrent_runs:  The maximum amount of runs to do at the same time.
            If None, the number of runs will grow with the number of available
            devices.  If 0, all jobs will be run in the main process (useful for
            debugging).
        devices:  The devices to use for training and the maximum number of
            models to be trained at once for each device.
    """
    project_dir = Path(project_dir)
    project_dir.mkdir(exist_ok=True, parents=True)

    # add logfile handler
    if logfile is not None:
        file_handler = logging.FileHandler(f'{project_dir/"logfile"}')
        file_handler.setLevel(logging.DEBUG)
        formatter = logging.Formatter('%(asctime)s: %(levelname)s: %(name)s: %(message)s')
        file_handler.setFormatter(formatter)
        logging.getLogger().addHandler(file_handler)

    logger.info('Getting runs')

    with Manager() as manager:
        # semaphores which tell us which GPUs still have resources available
        capacities = {
                device: manager.Semaphore(capacity)   # type: ignore
                for device, capacity in devices.items()}
        run_args = ({'run': run, 'train': train, 'deploy': deploy, 'devices': capacities}
                     for run in get(project_dir=project_dir, manager=manager))
        num_concurrent_runs = \
                sum(devices.values())*3 if num_concurrent_runs is None else num_concurrent_runs

        # We use a ThreadPool which starts processes so our launched processes are:
        #  1. Terminated after each training run so we don't leak resources
        #  2. We can spawn more processes in the launched subprocesses (not possible with Pool)
        with ThreadPool(num_concurrent_runs or 1) as pool:
            # only use pool if we actually want to run multiple runs in parallel
            runs = (pool.imap_unordered(_do_run_wrapper, run_args, chunksize=1)
                    if num_concurrent_runs >= 1
                    else (_do_run_wrapper(args, spawn_process=False) for args in run_args)) # type: ignore
            for _ in runs:
                pass


def _do_run_wrapper(kwargs, spawn_process: bool = True) -> None:
    """Starts a new process to train a model."""
    run = kwargs['run']
    del kwargs['run']
    try:
        # Starting a new process guarantees that the allocaded CUDA resources will
        # be released upon completion of training.
        if spawn_process:
            p = Process(target=run.__call__, kwargs=kwargs)
            p.start()
            p.join()
        else:
            run(**kwargs)
    finally:
        run.done.set()