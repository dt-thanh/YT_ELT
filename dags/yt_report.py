"""
DAG sinh bản tin - chạy THEO DỮ LIỆU, không theo giờ.

⭐ LỊCH THEO DATASET (data-aware scheduling, Airflow 2.4+)
    schedule=[Dataset(...)]  ->  DAG này chạy MỖI KHI yt_collect cập nhật bảng
                                 video_observations.

So với các cách khác:
    Theo GIỜ (21:30)       : collect chạy lâu hơn 30 phút là report đọc dữ liệu CŨ.
    TriggerDagRunOperator  : collect phải GỌI ĐÍCH DANH report -> trói chặt.
                             Đây là cách 3 DAG cũ dùng (xem Bước 9).
    Dataset                : collect chỉ nói "tôi đã ghi X". Ai cần X thì tự nghe.
                             Thêm DAG thứ ba cũng nghe X -> không sửa collect.

⭐ VÌ SAO yt_quality (Soda) KHÔNG dùng Dataset như DAG này?
Vì nhiệm vụ của Soda là phát hiện "pipeline NGỪNG chạy". Nếu nó chỉ chạy khi
collect chạy xong thì pipeline chết là Soda cũng im -> vô dụng.
    Report  : PHỤ THUỘC dữ liệu mới  -> chạy theo dữ liệu (Dataset)
    Monitor : phải ĐỘC LẬP           -> chạy theo giờ riêng
Cùng là "chạy sau collect" nhưng mục đích khác nhau -> cách lập lịch khác nhau.
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
    dag_id="yt_report",
    description="Sinh bản tin mỗi nhóm (LLM + validator, rơi về bản mẫu nếu hỏng)",
    default_args={
        "owner": "dataengineers",
        # Retry 1 lần: lỗi mạng tới OpenAI thường là tạm thời. Không retry nhiều
        # hơn vì mỗi lần là tiền thật, và bản mẫu đã là lưới an toàn.
        "retries": 1,
        "retry_delay": timedelta(minutes=5),
    },
    start_date=pendulum.datetime(2026, 9, 24, tz=local_tz),
    schedule=[Dataset(OBSERVATIONS_DATASET_URI)],
    catchup=False,
    max_active_runs=1,
    dagrun_timeout=timedelta(minutes=20),
    tags=["youtube", "report", "llm"],
) as dag:

    BashOperator(
        task_id="generate_reports",
        # Không có --no-llm: dùng LLM nếu llm_enabled và có key; thiếu key thì
        # lệnh TỰ rơi về bản mẫu (cảnh báo trong log), KHÔNG làm task đỏ.
        bash_command="cd /opt/airflow && python -m youtube_intel.cli report",
        doc_md=(
            "Sinh bản tin cho mỗi nhóm từ `yti.v_video_growth`.\n\n"
            "Kết quả lưu ở `yti.reports`. Cột `status`: `validated` = LLM đã qua "
            "validator; `draft` = bản mẫu (LLM tắt, thiếu key, hoặc hỏng 2 lần)."
        ),
    )
