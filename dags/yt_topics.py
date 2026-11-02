"""
DAG yt_topics - weekly topic clusters and S4-S6 (spec §8.2).

Only computes when derived_metrics_enabled is true (R2); otherwise succeeds as a
no-op. Needs the bge-m3 model installed in the image (`.[embeddings]`).
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
    dag_id="yt_topics",
    description="Weekly topic clusters + S4-S6 (only with derived_metrics_enabled)",
    default_args={"owner": "dataengineers", "retries": 1, "retry_delay": timedelta(minutes=15)},
    start_date=pendulum.datetime(2026, 11, 2, tz=local_tz),
    schedule="0 23 * * 0",           # Sunday 23:00, after the day's collection
    catchup=False,
    max_active_runs=1,
    dagrun_timeout=timedelta(hours=2),
    tags=["youtube", "signals", "topics"],
) as dag:

    BashOperator(
        task_id="cluster_topics",
        bash_command="cd /opt/airflow && python -m youtube_intel.cli topics",
        doc_md="bge-m3 + HDBSCAN into `yti.topic_clusters`; no-op while derived metrics are off.",
    )
