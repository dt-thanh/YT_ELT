"""
BIÊN GIỚI LOAD - nơi DUY NHẤT được phép ép kiểu dữ liệu thô thành kiểu có nghĩa.

Toàn bộ file này là HÀM THUẦN: vào gì ra nấy, không đụng mạng, không đụng
database, không đọc biến môi trường. Nhờ vậy:
  - test bằng pytest thường, chạy trong mili giây, không cần dựng hạ tầng
  - tái hiện bug bằng đúng một dòng gọi hàm
  - đây là nơi ĐÁNG viết test nhất của cả project

NGUYÊN TẮC XUYÊN SUỐT: KHÔNG BAO GIỜ BỊA DỮ LIỆU.
Không parse được -> trả None. None nghĩa là "KHÔNG BIẾT".
KHÔNG được biến "không biết" thành 0, thành chuỗi rỗng, hay thành giá trị mặc định.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# NGƯỠNG SHORTS - một hằng số có ngày hết hạn
#
# YouTube nâng giới hạn Shorts từ 60s lên 180s (3 phút) từ tháng 10/2024.
# Code cũ dùng 60 -> phân loại sai mọi Shorts dài 61-180 giây.
# Đã bắt gặp thật: video Wolfoo có '#shorts' trong tiêu đề nhưng dài PT1M8S (68s).
#
# ⚠️ BÀI HỌC: quy tắc nghiệp vụ dựa trên chính sách bên thứ ba SẼ HẾT HẠN.
# Nó không báo lỗi, không gãy - chỉ âm thầm phân loại sai từ ngày nền tảng đổi luật.
# Vì thế: (1) đặt thành hằng số có tên, có ghi nguồn và ngày
#         (2) luôn lưu format_evidence bên cạnh format_label
# ---------------------------------------------------------------------------
SHORTS_MAX_SECONDS = 180   # nguồn: YouTube Shorts, cập nhật 10/2024

# ISO-8601 duration: PnDTnHnMnS  ví dụ PT12M38S, P1DT2H3M4S, PT25H, P0D
# Regex CÓ NEO ^...$ để chuỗi rác không khớp một phần rồi lọt qua.
_ISO_DURATION = re.compile(
    r"^P"
    r"(?:(?P<days>\d+)D)?"
    r"(?:T"
    r"(?:(?P<hours>\d+)H)?"
    r"(?:(?P<minutes>\d+)M)?"
    r"(?:(?P<seconds>\d+)S)?"
    r")?$"
)


@dataclass(frozen=True)
class NormalizedVideo:
    """Một video đã được ép kiểu, sẵn sàng vào database.

    So với dict thô: mọi trường đã ĐÚNG KIỂU, và những trường không xác định
    được là None chứ không phải giá trị bịa.
    """
    video_id: str
    channel_id: str | None
    title: str | None
    description: str | None
    published_at: datetime | None
    duration_seconds: int | None
    thumbnail_url: str | None
    category_id: str | None
    default_language: str | None
    live_state: str | None
    made_for_kids: bool | None
    format_label: str
    format_evidence: str
    view_count: int | None
    like_count: int | None
    comment_count: int | None
    # Vì sao giữ lại chuỗi gốc? Để staging lưu được giá trị KHÔNG parse nổi.
    # Nếu chỉ giữ kết quả parse, gặp giá trị lạ ta mất luôn bằng chứng để điều tra.
    raw_view_count: str | None
    raw_like_count: str | None
    raw_comment_count: str | None
    raw_duration: str | None
    raw_published_at: str | None

    # Hai trường này KHÔNG đến từ parse dữ liệu video, mà từ NGỮ CẢNH THU THẬP:
    #   observed_at       - thời điểm THỰC SỰ gọi API lấy con số này
    #   source_object_key - file raw trên MinIO đã sinh ra dòng này (data lineage)
    # Có default None vì normalize_video() (đường từ raw JSON) chưa biết chúng;
    # from_staging_row() (đường từ staging) thì điền đầy đủ.
    observed_at: datetime | None = None
    source_object_key: str | None = None


# =============================================================================
# CÁC HÀM ÉP KIỂU NGUYÊN TỬ
# =============================================================================

def to_bigint(value) -> int | None:
    """Ép sang int. KHÔNG parse được -> None, TUYỆT ĐỐI KHÔNG trả 0.

    Vì sao quan trọng đến thế? Đã chứng minh bằng số ở Bước 3:
        2 bản ghi, 1 có like=50, 1 thiếu like
        NULL đúng cách -> AVG = 50   (SQL tự bỏ qua NULL)
        ép NULL -> 0    -> AVG = 25   SAI MỘT NỬA
    "Không biết" và "bằng 0" là hai sự thật khác nhau về thế giới.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        # bool là lớp con của int trong Python! int(True) == 1.
        # Không chặn thì True lặng lẽ thành 1 - một bug rất khó nhìn ra.
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def parse_iso8601_duration(value: str | None) -> int | None:
    """'PT12M38S' -> 758 giây. Trả None nếu không parse được.

    ⭐ PHẢI CỘNG CẢ PHẦN NGÀY. Đây là bug đã chứng minh ở repo cũ:
       'P1DT2H3M4S' -> parser cũ cho '02:03:04', MẤT TRẮNG 1 ngày.
       Nguyên nhân sâu xa: dùng kiểu TIME (thời điểm trong ngày) để biểu diễn
       DURATION (khoảng thời gian). Hai khái niệm khác nhau.

    'P0D' -> 0 giây. Đây là giá trị YouTube trả cho livestream đang/sắp phát:
    nghĩa là "CHƯA XÁC ĐỊNH", không phải "dài 0 giây". Hàm này trả đúng 0;
    việc diễn giải 0 là của classify_format().
    """
    if not value:
        return None

    match = _ISO_DURATION.match(value.strip())
    if not match:
        return None

    parts = match.groupdict()

    # ⚠️ Khớp regex là CHƯA ĐỦ. Mọi nhóm đều optional nên 'P' và 'PT' cũng khớp,
    # rồi cộng lại ra 0 giây - tức BỊA ra giá trị 0 từ một chuỗi rác.
    # Phải có ÍT NHẤT MỘT thành phần thì mới là duration hợp lệ.
    # (Ca này do test ca biên phát hiện, không phải do đọc lại code.)
    if all(v is None for v in parts.values()):
        return None

    try:
        days = int(parts["days"] or 0)
        hours = int(parts["hours"] or 0)
        minutes = int(parts["minutes"] or 0)
        seconds = int(parts["seconds"] or 0)
    except (TypeError, ValueError):
        return None

    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def parse_timestamp(value: str | None) -> datetime | None:
    """RFC-3339 '2015-03-01T10:00:00Z' -> datetime CÓ múi giờ (UTC).

    Chữ 'Z' = Zulu = UTC. datetime.fromisoformat() của Python < 3.11 KHÔNG
    hiểu 'Z' và sẽ ném ValueError -> đổi thành '+00:00' cho tương thích.

    Luôn trả datetime AWARE (có tzinfo). Trộn aware và naive là nguồn lỗi thời
    gian phổ biến nhất trong Python, và Postgres TIMESTAMPTZ cần aware.
    """
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    # Chuỗi không có múi giờ -> giả định UTC (YouTube luôn trả UTC).
    # Giả định này phải GHI RA, không được ngầm hiểu.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_bool_or_none(value) -> bool | None:
    """BA TRẠNG THÁI: True / False / None(không biết).

    status.madeForKids vắng mặt -> None. KHÔNG ép thành False:
    "không biết có phải nội dung trẻ em không" khác hoàn toàn
    "chắc chắn không phải nội dung trẻ em". Nhầm hai cái này có hệ quả pháp lý.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "1", "yes"):
        return True
    if text in ("false", "0", "no"):
        return False
    return None


# =============================================================================
# PHÂN LOẠI ĐỊNH DẠNG - trả về CẢ KẾT LUẬN LẪN BẰNG CHỨNG
# =============================================================================

def classify_format(duration_seconds: int | None, live_state: str | None) -> tuple[str, str]:
    """Trả (format_label, format_evidence).

    ⭐ THIẾT KẾ QUAN TRỌNG: KHÔNG kết luận 'shorts' chỉ từ thời lượng.
    Một video dài 2 phút CÓ THỂ là Shorts, mà cũng có thể là video thường ngắn.
    Từ metadata công khai, YouTube KHÔNG cho ta biết chắc.

    Nên ta chỉ khẳng định điều CHẮC CHẮN ĐÚNG:
        dài hơn 180s  -> CHẮC CHẮN không phải Shorts  -> 'normal'
        <= 180s       -> CÓ THỂ là Shorts, KHÔNG CHẮC -> 'unknown'

    Nghe có vẻ "yếu" hơn việc gán đại nhãn 'shorts', nhưng đây mới là TRUNG THỰC.
    Gán nhãn sai rồi đem đi so sánh hiệu suất Shorts vs video thường sẽ cho ra
    kết luận sai - và không ai phát hiện, vì dữ liệu trông vẫn "đầy đủ".

    format_evidence lưu LÝ DO. Sáu tháng sau nhìn nhãn lạ, ta biết nó được gán
    theo luật nào và luật đó còn đúng không. Đây là explainability ở tầng dữ liệu.
    """
    if live_state in ("live", "upcoming"):
        return "live", f"live_state={live_state}"

    if duration_seconds is None:
        return "unknown", "duration không parse được"

    if duration_seconds == 0:
        # YouTube trả P0D cho livestream chưa xác định độ dài.
        return "unknown", "duration=0 (livestream đang/sắp phát, độ dài chưa xác định)"

    if duration_seconds > SHORTS_MAX_SECONDS:
        return "normal", (
            f"duration={duration_seconds}s > ngưỡng Shorts {SHORTS_MAX_SECONDS}s "
            "-> chắc chắn không phải Shorts"
        )

    # <= 180s: ĐỦ ĐIỀU KIỆN là Shorts nhưng KHÔNG có bằng chứng xác nhận.
    return "unknown", (
        f"duration={duration_seconds}s <= {SHORTS_MAX_SECONDS}s -> đủ điều kiện Shorts "
        "nhưng metadata công khai không xác nhận được; chưa gán nhãn shorts"
    )


# =============================================================================
# GHÉP TẤT CẢ
# =============================================================================

def normalize_video(raw: dict) -> NormalizedVideo:
    """Một bản ghi thô từ raw JSON -> NormalizedVideo đã ép kiểu.

    Hàm này KHÔNG ném ngoại lệ khi gặp dữ liệu lạ. Nó trả None cho trường hỏng
    và GIỮ chuỗi gốc trong các trường raw_*.

    Vì sao không ném lỗi? Vì đây là đường vào STAGING, không phải vùng công bố.
    Nguyên tắc: dữ liệu bẩn PHẢI VÀO ĐƯỢC staging thì mới điều tra được nó bẩn
    ở đâu. Chặn ngay lúc parse = mất luôn bằng chứng. Việc CHẶN là của quality
    gate ở Bước 7, trên đường từ staging sang published.
    """
    duration_seconds = parse_iso8601_duration(raw.get("duration"))
    live_state = raw.get("liveBroadcastContent")
    label, evidence = classify_format(duration_seconds, live_state)

    return NormalizedVideo(
        video_id=raw.get("video_id"),
        channel_id=raw.get("channel_id"),
        title=raw.get("title"),
        description=raw.get("description"),
        published_at=parse_timestamp(raw.get("publishedAt")),
        duration_seconds=duration_seconds,
        thumbnail_url=raw.get("thumbnail_url"),
        category_id=raw.get("categoryId"),
        default_language=raw.get("defaultLanguage"),
        live_state=live_state if live_state in ("none", "live", "upcoming") else None,
        made_for_kids=to_bool_or_none(raw.get("madeForKids")),
        format_label=label,
        format_evidence=evidence,
        view_count=to_bigint(raw.get("viewCount")),
        like_count=to_bigint(raw.get("likeCount")),
        comment_count=to_bigint(raw.get("commentCount")),
        raw_view_count=_as_text(raw.get("viewCount")),
        raw_like_count=_as_text(raw.get("likeCount")),
        raw_comment_count=_as_text(raw.get("commentCount")),
        raw_duration=_as_text(raw.get("duration")),
        raw_published_at=_as_text(raw.get("publishedAt")),
    )


def from_staging_row(row: dict) -> NormalizedVideo:
    """Một dòng staging (cột raw_* dạng TEXT) -> NormalizedVideo đã ép kiểu.

    ⭐ VÌ SAO CẦN HÀM NÀY, TRONG KHI ĐÃ CÓ normalize_video()?
    Vì có HAI đường vào khác nhau:
        normalize_video()   : từ raw JSON (khóa kiểu "viewCount")  -> nạp staging
        from_staging_row()  : từ dòng staging (cột "raw_view_count") -> publish
    Hai đường, nhưng DÙNG CHUNG đúng các hàm parse bên trên.

    Đây là điểm mấu chốt: LOGIC PARSE CHỈ TỒN TẠI MỘT CHỖ.
    Nếu publish tự viết lại cách parse duration, ta sẽ có hai cách parse khác
    nhau cho cùng một dữ liệu -> replay từ staging cho kết quả khác replay từ
    raw -> loại bug không thể tin nổi và cực khó truy.
    """
    duration_seconds = parse_iso8601_duration(row.get("raw_duration"))
    live_state = row.get("live_broadcast")
    label, evidence = classify_format(duration_seconds, live_state)

    return NormalizedVideo(
        video_id=row.get("video_id"),
        channel_id=row.get("channel_id"),
        title=row.get("title"),
        description=row.get("raw_description"),
        published_at=parse_timestamp(row.get("raw_published_at")),
        duration_seconds=duration_seconds,
        thumbnail_url=row.get("raw_thumbnail_url"),
        category_id=row.get("raw_category_id"),
        default_language=row.get("raw_default_language"),
        live_state=live_state if live_state in ("none", "live", "upcoming") else None,
        made_for_kids=to_bool_or_none(row.get("raw_made_for_kids")),
        format_label=label,
        format_evidence=evidence,
        view_count=to_bigint(row.get("raw_view_count")),
        like_count=to_bigint(row.get("raw_like_count")),
        comment_count=to_bigint(row.get("raw_comment_count")),
        raw_view_count=_as_text(row.get("raw_view_count")),
        raw_like_count=_as_text(row.get("raw_like_count")),
        raw_comment_count=_as_text(row.get("raw_comment_count")),
        raw_duration=_as_text(row.get("raw_duration")),
        raw_published_at=_as_text(row.get("raw_published_at")),
        observed_at=row.get("observed_at"),
        source_object_key=row.get("source_object_key"),
    )


def _as_text(value) -> str | None:
    """Giữ nguyên dạng chuỗi để staging lưu được cả giá trị không parse nổi."""
    return None if value is None else str(value)
