"""
DAG thu thập YouTube - WRAPPER MỎNG quanh CLI.

⭐ TRIẾT LÝ: AIRFLOW LÀ BỘ HẸN GIỜ, KHÔNG PHẢI NƠI CHỨA NGHIỆP VỤ.
File này KHÔNG có một dòng logic nghiệp vụ nào. Nó chỉ làm 4 việc:
    1. Đến giờ thì gọi   (schedule)
    2. Lỗi thì thử lại   (retries)
    3. Ghi log           (UI)
    4. Truyền NGÀY của lần chạy xuống CLI  (logical date -> scheduled_for)

Toàn bộ nghiệp vụ nằm trong src/youtube_intel/. Nhờ vậy:
    - chạy tay được:  python -m youtube_intel.cli collect
    - test được không cần Airflow
    - đổi sang Dagster/Prefect/cron chỉ sửa file này

SO VỚI 3 DAG CŨ (produce_json -> update_db -> data_quality):
    - Bỏ hẳn TriggerDagRunOperator. Ba DAG nối nhau bằng "trigger" KHÔNG truyền
      được ngữ cảnh (DAG sau không biết đang xử lý lần thu nào -> phải đoán bằng
      date.today()) và KHÔNG có transaction xuyên DAG (dữ liệu bẩn đã publish
      rồi mới bị phát hiện).
    - Giờ extract + load + gate + publish nằm trong MỘT transaction Python,
      nên không cần trigger. Gate chạy TRƯỚC publish.
    - Soda đổi vai: từ "kiểm tra sau publish" thành CAMERA giám sát toàn bảng,
      chạy ở DAG riêng sau khi dữ liệu đã vào (Bước 10).
"""

from __future__ import annotations

from datetime import timedelta

import pendulum
from airflow import DAG
from airflow.operators.bash import BashOperator
from airflow.operators.empty import EmptyOperator

from airflow.datasets import Dataset

from youtube_intel.config import load_config
from youtube_intel.pipeline import OBSERVATIONS_DATASET_URI

# =============================================================================
# ĐỌC DANH SÁCH KÊNH TỪ CONFIG
#
# ⚠️ Đây là I/O ở CẤP MODULE - chạy mỗi lần scheduler parse file (~30 giây/lần).
# Quy tắc "không I/O lúc import DAG" chủ yếu nhắm vào MẠNG và DATABASE.
# Đọc một file YAML 2KB trên đĩa cục bộ thì rẻ và chấp nhận được.
#
# Đổi lại ta được điều rất hay: THÊM KÊNH VÀO YAML -> TASK MỚI TỰ HIỆN RA TRONG UI,
# không cần sửa DAG, không cần deploy.
#
# Khi nào cách này hết dùng được? Khi danh sách nguồn lên hàng nghìn và nằm trong
# DATABASE (metadata-driven pipeline). Lúc đó phải dùng DYNAMIC TASK MAPPING
# (.expand()) để đọc danh sách lúc CHẠY thay vì lúc parse.
# =============================================================================
# ⭐ DÙNG CHÍNH load_config() CỦA PACKAGE, không tự đọc YAML lần nữa.
# Nếu DAG tự parse YAML, ta sẽ có HAI cách đọc cùng một file -> hai cách hiểu
# khác nhau khi config đổi. Dùng chung hàm = dùng chung cả phần VALIDATE
# (channel id sai định dạng, group sai tên... bị bắt ngay lúc parse DAG).
#
# require=() vì lúc parse DAG ta CHƯA CẦN secret - chỉ cần biết có kênh nào và
# lịch ra sao. Đòi secret ở đây sẽ làm DAG không import được trên máy chưa
# cấu hình đủ (least privilege cho cấu hình - bài học Bước 8).
CFG = load_config(require=())
CHANNELS = [
    {"id": c.channel_id, "title": c.title, "group": c.group}
    for c in CFG.enabled_channels()
]

# Tên task chỉ được chứa chữ, số, dấu chấm, gạch ngang, gạch dưới.
# Tên kênh có dấu tiếng Việt và emoji -> phải làm sạch.
def _task_name(channel: dict) -> str:
    raw = (channel.get("title") or channel["id"])
    safe = "".join(ch if ch.isascii() and (ch.isalnum() or ch in "-_") else "_" for ch in raw)
    return f"collect_{safe.strip('_')[:40]}"


# Múi giờ LẤY TỪ CONFIG, không viết cứng -> đổi report_timezone là DAG đổi theo.
local_tz = pendulum.timezone(CFG.settings.report_timezone)

default_args = {
    "owner": "dataengineers",
    "depends_on_past": False,          # lần chạy này KHÔNG chờ lần trước thành công
    "email_on_failure": False,         # TODO Bước 12: thay bằng cảnh báo Slack
    # Retry ở TẦNG NGOÀI CÙNG. Bên trong đã có 2 tầng retry:
    #   tenacity (1 request HTTP)  ->  không cứu được thì
    #   Airflow (cả task)          ->  chạy lại toàn bộ kênh đó
    # An toàn vì CLI đã idempotent: chạy lại cùng collection_id không sinh dòng trùng.
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
    "retry_exponential_backoff": True,
}

