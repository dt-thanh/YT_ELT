"""
DAG yt_signals - S1-S3 after each collection (same Dataset as yt_report).

Only computes when derived_metrics_enabled is true (R2); otherwise the task
prints why and succeeds, so basic mode stays green.
"""

from __future__ import annotations

from datetime import timedelta

import pendulum
from airflow import DAG
from airflow.datasets import Dataset
from airflow.operators.bash import BashOperator

from youtube_intel.config import load_config
from youtube_intel.pipeline import OBSERVATIONS_DATASET_URI

CFG = load_config(require=())
local_tz = pendulum.timezone(CFG.settings.report_timezone)

with DAG(
    dag_id="yt_signals",
    description="Video signals S1-S3 (only with derived_metrics_enabled)",
    default_args={"owner": "dataengineers", "retries": 1, "retry_delay": timedelta(minutes=5)},
    start_date=pendulum.datetime(2026, 11, 2, tz=local_tz),
    schedule=[Dataset(OBSERVATIONS_DATASET_URI)],
    catchup=False,
    max_active_runs=1,
    dagrun_timeout=timedelta(minutes=30),
    tags=["youtube", "signals"],
) as dag:

    BashOperator(
        task_id="compute_signals",
        bash_command="cd /opt/airflow && python -m youtube_intel.cli signals",
        doc_md="S1-S3 into `yti.video_signals`; no-op while derived metrics are off.",
    )
