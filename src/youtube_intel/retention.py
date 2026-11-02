"""
RETENTION - vòng đời dữ liệu: cái gì được xóa, khi nào, và vì sao.

⚠️ ĐÂY LÀ MODULE NGUY HIỂM NHẤT CỦA CẢ PROJECT.
Mọi module khác sai thì sửa code rồi chạy lại. Module này sai thì DỮ LIỆU MẤT,
và với raw thì mất VĨNH VIỄN (YouTube không trả về số liệu quá khứ).

BỐN NGUYÊN TẮC, KHÔNG ĐƯỢC PHÁ:

1. MẶC ĐỊNH LÀ XEM TRƯỚC (dry-run).
   Mọi hàm ở đây tách làm hai: plan_*() chỉ ĐẾM và LIỆT KÊ, delete_*() mới xóa.
   CLI mặc định chỉ chạy plan_*; phải gõ thêm --apply mới xóa thật.
   Thao tác không hoàn tác được thì không bao giờ là hành vi mặc định.

2. KHÔNG XÓA THỨ KHÔNG TẠO LẠI ĐƯỢC.
   Thứ tự an toàn, từ dễ tới khó:
       staging  -> dựng lại từ raw bất cứ lúc nào   -> xóa thoải mái
       orphan   -> batch dở dang, vốn đã vô dụng    -> xóa sau thời gian ân hạn
       raw      -> KHÔNG TẠO LẠI ĐƯỢC              -> chỉ xóa theo chính sách đã xác minh

3. KHÔNG XÓA BẰNG CHỨNG.
   Batch quality_status='quarantined' phải GIỮ NGUYÊN staging của nó - đó là
   thứ duy nhất giúp hiểu vì sao dữ liệu bẩn. Xóa nó đi là tự bịt mắt mình.

4. CON SỐ CHÍNH SÁCH NẰM TRONG CONFIG, KHÔNG HARDCODE.
   Giới hạn trên (30 ngày, 36 tháng) đã đọc từ nguồn gốc và ghi ở
   docs/policy_notes.md; config.py từ chối số vượt giới hạn.

R3 (đặc tả mục 3, Developer Policies III.E.4) ĐÈ LÊN nguyên tắc 2 và 3: dữ liệu
API công khai - kể cả raw và staging của batch bị cách ly - phải xóa hoặc làm mới
sau tối đa 30 ngày, và xóa trong 7 ngày khi có yêu cầu. Mỗi bảng có fetched_at và
data_class (migration 008); R3_RULES dưới đây là một quy tắc cho mỗi lớp dữ liệu.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from youtube_intel.storage import MANIFEST_FILENAME, RawStore, utc_now

logger = logging.getLogger(__name__)

# Số mẫu in ra trong báo cáo. Không in hết: một lần dọn có thể đụng hàng nghìn
# object, nhồi hết vào log là vô ích. Vài mẫu đủ để người vận hành NHẬN RA
# mình sắp xóa đúng thứ mình nghĩ.
MAX_SAMPLES = 5


@dataclass
class CleanupPlan:
    """Kế hoạch dọn dẹp - CHƯA xóa gì cả.

    Tách 'kế hoạch' khỏi 'thực thi' là mẫu PLAN/APPLY, giống terraform plan
    rồi terraform apply. Người vận hành nhìn thấy TOÀN BỘ hậu quả trước khi
    chấp nhận. Với thao tác không hoàn tác được, đây là bắt buộc.
    """
    what: str
    count: int = 0
    bytes_freed: int = 0
    samples: list[str] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)
    note: str | None = None

    def describe(self) -> str:
        if self.note:
            return f"  {self.what:<28} {self.note}"
        size = f" ({_human(self.bytes_freed)})" if self.bytes_freed else ""
        line = f"  {self.what:<28} {self.count:>6} mục{size}"
        for s in self.samples[:MAX_SAMPLES]:
            line += f"\n      - {s}"
        if self.count > MAX_SAMPLES:
            line += f"\n      ... và {self.count - MAX_SAMPLES} mục nữa"
        return line


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# =============================================================================
# 1. STAGING - an toàn nhất, dọn trước
# =============================================================================

def plan_staging_cleanup(cur, *, days: int) -> CleanupPlan:
    """Đếm dòng staging đủ điều kiện xóa. KHÔNG xóa.

    ⭐ HAI ĐIỀU KIỆN, CẢ HAI ĐỀU BẮT BUỘC:
      (a) đã quá `days` ngày
      (b) batch đó có quality_status = 'passed'

    Điều kiện (b) là thứ quan trọng nhất: batch 'quarantined' hoặc 'pending'
    phải GIỮ NGUYÊN. Dữ liệu bẩn ở staging chính là BẰNG CHỨNG để điều tra -
    xóa nó là tự bịt mắt. Và 'pending' nghĩa là chưa ai kiểm tra, xóa còn tệ hơn.
    """
    cur.execute(
        """
        SELECT count(*) AS n,
               count(DISTINCT s.batch_id) AS batches,
               min(s.loaded_at) AS oldest
          FROM yti_staging.video_observations_stg s
          JOIN yti.channel_batches b USING (batch_id)
         WHERE s.loaded_at < now() - make_interval(days => %s)
           AND b.quality_status = 'passed';
        """,
        (days,),
    )
    row = cur.fetchone()
    n = row["n"] or 0

    plan = CleanupPlan(what="staging (đã publish)", count=n)
    if n:
        plan.samples = [
            f"{row['batches']} batch, dòng cũ nhất: {row['oldest']:%Y-%m-%d %H:%M}"
        ]
    else:
        plan.note = f"không có dòng nào cũ hơn {days} ngày thuộc batch đã publish"
    return plan


def delete_staging(cur, *, days: int) -> int:
    """XÓA THẬT dòng staging đã quá hạn. Trả về số dòng đã xóa.

    Dùng DELETE ... USING thay vì subquery IN (...): Postgres tối ưu tốt hơn
    và đọc dễ hơn khi cần lọc theo bảng khác.
    """
    cur.execute(
        """
        DELETE FROM yti_staging.video_observations_stg s
         USING yti.channel_batches b
         WHERE s.batch_id = b.batch_id
           AND s.loaded_at < now() - make_interval(days => %s)
           AND b.quality_status = 'passed';
        """,
        (days,),
    )
    n = cur.rowcount
    logger.info("Đã xóa %d dòng staging (cũ hơn %d ngày, batch đã publish)", n, days)
    return n


# =============================================================================
# 2. ORPHAN - batch dở dang trên MinIO
# =============================================================================

def plan_orphan_cleanup(store: RawStore, *, hours: int) -> CleanupPlan:
    """Tìm batch KHÔNG CÓ MANIFEST và đã quá thời gian ân hạn.

    ⭐ NHỚ LẠI BƯỚC 5: manifest là COMMIT MARKER.
    Thư mục batch không có `_manifest.json` = lần ghi đó CHẾT GIỮA CHỪNG.
    Dữ liệu trong đó không đầy đủ, replay bỏ qua nó, không ai đọc nó -> rác.

    VÌ SAO CẦN THỜI GIAN ÂN HẠN?
    Vì một batch ĐANG CHẠY cũng chưa có manifest. Xóa ngay là xóa nhầm dữ liệu
    của tiến trình đang làm việc. Ân hạn (mặc định 24h) đảm bảo mọi tiến trình
    hợp lệ đã kết thúc từ lâu.
    """
    # Hệ thống MỚI TINH (chưa `collect` lần nào) thì bucket chưa được tạo.
    # Trước đây trường hợp này làm lệnh gãy với NoSuchBucket - lỗi lộ ra khi
    # mô phỏng CI trên hệ thống sạch. Báo RÕ lý do thay vì gãy, và cũng thay vì
    # âm thầm coi như rỗng (gõ sai tên bucket cũng sẽ bị "coi như rỗng").
    if not store.bucket_exists():
        return CleanupPlan(what="raw orphan (thiếu manifest)",
                           note="chưa có bucket - hệ thống chưa thu thập lần nào, không có gì để dọn")

    cutoff = utc_now() - timedelta(hours=hours)
    objects = store.list_objects(store.prefix + "/")

    # Gom object theo thư mục batch (bỏ phần tên file cuối)
    by_batch: dict[str, list[dict]] = {}
    for obj in objects:
        prefix = obj["key"].rsplit("/", 1)[0]
        by_batch.setdefault(prefix, []).append(obj)

    plan = CleanupPlan(what="raw orphan (thiếu manifest)")
    for prefix, objs in sorted(by_batch.items()):
        has_manifest = any(o["key"].endswith(MANIFEST_FILENAME) for o in objs)
        if has_manifest:
            continue                                  # batch hoàn chỉnh -> giữ
        newest = max(o["last_modified"] for o in objs)
        if newest >= cutoff:
            continue                                  # còn trong ân hạn -> giữ

        plan.count += len(objs)
        plan.bytes_freed += sum(o["size"] for o in objs)
        plan.keys.extend(o["key"] for o in objs)
        if len(plan.samples) < MAX_SAMPLES:
            short = prefix.split("collection_id=")[-1]
            plan.samples.append(f"{short}  ({len(objs)} file, {newest:%Y-%m-%d %H:%M})")

    if not plan.count:
        plan.note = f"không có batch dở dang nào cũ hơn {hours} giờ"
    return plan


# =============================================================================
# 3. RAW - nguy hiểm nhất, chỉ xóa khi chính sách đã được xác minh
# =============================================================================

def plan_raw_cleanup(store: RawStore, *, days: int | None) -> CleanupPlan:
    """Tìm raw object quá hạn.

    ⚠️ days=None -> KHÔNG LÀM GÌ CẢ, và đó là mặc định có chủ đích.
    Chính sách YouTube về vòng đời dữ liệu API lưu trữ CHƯA được đọc và xác
    minh ([S7] Developer Policies). Brief đã chốt: không code theo con số chưa
    kiểm chứng. Raw là thứ DUY NHẤT không tạo lại được - thà giữ thừa còn hơn
    xóa nhầm.
    """
    plan = CleanupPlan(what="raw (theo chính sách)")
    if days is None:
        plan.note = ("TẮT - raw_retention_days=null (config.py hiện từ chối giá trị này "
                     "vì vi phạm R3).")
        return plan

    if not store.bucket_exists():
        plan.note = "chưa có bucket - hệ thống chưa thu thập lần nào, không có gì để dọn"
        return plan

    cutoff = utc_now() - timedelta(days=days)
    for obj in store.list_objects(store.prefix + "/"):
        if obj["last_modified"] < cutoff:
            plan.count += 1
            plan.bytes_freed += obj["size"]
            plan.keys.append(obj["key"])
            if len(plan.samples) < MAX_SAMPLES:
                plan.samples.append(f"{obj['key'][-70:]}  ({obj['last_modified']:%Y-%m-%d})")

    if not plan.count:
        plan.note = f"không có object nào cũ hơn {days} ngày"
    return plan


# =============================================================================
# 4. R3 - hạn lưu dữ liệu API theo lớp (data_class, fetched_at - migration 008)
# =============================================================================

@dataclass(frozen=True)
class R3Limits:
    api_days: int       # raw, metadata, văn bản, staging, bản tin, embedding
    stats_days: int     # thống kê công khai

    @classmethod
    def from_settings(cls, settings) -> "R3Limits":
        """Cờ tắt: thống kê cũng 30 ngày. Cờ bật (đã được chấp thuận): tới 36 tháng."""
        stats = (settings.approved_stats_retention_days if settings.derived_metrics_enabled
                 else settings.api_data_retention_days)
        return cls(api_days=settings.api_data_retention_days, stats_days=stats)


@dataclass(frozen=True)
class R3Rule:
    what: str
    data_class: str
    limit: str          # "api" | "stats"
    count_sql: str      # trả về n, oldest
    delete_sql: str     # xóa, hoặc xóa trường API (bảng registry tracked_channels)

    def days(self, limits: R3Limits) -> int:
        return limits.stats_days if self.limit == "stats" else limits.api_days


_CUTOFF = "now() - make_interval(days => %(days)s)"

# Thứ tự = thứ tự xóa: embedding trước văn bản gốc của nó.
R3_RULES: tuple[R3Rule, ...] = (
    R3Rule("thống kê công khai", "stats_non_authorized", "stats",
           f"SELECT count(*) AS n, min(fetched_at) AS oldest FROM yti.video_observations "
           f"WHERE fetched_at < {_CUTOFF}",
           f"DELETE FROM yti.video_observations WHERE fetched_at < {_CUTOFF}"),
    # Embedding theo hạn của văn bản gốc (đặc tả R3): xóa khi tiêu đề/mô tả nguồn quá hạn.
    R3Rule("embedding tìm kiếm", "derived", "api",
           f"SELECT count(*) AS n, min(v.fetched_at) AS oldest FROM yti.video_chunks c "
           f"JOIN yti.videos_current v USING (video_id) WHERE v.fetched_at < {_CUTOFF}",
           f"DELETE FROM yti.video_chunks c USING yti.videos_current v "
           f"WHERE c.video_id = v.video_id AND v.fetched_at < {_CUTOFF}"),
    R3Rule("metadata video", "metadata_text", "api",
           f"SELECT count(*) AS n, min(fetched_at) AS oldest FROM yti.videos_current "
           f"WHERE fetched_at < {_CUTOFF}",
           f"DELETE FROM yti.videos_current WHERE fetched_at < {_CUTOFF}"),
    # Dòng kênh là registry của ta (khóa ngoại từ channel_batches): chỉ xóa trường API.
    R3Rule("metadata kênh", "metadata_text", "api",
           f"SELECT count(*) AS n, min(fetched_at) AS oldest FROM yti.tracked_channels "
           f"WHERE fetched_at < {_CUTOFF}",
           f"UPDATE yti.tracked_channels SET title = NULL, description = NULL, "
           f"uploads_playlist_id = NULL, fetched_at = NULL WHERE fetched_at < {_CUTOFF}"),
    # Kể cả batch 'quarantined': R3 thắng nguyên tắc giữ bằng chứng.
    R3Rule("staging (mọi batch)", "metadata_text", "api",
           f"SELECT count(*) AS n, min(fetched_at) AS oldest "
           f"FROM yti_staging.video_observations_stg WHERE fetched_at < {_CUTOFF}",
           f"DELETE FROM yti_staging.video_observations_stg WHERE fetched_at < {_CUTOFF}"),
    # Signals and clusters (migration 010): numbers derived from public statistics.
    R3Rule("tín hiệu S1-S3", "derived", "stats",
           f"SELECT count(*) AS n, min(fetched_at) AS oldest FROM yti.video_signals "
           f"WHERE fetched_at < {_CUTOFF}",
           f"DELETE FROM yti.video_signals WHERE fetched_at < {_CUTOFF}"),
    R3Rule("cụm chủ đề", "derived", "stats",
           f"SELECT count(*) AS n, min(fetched_at) AS oldest FROM yti.topic_clusters "
           f"WHERE fetched_at < {_CUTOFF}",
           f"DELETE FROM yti.topic_clusters WHERE fetched_at < {_CUTOFF}"),
    R3Rule("bản tin (evidence có tiêu đề + số liệu)", "metadata_text", "api",
           f"SELECT count(*) AS n, min(fetched_at) AS oldest FROM yti.reports "
           f"WHERE fetched_at < {_CUTOFF}",
           f"DELETE FROM yti.reports WHERE fetched_at < {_CUTOFF}"),
)


def plan_r3(cur, limits: R3Limits) -> list[CleanupPlan]:
    """Đếm dữ liệu API quá hạn R3, từng lớp. KHÔNG xóa."""
    plans = []
    for rule in R3_RULES:
        days = rule.days(limits)
        cur.execute(rule.count_sql, {"days": days})
        row = cur.fetchone()
        plan = CleanupPlan(what=f"R3 {rule.what} [{rule.data_class}, {days} ngày]",
                           count=row["n"] or 0)
        if plan.count:
            plan.samples = [f"cũ nhất: {row['oldest']:%Y-%m-%d %H:%M}"]
        else:
            plan.note = f"không có gì cũ hơn {days} ngày"
        plans.append(plan)
    return plans


def delete_r3(cur, limits: R3Limits) -> dict[str, int]:
    """XÓA THẬT dữ liệu API quá hạn R3. Nơi gọi bọc transaction."""
    done = {}
    for rule in R3_RULES:
        cur.execute(rule.delete_sql, {"days": rule.days(limits)})
        done[rule.what] = cur.rowcount
        logger.info("R3 %s: %d dòng (quá %d ngày)", rule.what, cur.rowcount, rule.days(limits))
    return done


# =============================================================================
# 5. XÓA THEO YÊU CẦU (R3: trong 7 ngày) - toàn bộ dữ liệu API của một kênh
# =============================================================================

_CHANNEL_VIDEOS = """
    SELECT video_id FROM yti.videos_current WHERE channel_id = %(ch)s
    UNION
    SELECT DISTINCT video_id FROM yti_staging.video_observations_stg
     WHERE channel_id = %(ch)s AND video_id IS NOT NULL
    UNION
    SELECT DISTINCT o.video_id FROM yti.video_observations o
      JOIN yti.channel_batches b USING (batch_id) WHERE b.channel_id = %(ch)s
