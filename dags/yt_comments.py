"""
DAG yt_comments - comments of outstanding videos: collect, classify, cluster
questions (spec §8.3). Runs after yt_signals has had time to score the day.

Only computes when derived_metrics_enabled is true (R2); otherwise a no-op.
Classification calls OpenAI through the studio gateway (cost ceiling per run).
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
    dag_id="yt_comments",
    description="Comment insights for outstanding videos (only with derived_metrics_enabled)",
    default_args={"owner": "dataengineers", "retries": 1, "retry_delay": timedelta(minutes=15)},
    start_date=pendulum.datetime(2026, 11, 2, tz=local_tz),
    schedule="0 23 * * *",           # daily, two hours after the 21:00 collection
    catchup=False,
    max_active_runs=1,
    dagrun_timeout=timedelta(hours=1),
    tags=["youtube", "comments", "llm"],
) as dag:

    BashOperator(
        task_id="comment_insights",
        bash_command="cd /opt/airflow && python -m youtube_intel.cli comments",
        doc_md="Collect -> classify -> viewer_questions; no-op while derived metrics are off.",
    )
