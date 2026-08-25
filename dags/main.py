from airflow import DAG
import pendulum
from datetime import datetime, timedelta
from airflow.operators.trigger_dagrun import TriggerDagRunOperator

# ### [SỬA] save_to_json -> save_to_minio (vì đã đổi extract sang ghi vào MinIO)
from api.video_stats import (
    get_playlist_id,
    get_video_ids,
    extract_video_data,
    save_to_minio,
)

from datawarehouse.dwh import staging_table, core_table
from dataquality.soda import yt_elt_data_quality

# Múi giờ chạy DAG. Bạn ở VN có thể đổi sang "Asia/Ho_Chi_Minh" nếu muốn
# lịch 14h chạy theo giờ Việt Nam thay vì giờ Malta.
local_tz = pendulum.timezone("Europe/Malta")

# default_args áp cho MỌI task trong cả 3 DAG (trừ chỗ task tự ghi đè)
default_args = {
    "owner": "dataengineers",
    "depends_on_past": False,
    "email_on_failure": False,     # production nên bật + cấu hình alert (Slack/email) = ô "Alerting"
    "email_on_retry": False,
    "email": "data@engineers.com",
    # ### [NÂNG CẤP] Bật retry mặc định cho mọi task (nền an toàn vì task đã idempotent).
    # Task extract trong video_stats.py có @task(retries=3) sẽ TỰ GHI ĐÈ số này.
    "retries": 2,
    "retry_delay": timedelta(minutes=1),
    "max_active_runs": 1,          # cấm 2 lần chạy chồng lên nhau
    "dagrun_timeout": timedelta(hours=1),
    "start_date": datetime(2025, 1, 1, tzinfo=local_tz),
}

staging_schema = "staging"
core_schema = "core"

# ==========================================================================
# DAG 1: produce_json — MODULE 1 (Extract). DAG DUY NHẤT có lịch thật.
# Chạy 14:00 mỗi ngày; xong thì tự kích hoạt DAG update_db.
# ==========================================================================
with DAG(
    dag_id="produce_json",
    default_args=default_args,
    description="DAG to produce JSON file with raw data",
    schedule="0 14 * * *",        # 14:00 hằng ngày (theo local_tz)
    catchup=False,                # không chạy dồn các ngày quá khứ
) as dag_produce:

    playlist_id = get_playlist_id()
    video_ids = get_video_ids(playlist_id)
    extract_data = extract_video_data(video_ids)
    # ### [SỬA] save_to_json -> save_to_minio
    save_task = save_to_minio(extract_data)

    # Extract xong -> "bấm nút" khởi động DAG kế tiếp
    trigger_update_db = TriggerDagRunOperator(
        task_id="trigger_update_db",
        trigger_dag_id="update_db",
    )

    playlist_id >> video_ids >> extract_data >> save_task >> trigger_update_db

# ==========================================================================
# DAG 2: update_db — MODULE 2 (Load + Transform).
# schedule=None: KHÔNG tự chạy, chỉ chạy khi produce_json kích hoạt.
# ==========================================================================
with DAG(
    dag_id="update_db",
    default_args=default_args,
    description="DAG to process JSON file and insert data into both staging and core schemas",
    catchup=False,
    schedule=None,
) as dag_update:

    update_staging = staging_table()
    update_core = core_table()

    trigger_data_quality = TriggerDagRunOperator(
        task_id="trigger_data_quality",
        trigger_dag_id="data_quality",
    )

    update_staging >> update_core >> trigger_data_quality

# ==========================================================================
# DAG 3: data_quality — SECTION 6 (Testing / Soda) = ô "Quality gate".
# schedule=None: chỉ chạy khi update_db kích hoạt.
# ==========================================================================
with DAG(
    dag_id="data_quality",
    default_args=default_args,
    description="DAG to check the data quality on both layers in the database",
    catchup=False,
    schedule=None,
) as dag_quality:

    soda_validate_staging = yt_elt_data_quality(staging_schema)
    soda_validate_core = yt_elt_data_quality(core_schema)

    soda_validate_staging >> soda_validate_core