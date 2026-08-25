import json
import logging
from datetime import date

import boto3
from airflow.models import Variable

logger = logging.getLogger(__name__)


def load_data():
    # ### [NÂNG CẤP] đọc từ MinIO thay vì file local ./data/
    snapshot_date = date.today()
    bucket = "youtube-raw"
    key = f"videos/date={snapshot_date}/data.json"   # ĐÚNG path mà save_to_minio đã ghi

    s3 = boto3.client(
        "s3",
        endpoint_url=Variable.get("MINIO_ENDPOINT"),
        aws_access_key_id=Variable.get("MINIO_ACCESS_KEY"),
        aws_secret_access_key=Variable.get("MINIO_SECRET_KEY"),
    )

    try:
        logger.info("Đọc raw JSON từ MinIO: s3://%s/%s", bucket, key)

        response = s3.get_object(Bucket=bucket, Key=key)
        data = json.loads(response["Body"].read().decode("utf-8"))

        return data

    # NoSuchKey = "không có file này" - bản MinIO tương đương FileNotFoundError
    except s3.exceptions.NoSuchKey:
        logger.error("Không tìm thấy file trong MinIO: %s", key)
        raise
    except json.JSONDecodeError:
        logger.error("JSON lỗi trong file: %s", key)
        raise