# Copyright (c) 2020, eQualit.ie inc.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.


from baskerville.models.pipeline_tasks.tasks_base import Task
from baskerville.models.config import BaskervilleConfig
from baskerville.models.pipeline_tasks.tasks import Preprocess, \
    SaveRsInPostgres, \
    Predict, GetDataLog


def set_up_isac_rawlog_pipeline(config: BaskervilleConfig):
    predict_tasks = [
        GetDataLog(
            config,
            steps=[
                Preprocess(config),
                Predict(config),
                SaveRsInPostgres(config),
            ]),
    ]

    isace_raw_log_pipeline = Task(config, predict_tasks)
    isace_raw_log_pipeline.name = 'Raw Log Pipeline'
    return isace_raw_log_pipeline
