"""
DAG yt_embed - keeps the search index (yti.video_chunks) up to date.

Runs on the same Dataset event as yt_report (yt_collect finished publishing),
but as a separate DAG: an embedding failure must not block the daily brief,
and the brief does not need embeddings. `embed` only embeds new or changed
chunks, so a daily run costs close to nothing.
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
    dag_id="yt_embed",
    description="Embed new/changed video titles and descriptions for assistant search",
    default_args={"owner": "dataengineers", "retries": 1, "retry_delay": timedelta(minutes=5)},
    start_date=pendulum.datetime(2026, 10, 16, tz=local_tz),
    schedule=[Dataset(OBSERVATIONS_DATASET_URI)],
    catchup=False,
    max_active_runs=1,
    dagrun_timeout=timedelta(minutes=30),
    tags=["youtube", "rag", "llm"],
) as dag:

    BashOperator(
        task_id="embed_videos",
        bash_command="cd /opt/airflow && python -m youtube_intel.cli embed",
        doc_md="Chunk + embed titles/descriptions into `yti.video_chunks` (only new or changed).",
    )