# ---------------------------------------------------------------------------
# ⭐ collection_id TẤT ĐỊNH TỪ LOGICAL DATE
#
# Bài toán: 4 task chạy SONG SONG, mỗi task một tiến trình riêng. Chúng phải
# dùng CHUNG một collection_id - nếu mỗi task tự sinh uuid4 thì 4 kênh thành 4
# lần thu khác nhau, phá vỡ grain "một collection = một lần thu cho mọi kênh".
#
# Lời giải: BĂM logical date thành UUID. Cả 4 task tính ra CÙNG một giá trị mà
# không cần nói chuyện với nhau. Và retry/backfill cũng ra ĐÚNG giá trị đó
# -> idempotent. Không cần XCom, không cần task "khởi tạo" đi trước.
#
# {{ data_interval_start }} là LOGICAL DATE của Airflow: ô lịch mà lần chạy này
# đại diện. KHÁC với thời điểm thực sự chạy. Chính nhờ nó mà backfill hoạt động:
# chạy lại cho ô 2026-09-20T00:00 sẽ dùng đúng collection_id của ô đó.
# ---------------------------------------------------------------------------
COLLECTION_ID_EXPR = (
    "{{ macros.uuid.uuid5(macros.uuid.NAMESPACE_URL, "
    "'yt-collection/' ~ data_interval_start.isoformat()) }}"
)

CLI = "cd /opt/airflow && python -m youtube_intel.cli"

with DAG(
    dag_id="yt_collect",
    description="Thu thập số liệu kênh YouTube -> MinIO + Postgres (lịch lấy từ config/channels.yaml)",
    default_args=default_args,
    start_date=pendulum.datetime(2026, 9, 23, tz=local_tz),
    # ⭐ LỊCH SUY RA TỪ CONFIG, không viết cứng ở đây.
    # channels.yaml: snapshot_hours=24, snapshot_anchor_hour=21 -> "0 21 * * *"
    # Một khái niệm, MỘT nơi định nghĩa. Đổi giờ chạy = sửa YAML, không sửa DAG.
    # (Trả nợ kỹ thuật "lịch khai ở hai nơi" ghi từ Bước 9.)
    schedule=CFG.settings.cron_expression(),
    catchup=False,          # KHÔNG chạy bù các ô lịch quá khứ.
                            # Vì sao? YouTube KHÔNG trả số liệu quá khứ - chạy bù
                            # chỉ lấy được số liệu HÔM NAY rồi gắn nhãn ngày cũ.
                            # Đó là BỊA DỮ LIỆU. Backfill ở đây chỉ có nghĩa với
                            # `replay` (nạp lại từ raw đã có).
    max_active_runs=1,      # cấm 2 lần chạy chồng nhau
    dagrun_timeout=timedelta(hours=1),
    tags=["youtube", "elt", "collect"],
) as dag:

    start = EmptyOperator(task_id="start")
    # outlets: task này THÔNG BÁO "tôi vừa cập nhật tập dữ liệu X".
    # DAG nào lập lịch theo X (yt_report) sẽ tự chạy. yt_collect KHÔNG cần biết
    # ai đang nghe - khác hẳn TriggerDagRunOperator của 3 DAG cũ (phải gọi đích
    # danh DAG kia -> trói chặt hai DAG vào nhau).
    done = EmptyOperator(task_id="done",
                         outlets=[Dataset(OBSERVATIONS_DATASET_URI)])

    collect_tasks = []
    for channel in CHANNELS:
        collect_tasks.append(
            BashOperator(
                task_id=_task_name(channel),
                # Vì sao BashOperator chứ không PythonOperator?
                #   - Chạy CLI trong TIẾN TRÌNH RIÊNG: code nghiệp vụ crash cũng
                #     không kéo theo scheduler.
                #   - MÃ THOÁT của CLI thành trạng thái task một cách tự nhiên
                #     (0 = success, khác 0 = failed) - đúng giao diện ta thiết kế ở Bước 8.
                #   - Lệnh in ra trong log là lệnh BẠN GÕ TAY ĐƯỢC để tái hiện.
                #     Đây là giá trị vận hành rất lớn khi debug lúc 2 giờ sáng.
                bash_command=(
                    f"{CLI} collect "
                    f"--channel {channel['id']} "
                    f"--collection-id {COLLECTION_ID_EXPR} "
                    # scheduled_for = logical date, KHÔNG phải now().
                    # Đây là thứ làm cho chạy lại/backfill sinh ra cùng một ô lịch.
                    "--scheduled-for {{ data_interval_start.isoformat() }}"
                ),
                # Mỗi kênh một task -> lỗi ở kênh này KHÔNG chặn kênh khác
                # (bulkhead pattern), và retry được RIÊNG kênh lỗi thay vì gọi
                # lại cả 4 kênh -> tiết kiệm quota.
                doc_md=f"Thu thập kênh **{channel.get('title')}** (`{channel['id']}`).",
            )
        )

    # ALL_DONE chứ không phải mặc định ALL_SUCCESS:
    # 3/4 kênh xong thì vẫn đi tiếp tới `done`. Một kênh lỗi KHÔNG được làm
    # cả run treo - đúng tinh thần trạng thái 'partial' ở Bước 8.
    done.trigger_rule = "all_done"

    start >> collect_tasks >> done
