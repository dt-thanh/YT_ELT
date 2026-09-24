"""
DAG giám sát chất lượng dữ liệu - vai "CAMERA".

⭐ VÌ SAO LÀ DAG RIÊNG, KHÔNG PHẢI MỘT TASK TRONG yt_collect?

Đây là điểm dễ làm sai nhất. Nếu Soda chạy SAU collect trong CÙNG một DAG thì:
    pipeline ngừng chạy  ->  Soda cũng ngừng chạy  ->  check freshness
    KHÔNG BAO GIỜ NỔ.
Camera tắt cùng lúc với đèn - đúng lúc cần nó nhất thì nó không có ở đó.

NGUYÊN TẮC: THỨ GIÁM SÁT PHẢI CÓ NHỊP ĐỘC LẬP VỚI THỨ NÓ GIÁM SÁT.
DAG này chạy 6 giờ/lần, bất kể yt_collect có chạy hay không. Với ngưỡng
freshness 30h, một sự cố "pipeline chết" sẽ bị phát hiện trong vòng ~36 giờ.

PHÂN VAI, ĐỪNG NHẦM:
    quality.py (Bước 7)  = KHÓA CỬA. Trước publish, trong transaction, CHẶN được.
    Soda (file này)      = CAMERA.   Sau publish, toàn bảng, chỉ BÁO ĐỘNG.
Trùng nhau vài check là CÓ CHỦ Ý - lớp phòng thủ thứ hai (defense in depth).
"""

from __future__ import annotations

from datetime import timedelta

import pendulum
from airflow import DAG
from airflow.operators.bash import BashOperator

from youtube_intel.config import load_config

CFG = load_config(require=())
local_tz = pendulum.timezone(CFG.settings.report_timezone)

SODA = "/opt/airflow/include/soda"

# Mã thoát của soda scan:
#   0 = mọi check PASS
#   1 = có WARNING   -> KHÔNG làm task đỏ (cảnh báo là để người xem, không phải để chặn)
#   2 = có FAILURE   -> task ĐỎ
#   3 = lỗi kỹ thuật -> task ĐỎ
#
# Vì sao warning không làm đỏ? Vì Airflow chỉ có hai trạng thái success/failed.
# Nếu warning cũng đỏ, người vận hành sẽ quen với màu đỏ và NGỪNG NHÌN -
# alert fatigue. Warning vẫn nằm đầy đủ trong log để đọc khi cần.
SODA_CMD = (
    f"cd /opt/airflow && "
    f"soda scan -d pg_datasource -c {SODA}/configuration.yml {SODA}/checks.yml; "
    f"code=$?; "
    f'if [ "$code" -le 1 ]; then exit 0; else exit "$code"; fi'
)

with DAG(
    dag_id="yt_quality",
    description="Soda: giám sát vùng đã công bố (chạy ĐỘC LẬP với yt_collect)",
    default_args={
        "owner": "dataengineers",
        "retries": 1,
        "retry_delay": timedelta(minutes=10),
    },
    start_date=pendulum.datetime(2026, 9, 23, tz=local_tz),
    # 6 giờ/lần, CỐ Ý KHÔNG khớp với lịch collect (1 lần/ngày lúc 21:00).
    # Giám sát phải chạy dày hơn thứ nó giám sát thì mới phát hiện kịp.
    schedule="0 */6 * * *",
    catchup=False,
    max_active_runs=1,
    dagrun_timeout=timedelta(minutes=15),
    tags=["youtube", "quality", "monitoring"],
) as dag:

    soda_scan = BashOperator(
        task_id="soda_scan_published",
        bash_command=SODA_CMD,
        doc_md=(
            "Quét `yti` (vùng đã công bố) bằng Soda.\n\n"
            "**Check quan trọng nhất: `freshness(observed_at) < 30h`** — "
            "đây là thứ quality gate KHÔNG THỂ phát hiện, vì gate chỉ soi từng "
            "batch; không có batch nào thì gate không chạy."
        ),
    )
