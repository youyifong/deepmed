#!/usr/bin/env python3
from deepmed.experiment_imports import *

# this is a tiny toy data set; do not expect any good results from this
cohort_path = untar_data(
    'https://katherlab-datasets.s3.eu-central-1.amazonaws.com/tiny-test-data.zip')

cohorts_df = cohort(
    tiles_path=cohort_path/'tiles',
    clini_path=cohort_path/'clini.csv',
    slide_path=cohort_path/'slide.csv')


def main():
    do_experiment(
        project_dir='multi_target_crossval',
        get=partial(
            get.multi_target,
            get.crossval,
            get.simple_run,
            train_cohorts_df=cohorts_df,
            target_labels=['ER Status By IHC'],
            max_train_tile_num=128,
            max_valid_tile_num=64,
            max_test_tile_num=256,
            valid_frac=.2,
            multi_target_evaluators=[
                partial(aggregate_stats, group_levels=[0, -1])],
            crossval_evaluators=[aggregate_stats],
            evaluators=[Grouped(auroc), Grouped(count)],
            train=partial(
                train,
                batch_size=96,
                max_epochs=4),
        ),
        devices={'cuda:0': 4})


if __name__ == '__main__':
    main()
