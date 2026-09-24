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
   raw_retention_days=None nghĩa là "chưa xác minh chính sách -> không xóa".
   Đặt số vào code là cam kết một điều mình chưa kiểm chứng.
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
        plan.note = ("TẮT - raw_retention_days=null. Đọc [S7] và ghi vào "
                     "docs/policy_notes.md trước khi bật.")
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
