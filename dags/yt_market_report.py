"""
DAG yt_market_report - the Report agent's daily market report per pillar (spec §7.3).

Runs when yt_collect publishes new observations. No OpenAI call unless
report.llm_summary is turned on in config/studio.yaml.
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
    dag_id="yt_market_report",
    description="Daily market report per pillar (Report agent)",
    default_args={"owner": "dataengineers", "retries": 1, "retry_delay": timedelta(minutes=5)},
    start_date=pendulum.datetime(2026, 11, 2, tz=local_tz),
    schedule=[Dataset(OBSERVATIONS_DATASET_URI)],
    catchup=False,
    max_active_runs=1,
    dagrun_timeout=timedelta(minutes=30),
    tags=["youtube", "studio", "report"],
) as dag:

    BashOperator(
        task_id="market_report",
        bash_command="cd /opt/airflow && python -m youtube_intel.cli market-report",
        doc_md="One grounded market report per pillar into `yti.reports`.",
    )
