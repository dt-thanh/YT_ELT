"""
TẦNG ORCHESTRATION — bọc logic thuần trong youtube_client.py thành Airflow task.

Mỗi task ở đây phải MỎNG, chỉ làm đúng 3 việc:
    1. Đọc config (Variable.get)  - việc của Airflow
    2. Gọi hàm ở tầng logic thuần - việc của nghiệp vụ
    3. Ghi log                    - việc của vận hành

Nếu một task ở đây bắt đầu dài ra và chứa vòng lặp, if/else nghiệp vụ,
thì logic đó ĐANG ĐI NHẦM CHỖ - phải đẩy xuống youtube_client.py.
"""

import json
import logging
from datetime import date, timedelta

import boto3
from airflow.decorators import task
from airflow.models import Variable

from api.youtube_client import (
    fetch_playlist_id,
    fetch_video_ids,
    fetch_video_details,
)

logger = logging.getLogger(__name__)

# ### [BUỔI 2] KHÔNG gọi Variable.get() ở cấp module.
# Mọi dòng ở cấp module chạy lại MỖI LẦN Airflow parse file DAG (mặc định 30s/lần),
# tức mỗi lần lại query metadata database - hàng nghìn query vô ích mỗi ngày, và DAG
# vỡ ngay lúc parse nếu Variable chưa tồn tại. Config phải đọc BÊN TRONG task.
# (Buổi 7 đào sâu vòng đời parse-vs-run.)

# Retry TẦNG NGOÀI: Airflow chạy lại CẢ TASK khi retry tầng trong (tenacity,
# trong youtube_client.call_api) đã bó tay. Đắt hơn nhưng cứu được nhiều loại lỗi hơn.
TASK_ARGS = dict(
    retries=3,
    retry_delay=timedelta(seconds=30),
    retry_exponential_backoff=True,
)


@task(**TASK_ARGS)
def get_playlist_id() -> str:
    api_key = Variable.get("API_KEY")
    channel_id = Variable.get("CHANNEL_ID", default_var=None)
    handle = Variable.get("CHANNEL_HANDLE", default_var=None)

    playlist_id, resolved_channel_id = fetch_playlist_id(api_key, channel_id, handle)

    if not channel_id:
        # Code tự dạy người vận hành cách cấu hình đúng cho lần sau.
        logger.warning(
            "Đang định danh kênh bằng handle (không bền). "
            "Hãy đặt Airflow Variable CHANNEL_ID=%s để cố định kênh.",
            resolved_channel_id,
        )

    logger.info("Đã lấy playlist uploads: %s", playlist_id)
    return playlist_id


@task(**TASK_ARGS)
def get_video_ids(playlist_id: str) -> list[str]:
    video_ids = fetch_video_ids(Variable.get("API_KEY"), playlist_id)
    logger.info("Tổng số video lấy được: %d", len(video_ids))
    return video_ids


@task(**TASK_ARGS)
def extract_video_data(video_ids: list[str]) -> list[dict]:
    extracted_data = fetch_video_details(Variable.get("API_KEY"), video_ids)
    logger.info("Đã trích xuất chi tiết %d video", len(extracted_data))
    return extracted_data


@task(**TASK_ARGS)
def save_to_minio(extracted_data: list[dict]) -> str:
    # TODO [BUỔI 9]: đây là trách nhiệm LOAD-TO-LAKE, không phải EXTRACT.
    # Tách sang dags/storage/minio.py để file này chỉ còn lo chuyện YouTube.
    s3 = boto3.client(
        "s3",
        endpoint_url=Variable.get("MINIO_ENDPOINT"),       # http://minio:9000
        aws_access_key_id=Variable.get("MINIO_ACCESS_KEY"),
        aws_secret_access_key=Variable.get("MINIO_SECRET_KEY"),
    )
    bucket = "youtube-raw"
    if bucket not in [b["Name"] for b in s3.list_buckets()["Buckets"]]:
        s3.create_bucket(Bucket=bucket)

    # TODO [BUỔI 8]: date.today() khiến pipeline KHÔNG backfill được và có thể
    # lệch partition nếu DAG chạy vắt qua nửa đêm. Phải thay bằng logical date (ds).
    key = f"videos/date={date.today()}/data.json"          # partition theo ngày
    body = json.dumps(extracted_data, indent=4, ensure_ascii=False)
    s3.put_object(Bucket=bucket, Key=key, Body=body.encode("utf-8"))

    logger.info("Đã đẩy %d video lên s3://%s/%s", len(extracted_data), bucket, key)
    return key
