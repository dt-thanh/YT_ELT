from datawarehouse.data_utils import (
    get_conn_cursor,
    close_conn_cursor,
    create_schema,
    create_table,
)
from datawarehouse.data_loading import load_data
from datawarehouse.data_modification import upsert_row      # ### [NÂNG CẤP] chỉ cần 1 hàm
from datawarehouse.data_transformation import transform_data

import logging
from datetime import date
from airflow.decorators import task

logger = logging.getLogger(__name__)
table = "yt_api"


@task
def staging_table():
    schema = "staging"
    snapshot_date = date.today()          # ### [NÂNG CẤP] cùng mốc ngày với partition MinIO
    conn, cur = None, None

    try:
        conn, cur = get_conn_cursor()

        create_schema(schema)
        create_table(schema)

        YT_data = load_data()             # đọc raw JSON từ MinIO

        # ### [NÂNG CẤP] Không còn get_video_ids / insert / update / delete.
        # Chỉ cần gắn snapshot_date rồi UPSERT từng dòng.
        for row in YT_data:
            row["snapshot_date"] = snapshot_date
            upsert_row(cur, conn, schema, row)

        logger.info("%s: upsert xong %d dòng", schema, len(YT_data))

    except Exception as e:
        logger.error("Lỗi khi cập nhật bảng %s: %s", schema, e)
        raise
    finally:
        if conn and cur:
            close_conn_cursor(conn, cur)


@task
def core_table():
    schema = "core"
    conn, cur = None, None

    try:
        conn, cur = get_conn_cursor()

        create_schema(schema)
        create_table(schema)

        # ### [NÂNG CẤP] Chỉ lấy snapshot HÔM NAY từ staging để transform sang core
        # (nếu lấy hết mọi ngày sẽ transform lại cả lịch sử mỗi lần chạy - thừa)
        cur.execute(
            f"""SELECT * FROM staging.{table} WHERE "Snapshot_Date" = %s;""",
            (date.today(),),
        )
        rows = cur.fetchall()

        for row in rows:
            transformed_row = transform_data(row)     # đổi Duration + thêm Video_Type
            upsert_row(cur, conn, schema, transformed_row)

        logger.info("%s: upsert xong %d dòng", schema, len(rows))

    except Exception as e:
        logger.error("Lỗi khi cập nhật bảng %s: %s", schema, e)
        raise
    finally:
        if conn and cur:
            close_conn_cursor(conn, cur)