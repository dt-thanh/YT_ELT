"""
DAG yt_retention - applies R3 every day: public YouTube API data (raw JSON,
statistics, titles, descriptions, embeddings, briefs) is deleted or cleared once
it is older than api_data_retention_days (config/channels.yaml, at most 30).

A schedule of its own, not a Dataset trigger: deletion must happen even on days
nothing was collected. Without this DAG the 30-day limit is only met if someone
remembers to run `youtube-intel retention --apply`.
"""

from __future__ import annotations

from datetime import timedelta

import pendulum
from airflow import DAG
from airflow.operators.bash import BashOperator

from youtube_intel.config import load_config

CFG = load_config(require=())
local_tz = pendulum.timezone(CFG.settings.report_timezone)

with DAG(
    dag_id="yt_retention",
    description="Delete YouTube API data older than the R3 limits (docs/policy_notes.md)",
    default_args={"owner": "dataengineers", "retries": 2, "retry_delay": timedelta(minutes=15)},
    start_date=pendulum.datetime(2026, 10, 31, tz=local_tz),
    schedule="30 20 * * *",          # daily, before the 21:00 collection
    catchup=False,
    max_active_runs=1,
    dagrun_timeout=timedelta(minutes=30),
    tags=["youtube", "retention", "policy"],
) as dag:

    BashOperator(
        task_id="apply_retention",
        bash_command="cd /opt/airflow && python -m youtube_intel.cli retention --apply",
        doc_md="Staging, R3 rules per data_class (migration 008), orphan and raw cleanup.",
    )
