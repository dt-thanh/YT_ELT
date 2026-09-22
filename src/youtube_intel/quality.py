"""
QUALITY GATE - cửa chặn dữ liệu bẩn TRƯỚC khi vào vùng công bố.

VỊ TRÍ TRONG LUỒNG:
    staging (có thể bẩn)  ->  [GATE]  ->  published (luôn sạch)
                                 |
                                 +-> quarantine (giữ lại để điều tra)

VÌ SAO CHẶN TRƯỚC CHỨ KHÔNG DỌN SAU?
Đổ vào bảng chính rồi mới kiểm tra thì trong khoảng thời gian đó dashboard ĐÃ
đọc thấy dữ liệu sai, và ai đó ĐÃ ra quyết định dựa trên nó. Chặn trước = vùng
công bố không bao giờ có một khoảnh khắc nào bẩn.
Ví von: KHÓA CỬA, không phải dọn dẹp sau khi mất trộm.

HAI MỨC NGHIÊM TRỌNG:
    ERROR   -> dữ liệu KHÔNG THỂ đúng   -> chặn cả batch, quarantine
    WARNING -> có thể đúng, chỉ thiếu/lạ -> cho qua nhưng GHI LẠI

Vì sao phải chia? Nếu coi "thiếu likeCount" là ERROR thì mọi batch đều bị chặn
(đã đo: 7/1000 video MrBeast thiếu like) -> pipeline đỏ mỗi ngày -> người ta tắt
cảnh báo -> cảnh báo mất tác dụng. Thuật ngữ: ALERT FATIGUE.

FILE NÀY LÀ HÀM THUẦN: nhận list[dict] (các dòng staging), trả về báo cáo.
KHÔNG đụng database, KHÔNG đụng mạng -> test được bằng dict viết tay.
Việc ĐỌC dòng từ staging là của repository.py; việc ĐÁNH GIÁ là của file này.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from youtube_intel.normalize import parse_iso8601_duration, parse_timestamp, to_bigint

ERROR = "error"
WARNING = "warning"

# Ngưỡng chênh lệch expected vs received còn chấp nhận được.
# Giống MISSING_TOLERANCE ở tầng API: nguồn thật luôn lệch nhẹ (video bị xóa
# giữa hai bước gọi). So bằng tuyệt đối -> đỏ mỗi ngày -> vô dụng.
RECEIVED_TOLERANCE = 0.05

# Số dòng vi phạm được lưu làm mẫu trong báo cáo. Không lưu hết:
# một batch lỗi hệ thống có thể có 200 dòng sai, nhồi hết vào log/JSONB là vô ích.
# Vài mẫu là đủ để CHẨN ĐOÁN; muốn xem hết thì truy vấn staging.
MAX_SAMPLES = 5


@dataclass(frozen=True)
class Issue:
    """Một vấn đề phát hiện được.

    `code` là mã MÁY ĐỌC ĐƯỢC (snake_case, ổn định) - dùng để đếm, thống kê,
    đặt alert. `message` là câu cho NGƯỜI đọc.
    Đừng bao giờ bắt hệ thống khác phải parse chuỗi message để biết lỗi gì.
    """
    severity: str
    code: str
    message: str
    count: int
    samples: tuple = field(default_factory=tuple)


@dataclass(frozen=True)
class QualityReport:
    batch_id: str
    checked_rows: int
    issues: tuple[Issue, ...]

    @property
    def errors(self) -> tuple[Issue, ...]:
        return tuple(i for i in self.issues if i.severity == ERROR)

    @property
    def warnings(self) -> tuple[Issue, ...]:
        return tuple(i for i in self.issues if i.severity == WARNING)

    @property
    def passed(self) -> bool:
        """CHỈ error mới chặn. Warning không chặn."""
        return not self.errors

    @property
    def quality_status(self) -> str:
        """Giá trị ghi vào channel_batches.quality_status."""
        return "passed" if self.passed else "quarantined"

    def to_dict(self) -> dict:
        """Dạng JSON để lưu vào database / log có cấu trúc."""
        return {
            "batch_id": self.batch_id,
            "checked_rows": self.checked_rows,
            "status": self.quality_status,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "issues": [
                {"severity": i.severity, "code": i.code, "message": i.message,
                 "count": i.count, "samples": list(i.samples)}
                for i in self.issues
            ],
        }

    def summary(self) -> str:
        if self.passed and not self.warnings:
            return f"PASSED - {self.checked_rows} dòng, không vấn đề gì."
        parts = [f"{self.quality_status.upper()} - {self.checked_rows} dòng"]
        for i in self.issues:
            parts.append(f"  [{i.severity:<7}] {i.code} ({i.count}): {i.message}")
        return "\n".join(parts)


# =============================================================================
# GATE
# =============================================================================

def check_batch(
    *,
    batch_id: str,
    rows: list[dict],
    expected_count: int | None = None,
    discovery_truncated: bool = False,
) -> QualityReport:
    """Kiểm tra toàn bộ dòng staging của MỘT batch.

    rows: list[dict] đọc từ yti_staging.video_observations_stg
          (repository.fetch_staging_rows trả về đúng dạng này)
    """
    issues: list[Issue] = []

    # ---- ERROR 1: batch rỗng -------------------------------------------
    # Không có dòng nào mà vẫn publish "thành công" là nói dối trắng trợn.
    if not rows:
        issues.append(Issue(ERROR, "empty_batch",
                            "Batch không có dòng nào để publish.", 0))
        return QualityReport(batch_id=batch_id, checked_rows=0, issues=tuple(issues))

    # ---- ERROR 2: thiếu video_id ---------------------------------------
    # video_id là KHÓA. Thiếu khóa thì dòng đó không thể định danh, không thể
    # upsert, không thể nối với lần quan sát trước. Vô dụng và gây trùng lặp.
    missing_id = [r for r in rows if not (r.get("video_id") or "").strip()]
    if missing_id:
        issues.append(Issue(ERROR, "missing_video_id",
                            "Có dòng không có video_id.", len(missing_id),
                            _samples(missing_id, "stg_id")))

    # ---- ERROR 3: trùng video_id trong cùng batch -----------------------
    # Đây là vi phạm GRAIN. Bảng published có UNIQUE(video_id, collection_id),
    # nên nếu để lọt, câu INSERT sẽ gãy giữa transaction. Bắt ở đây thì ta
    # QUARANTINE có kiểm soát thay vì để database ném lỗi bất ngờ.
    counts = Counter((r.get("video_id") or "") for r in rows)
    dupes = {vid: n for vid, n in counts.items() if vid and n > 1}
    if dupes:
        issues.append(Issue(ERROR, "duplicate_video_id",
                            f"video_id bị lặp trong batch: {list(dupes)[:MAX_SAMPLES]}",
                            sum(dupes.values()), tuple(list(dupes)[:MAX_SAMPLES])))

    # ---- ERROR 4: số đếm CÓ MẶT nhưng KHÔNG parse được -----------------
    # ⭐ PHÂN BIỆT HAI THỨ HOÀN TOÀN KHÁC NHAU:
    #   raw = None      -> YouTube không trả về  -> BÌNH THƯỜNG (warning)
    #   raw = "1,234"   -> có giá trị mà không đọc nổi -> ERROR
    # Trường hợp 2 nghĩa là YouTube ĐÃ ĐỔI ĐỊNH DẠNG, hoặc ta đọc sai file.
    # Cho qua là âm thầm mất dữ liệu; chặn lại là buộc con người xem xét.
    for col, label in (("raw_view_count", "view"), ("raw_like_count", "like"),
                       ("raw_comment_count", "comment")):
        bad = [r for r in rows
               if r.get(col) is not None and to_bigint(r.get(col)) is None]
        if bad:
            issues.append(Issue(ERROR, f"unparsable_{label}_count",
                                f"{label}Count có giá trị nhưng không đổi được thành số "
                                f"(nghi ngờ YouTube đổi định dạng).",
                                len(bad), _samples(bad, col)))

    # ---- ERROR 5: duration có mặt nhưng không parse được ---------------
    bad_dur = [r for r in rows
               if r.get("raw_duration") is not None
               and parse_iso8601_duration(r.get("raw_duration")) is None]
    if bad_dur:
        issues.append(Issue(ERROR, "unparsable_duration",
                            "duration có giá trị nhưng không parse được ISO-8601.",
                            len(bad_dur), _samples(bad_dur, "raw_duration")))

    # ---- ERROR 6: published_at không parse được ------------------------
    # published_at là NOT NULL ở bảng published -> không parse được là chặn.
    bad_pub = [r for r in rows
               if r.get("raw_published_at") is not None
               and parse_timestamp(r.get("raw_published_at")) is None]
    if bad_pub:
        issues.append(Issue(ERROR, "unparsable_published_at",
                            "publishedAt không parse được RFC-3339.",
                            len(bad_pub), _samples(bad_pub, "raw_published_at")))

    # ---- ERROR 7: số âm ------------------------------------------------
    neg = [r for r in rows
           for col in ("raw_view_count", "raw_like_count", "raw_comment_count")
           if (v := to_bigint(r.get(col))) is not None and v < 0]
    if neg:
        issues.append(Issue(ERROR, "negative_count",
                            "Số đếm âm - không thể đúng.", len(neg),
                            _samples(neg, "video_id")))

    # ---- ERROR 8: like/comment > view (BẤT KHẢ về mặt logic) -----------
    # Muốn like một video thì phải xem nó -> like không thể nhiều hơn view.
    # So sánh BỎ QUA dòng có NULL: NULL nghĩa là "không biết", và
    # "không biết > 100" là UNKNOWN chứ không phải TRUE.
    impossible = []
    for r in rows:
        views = to_bigint(r.get("raw_view_count"))
        if views is None:
            continue
        for col in ("raw_like_count", "raw_comment_count"):
            val = to_bigint(r.get(col))
            if val is not None and val > views:
                impossible.append(r)
                break
    if impossible:
        issues.append(Issue(ERROR, "engagement_exceeds_views",
                            "like hoặc comment nhiều hơn view - bất khả về logic.",
                            len(impossible), _samples(impossible, "video_id")))

    # ---- ERROR 9: nhận ít hơn kỳ vọng quá ngưỡng -----------------------
    # ⚠️ CHỈ kiểm tra khi KHÔNG chủ động cắt. Nếu discovery_truncated=True thì
    # "thiếu" là ĐÚNG Ý (ta cap 50 video), không phải lỗi -> kiểm tra ở đây sẽ
    # là báo động sai.
    if expected_count and not discovery_truncated:
        shortfall = expected_count - len(rows)
        if shortfall > 0:
            ratio = shortfall / expected_count
            sev = ERROR if ratio > RECEIVED_TOLERANCE else WARNING
            issues.append(Issue(sev, "received_below_expected",
                                f"Nhận {len(rows)}/{expected_count} dòng "
                                f"(thiếu {ratio:.1%}, ngưỡng {RECEIVED_TOLERANCE:.0%}).",
                                shortfall))

    # =====================================================================
    # WARNING - cho qua nhưng phải ghi lại
    # =====================================================================

    # Thiếu like/comment là BÌNH THƯỜNG: chủ kênh ẩn like, hoặc video
    # Made for Kids bị YouTube TẮT BÌNH LUẬN BẮT BUỘC.
    # Tuyệt đối KHÔNG coi đây là lỗi, và KHÔNG suy ra "kênh kém tương tác".
    for col, label in (("raw_like_count", "like"), ("raw_comment_count", "comment")):
        n = sum(1 for r in rows if r.get(col) is None)
        if n:
            issues.append(Issue(WARNING, f"missing_{label}_count",
                                f"{n}/{len(rows)} dòng không có {label}Count "
                                f"(chủ kênh ẩn, hoặc Made for Kids tắt bình luận).", n))

    # viewCount thiếu thì đáng chú ý hơn nhiều: nó là chỉ số xếp hạng CHÍNH.
    # Vẫn là warning (không chặn) nhưng phải nổi bật trong báo cáo.
    n_no_view = sum(1 for r in rows if r.get("raw_view_count") is None)
    if n_no_view:
        issues.append(Issue(WARNING, "missing_view_count",
                            f"{n_no_view}/{len(rows)} dòng không có viewCount - "
                            "ảnh hưởng trực tiếp tới xếp hạng.", n_no_view))

    # Cắt ở page cap: không phải lỗi, nhưng PHẢI ghi ra để báo cáo không
    # vô tình khẳng định "đã phân tích toàn bộ kênh".
    if discovery_truncated:
        issues.append(Issue(WARNING, "discovery_truncated",
                            "Đã cắt ở giới hạn cấu hình - KHÔNG bao phủ hết kênh.", 1))

    return QualityReport(batch_id=batch_id, checked_rows=len(rows), issues=tuple(issues))


def _samples(rows: list[dict], key: str) -> tuple:
    """Lấy vài giá trị mẫu để chẩn đoán. Không lấy hết - xem MAX_SAMPLES."""
    out = []
    for r in rows[:MAX_SAMPLES]:
        out.append(r.get(key))
    return tuple(out)