"""

# A brief mentions the channel when it lists one of its videos or its name.
_REPORT_MATCH = (
    "EXISTS (SELECT 1 FROM jsonb_array_elements(COALESCE(r.evidence_bundle->'candidates', "
    "'[]'::jsonb)) c WHERE c->>'video_id' = ANY(%(vids)s)) "
    "OR (%(title)s IS NOT NULL AND r.evidence_bundle->'group_channels' ? %(title)s)")

# (what, count_sql, delete_sql); params: ch, vids, title. Embedding trước video.
CHANNEL_DELETION_STEPS: tuple[tuple[str, str, str], ...] = (
    ("bản tin nhắc tới kênh",
     "SELECT count(*) AS n FROM yti.reports r WHERE " + _REPORT_MATCH,
     "DELETE FROM yti.reports r WHERE " + _REPORT_MATCH),
    ("thống kê",
     "SELECT count(*) AS n FROM yti.video_observations o JOIN yti.channel_batches b "
     "USING (batch_id) WHERE b.channel_id = %(ch)s",
     "DELETE FROM yti.video_observations o USING yti.channel_batches b "
     "WHERE o.batch_id = b.batch_id AND b.channel_id = %(ch)s"),
    ("embedding",
     "SELECT count(*) AS n FROM yti.video_chunks c JOIN yti.videos_current v "
     "USING (video_id) WHERE v.channel_id = %(ch)s",
     "DELETE FROM yti.video_chunks c USING yti.videos_current v "
     "WHERE c.video_id = v.video_id AND v.channel_id = %(ch)s"),
    ("metadata video",
     "SELECT count(*) AS n FROM yti.videos_current WHERE channel_id = %(ch)s",
     "DELETE FROM yti.videos_current WHERE channel_id = %(ch)s"),
    ("staging",
     "SELECT count(*) AS n FROM yti_staging.video_observations_stg WHERE channel_id = %(ch)s",
     "DELETE FROM yti_staging.video_observations_stg WHERE channel_id = %(ch)s"),
    ("metadata kênh (tắt kênh)",
     "SELECT count(*) AS n FROM yti.tracked_channels WHERE channel_id = %(ch)s",
     "UPDATE yti.tracked_channels SET title = NULL, description = NULL, "
     "uploads_playlist_id = NULL, fetched_at = NULL, enabled = FALSE "
     "WHERE channel_id = %(ch)s"),
)


def _channel_params(cur, channel_id: str) -> dict:
    cur.execute(_CHANNEL_VIDEOS, {"ch": channel_id})
    vids = [r["video_id"] for r in cur.fetchall()]
    cur.execute("SELECT title FROM yti.tracked_channels WHERE channel_id = %(ch)s",
                {"ch": channel_id})
    row = cur.fetchone()
    return {"ch": channel_id, "vids": vids, "title": row["title"] if row else None}


def _channel_raw_keys(store: RawStore, channel_id: str) -> list[dict]:
    if not store.bucket_exists():
        return []
    marker = f"/channel_id={channel_id}/"
    return [o for o in store.list_objects(store.prefix + "/") if marker in o["key"]]


def plan_channel_deletion(cur, store: RawStore, channel_id: str) -> list[CleanupPlan]:
    """Đếm mọi dữ liệu API của một kênh, trong database và trên MinIO. KHÔNG xóa."""
    params = _channel_params(cur, channel_id)
    plans = []
    for what, count_sql, _ in CHANNEL_DELETION_STEPS:
        cur.execute(count_sql, params)
        plans.append(CleanupPlan(what=what, count=cur.fetchone()["n"] or 0))
    objs = _channel_raw_keys(store, channel_id)
    plans.append(CleanupPlan(what="raw trên MinIO", count=len(objs),
                             bytes_freed=sum(o["size"] for o in objs),
                             keys=[o["key"] for o in objs],
                             samples=[o["key"][-70:] for o in objs[:MAX_SAMPLES]]))
    return plans


def delete_channel_data(cur, channel_id: str) -> dict[str, int]:
    """XÓA THẬT dữ liệu API của một kênh trong database. Nơi gọi bọc transaction,
    rồi xóa raw (plan_channel_deletion(...)[-1].keys) sau khi commit."""
    params = _channel_params(cur, channel_id)
    done = {}
    for what, _, delete_sql in CHANNEL_DELETION_STEPS:
        cur.execute(delete_sql, params)
        done[what] = cur.rowcount
    logger.info("Đã xóa dữ liệu API của kênh %s: %s", channel_id, done)
    return done
