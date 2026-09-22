"""
PIPELINE - ghép mọi mảnh thành một luồng chạy được.

LUỒNG CHO MỖI KÊNH (thứ tự KHÔNG được đổi):
    1. channels.list          -> xác minh kênh + lấy uploads playlist
    2. playlistItems.list     -> danh sách video ID (có cap)
    3. videos.list            -> chi tiết theo lô 50
    4. ghi RAW lên MinIO      <- BỀN TRƯỚC: raw là thứ KHÔNG THỂ tạo lại
    5. ghi MANIFEST           <- commit marker: tồn tại = batch hoàn chỉnh
    6. nạp STAGING            ┐
    7. QUALITY GATE           ├─ CÙNG MỘT TRANSACTION
    8. PUBLISH hoặc QUARANTINE┘

BA NGUYÊN TẮC CỦA FILE NÀY:

1. RANH GIỚI TRANSACTION = MỘT KÊNH, không phải cả run.
   Kênh 4 lỗi thì kênh 1-3 đã publish phải GIỮ NGUYÊN. Gom cả 4 vào một
   transaction sẽ hoàn tác cả dữ liệu đã tốn quota lấy về.

2. LỖI KHÁC LOẠI XỬ KHÁC NHAU (dùng cây ngoại lệ từ youtube.py):
       QuotaExceededError  -> DỪNG CẢ RUN (kênh sau cũng sẽ lỗi, gọi thêm là phí)
       ConfigurationError  -> DỪNG CẢ RUN (key sai thì kênh nào cũng sai)
       lỗi khác            -> kênh này 'failed', CÁC KÊNH SAU VẪN CHẠY

3. TRẠNG THÁI 'partial' LÀ BẮT BUỘC.
   3/4 kênh thành công KHÔNG phải 'succeeded' (nói dối) cũng không phải
   'failed' (phí dữ liệu đã lấy). Hệ thống trung thực phải có chỗ cho 'một phần'.

File này KHÔNG import airflow. Airflow (Bước 9) chỉ là một wrapper mỏng gọi vào đây.
"""

from __future__ import annotations

import logging
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from youtube_intel.config import ChannelConfig, Config
from youtube_intel.normalize import from_staging_row, normalize_video
from youtube_intel.storage import BatchManifest, MANIFEST_SCHEMA_VERSION, RawStore, utc_now
from youtube_intel.youtube import (
    ConfigurationError,
    QuotaExceededError,
    YouTubeClient,
    YouTubeError,
)
from youtube_intel import quality, repository as repo

logger = logging.getLogger(__name__)

RESOURCE = "videos"


@dataclass
class ChannelOutcome:
    """Kết quả của MỘT kênh trong MỘT lần thu."""
    channel_id: str
    title: str
    status: str = "running"            # running | succeeded | failed
    quality_status: str = "pending"    # pending | passed | quarantined
    batch_id: str | None = None
    manifest_key: str | None = None
    expected_count: int = 0
    received_count: int = 0
    published_videos: int = 0
    published_observations: int = 0
    truncated: bool = False
    quota_units: int = 0
    error_code: str | None = None
    error_message: str | None = None
    issues: list = field(default_factory=list)


@dataclass
class CollectionResult:
    collection_id: str
    scheduled_for: datetime
    started_at: datetime
    ended_at: datetime | None = None
    status: str = "running"            # running | succeeded | partial | failed
    outcomes: list[ChannelOutcome] = field(default_factory=list)
    total_quota_units: int = 0
    aborted_reason: str | None = None

    @property
    def succeeded(self) -> list[ChannelOutcome]:
        return [o for o in self.outcomes if o.status == "succeeded"]

    @property
    def failed(self) -> list[ChannelOutcome]:
        return [o for o in self.outcomes if o.status == "failed"]

    def exit_code(self) -> int:
        """0 = ổn, 1 = có vấn đề.

        Mã thoát là cách chương trình nói chuyện với thế giới bên ngoài:
        Airflow, CI, cron đều đọc nó để biết task thành công hay thất bại.
        'partial' trả 1 vì nó CẦN người xem - im lặng cho qua là che giấu lỗi.
        """
        return 0 if self.status == "succeeded" else 1

    def summary(self) -> str:
        lines = [
            f"Collection {self.collection_id}",
            f"  lịch hẹn   : {self.scheduled_for.isoformat()}",
            f"  thực chạy  : {self.started_at.isoformat()}",
            f"  trạng thái : {self.status.upper()}",
            f"  quota      : {self.total_quota_units} units",
        ]
        if self.aborted_reason:
            lines.append(f"  DỪNG SỚM   : {self.aborted_reason}")
        lines.append("")
        for o in self.outcomes:
            mark = {"succeeded": "OK  ", "failed": "FAIL", "running": "??  "}[o.status]
            lines.append(
                f"  [{mark}] {o.title[:34]:<34} "
                f"{o.received_count}/{o.expected_count} bản ghi "
                f"-> {o.quality_status}"
                + (f" | publish {o.published_observations}" if o.published_observations else "")
                + (f" | {o.error_code}" if o.error_code else "")
            )
        return "\n".join(lines)


# =============================================================================
# TIỆN ÍCH
# =============================================================================

def current_slot(snapshot_hours: int, now: datetime | None = None) -> datetime:
    """Làm tròn XUỐNG về ô lịch gần nhất.

    snapshot_hours=12 -> các ô là 00:00 và 12:00 UTC.
    Chạy lúc 15:31 -> ô 12:00.

    ⭐ VÌ SAO KHÔNG DÙNG THẲNG now()?
    Vì scheduled_for phải là MỘT GIÁ TRỊ ỔN ĐỊNH, giống nhau ở mọi lần chạy lại
    trong cùng một ô. Nếu dùng now(), chạy lại lúc 15:35 sẽ tạo ra một
    scheduled_for KHÁC -> hai collection cho cùng một ô lịch -> dữ liệu trùng.
    Đây là nền tảng để backfill và chạy lại hoạt động đúng.
    """
    now = now or utc_now()
    now = now.astimezone(timezone.utc)
    slot_index = now.hour // snapshot_hours
    return now.replace(hour=slot_index * snapshot_hours, minute=0, second=0, microsecond=0)


def deterministic_batch_id(collection_id: str, channel_id: str) -> str:
    """batch_id TẤT ĐỊNH từ (collection, channel).

    uuid5 = hàm băm: cùng đầu vào luôn cho cùng UUID. Nhờ vậy chạy lại cùng một
    collection sẽ sinh ra CÙNG batch_id -> clear_staging_batch xóa đúng chỗ ->
    không nhân đôi dòng. Dùng uuid4 (ngẫu nhiên) là mất tính chất này.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{collection_id}/{channel_id}"))


def git_version() -> str | None:
    """Commit hash hiện tại, để ghi vào collection_runs.code_version.

    Sáu tháng sau nhìn thấy dữ liệu lạ, ta biết nó do phiên bản code nào sinh ra.
    Đây là data lineage ở mức đơn giản nhất - và rẻ nhất.
    """
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None
    except Exception:
        return None


# =============================================================================
# CHẠY MỘT LẦN THU
# =============================================================================

def run_collection(
    cfg: Config,
    *,
    scheduled_for: datetime | None = None,
    collection_id: str | None = None,
    only_channels: list[str] | None = None,
    dry_run: bool = False,
) -> CollectionResult:
    """Thu thập cho mọi kênh đang bật. Trả về kết quả chi tiết."""
    settings, secrets = cfg.settings, cfg.secrets

    channels = list(cfg.enabled_channels())
    if only_channels:
        channels = [c for c in channels if c.channel_id in only_channels]
    if not channels:
        raise ValueError("Không có kênh nào để thu (kiểm tra config / --channel).")

    # collection_id sinh Ở ĐÂY, TRƯỚC khi chạm database hay MinIO.
    # Lý do: nó nằm trong đường dẫn file raw, nên phải biết trước khi ghi file.
    # Đây chính là vì sao Bước 3 chọn UUID thay vì BIGSERIAL.
    collection_id = collection_id or str(uuid.uuid4())
    scheduled_for = scheduled_for or current_slot(settings.snapshot_hours)
    started_at = utc_now()

    result = CollectionResult(collection_id=collection_id,
                              scheduled_for=scheduled_for, started_at=started_at)

    store = RawStore(endpoint_url=secrets.minio_endpoint,
                     access_key=secrets.minio_access_key,
                     secret_key=secrets.minio_secret_key)
    db = repo.Database(host=secrets.db_host, port=secrets.db_port, name=secrets.db_name,
                       user=secrets.db_user, password=secrets.db_password)

    if dry_run:
        logger.warning("DRY RUN - chỉ gọi API và in kết quả, KHÔNG ghi MinIO/database.")
    else:
        store.ensure_bucket()
        with db.connect() as conn:
            with conn:
                with repo.dict_cursor(conn) as cur:
                    # Ghi NGAY khi bắt đầu, không đợi chạy xong. Nếu chết giữa
                    # chừng ta vẫn còn bằng chứng "đã có một lần chạy và nó
                    # không kết thúc". Chỉ ghi khi thành công thì mọi lần thất
                    # bại đều biến mất không dấu vết.
                    repo.insert_collection_run(cur, collection_id=collection_id,
                                               scheduled_for=scheduled_for,
                                               started_at=started_at,
                                               code_version=git_version())

    with YouTubeClient(secrets.youtube_api_key,
                       timeout=settings.request_timeout_seconds,
                       max_attempts=settings.max_attempts) as yt:
        for channel in channels:
            units_before = yt.meter.estimated_units
            outcome = ChannelOutcome(channel_id=channel.channel_id, title=channel.title)
            result.outcomes.append(outcome)

            try:
                _run_one_channel(cfg, yt, store, db, channel, outcome,
                                 collection_id=collection_id,
                                 scheduled_for=scheduled_for,
                                 started_at=started_at,
                                 dry_run=dry_run)
                outcome.status = "succeeded"

            # ---- LỖI DỪNG CẢ RUN ----------------------------------------
            # Hai loại này KHÔNG có ích gì khi thử kênh tiếp theo:
            # hết quota thì kênh nào cũng hết; key sai thì kênh nào cũng sai.
            # Cố chạy tiếp chỉ tốn thời gian và làm log rối.
            except QuotaExceededError as e:
                outcome.status, outcome.error_code = "failed", "quota_exceeded"
                outcome.error_message = str(e)
                result.aborted_reason = "Hết quota YouTube - dừng toàn bộ run."
                logger.error("HẾT QUOTA - dừng run. %s", e)
                break
            except ConfigurationError as e:
                outcome.status, outcome.error_code = "failed", "configuration_error"
                outcome.error_message = str(e)
                result.aborted_reason = "Lỗi cấu hình - dừng toàn bộ run."
                logger.error("LỖI CẤU HÌNH - dừng run. %s", e)
                break

            # ---- LỖI CHỈ ẢNH HƯỞNG MỘT KÊNH ------------------------------
            except (YouTubeError, repo.RepositoryError, Exception) as e:
                outcome.status = "failed"
                outcome.error_code = type(e).__name__
                outcome.error_message = str(e)[:500]
                # exc_info=True -> ghi cả stack trace vào log. Không có nó,
                # lỗi bất ngờ chỉ còn một dòng chữ và không truy được nguồn.
                logger.error("Kênh %s thất bại: %s", channel.title, e, exc_info=True)

            finally:
                outcome.quota_units = yt.meter.estimated_units - units_before

        result.total_quota_units = yt.meter.estimated_units

    # ---- Chốt trạng thái toàn run -----------------------------------------
    result.ended_at = utc_now()
    n_ok, n_total = len(result.succeeded), len(channels)
    if n_ok == n_total:
        result.status = "succeeded"
    elif n_ok == 0:
        result.status = "failed"
    else:
        result.status = "partial"      # ⭐ trạng thái trung thực cho 3/4

    if not dry_run:
        with db.connect() as conn:
            with conn:
                with repo.dict_cursor(conn) as cur:
                    repo.finish_collection_run(cur, collection_id=collection_id,
                                               status=result.status,
                                               ended_at=result.ended_at,
                                               notes=result.aborted_reason)
    return result


def _run_one_channel(cfg, yt, store, db, channel: ChannelConfig,
                     outcome: ChannelOutcome, *, collection_id: str,
                     scheduled_for: datetime, started_at: datetime,
                     dry_run: bool) -> None:
    """Toàn bộ luồng cho MỘT kênh. Ném ngoại lệ khi lỗi - nơi gọi bắt."""
    settings = cfg.settings

    # ---- 1-3. Gọi API ----------------------------------------------------
    info = yt.fetch_channel(channel_id=channel.channel_id)
    disc = yt.discover_video_ids(info.uploads_playlist_id,
                                 limit=settings.initial_video_limit_per_channel,
                                 page_limit=settings.discovery_page_limit_per_channel)
    # observed_at = thời điểm THỰC SỰ đọc con số, lấy NGAY TRƯỚC khi gọi videos.list.
    # Không dùng scheduled_for (lịch hẹn) và không dùng lúc INSERT vào database.
    observed_at = utc_now()
    det = yt.fetch_video_details(disc.video_ids)

    outcome.expected_count = det.expected_count
    outcome.received_count = det.received_count
    outcome.truncated = disc.truncated

    videos = [normalize_video(v) for v in det.videos]

    if dry_run:
        logger.info("[DRY RUN] %s: %d video, truncated=%s",
                    channel.title, len(videos), disc.truncated)
        return

    # ---- 4-5. RAW TRƯỚC, rồi MANIFEST ------------------------------------
    # Thứ tự này là quyết định thiết kế: raw KHÔNG THỂ tạo lại (YouTube không
    # trả số liệu quá khứ), còn database thì LUÔN dựng lại được từ raw.
    # Luôn bảo vệ thứ không thể tạo lại trước.
    objects = []
    for i, start in enumerate(range(0, len(det.videos), 50), start=1):
        obj = store.put_batch(resource=RESOURCE, channel_id=channel.channel_id,
                              collection_id=collection_id, attempt=1, batch_index=i,
                              payload=list(det.videos[start:start + 50]),
                              observed_at=observed_at)
        objects.append(obj.__dict__)

    manifest = BatchManifest(
        schema_version=MANIFEST_SCHEMA_VERSION, collection_id=collection_id,
        channel_id=channel.channel_id, attempt=1, resource=RESOURCE,
        scheduled_for=scheduled_for.isoformat(), started_at=started_at.isoformat(),
        completed_at=utc_now().isoformat(),
        expected_count=det.expected_count, received_count=det.received_count,
        missing_ids=list(det.missing_ids),
        discovery_total_reported=disc.total_reported,
        discovery_truncated=disc.truncated,
        request_count=yt.meter.request_count, estimated_units=yt.meter.estimated_units,
        objects=objects,
        request_params={"part": "snippet,contentDetails,statistics,status", "maxResults": 50},
        code_version=git_version(),
    )
    outcome.manifest_key = store.put_manifest(manifest)

    # ---- 6-8. MỘT TRANSACTION cho kênh này -------------------------------
    with db.connect() as conn:
        with conn:
            with repo.dict_cursor(conn) as cur:
                repo.upsert_channel(cur, channel_id=channel.channel_id,
                                    group_code=channel.group, title=info.title,
                                    description=info.description,
                                    uploads_playlist_id=info.uploads_playlist_id,
                                    source_url=channel.source_url, verified_at=utc_now())

                wanted = deterministic_batch_id(collection_id, channel.channel_id)
                batch_id = repo.insert_channel_batch(cur, batch_id=wanted,
                                                     collection_id=collection_id,
                                                     channel_id=channel.channel_id)
                outcome.batch_id = batch_id

                # Xóa trước khi nạp -> chạy lại không nhân đôi (delete-insert).
                repo.clear_staging_batch(cur, batch_id=batch_id)
                repo.load_staging_rows(cur, batch_id=batch_id, collection_id=collection_id,
                                       channel_id=channel.channel_id,
                                       observed_at=observed_at,
                                       source_object_key=objects[0]["key"] if objects else None,
                                       videos=videos)

                # ---- QUALITY GATE ----
                rows = repo.fetch_staging_rows(cur, batch_id=batch_id)
                report = quality.check_batch(batch_id=batch_id, rows=rows,
                                             expected_count=det.expected_count,
                                             discovery_truncated=disc.truncated)
                outcome.quality_status = report.quality_status
                outcome.issues = [
                    {"severity": i.severity, "code": i.code, "count": i.count}
                    for i in report.issues
                ]

                if report.passed:
                    pub = [from_staging_row(r) for r in rows]
                    d, f = repo.publish_batch(cur, batch_id=batch_id,
                                              collection_id=collection_id, videos=pub)
                    outcome.published_videos, outcome.published_observations = d, f
                else:
                    # QUARANTINE: dữ liệu Ở LẠI staging để điều tra, chỉ không
                    # được sang vùng công bố. Xóa = mất bằng chứng để hiểu lỗi.
                    codes = ",".join(i.code for i in report.errors)
                    logger.error("Kênh %s QUARANTINE: %s", channel.title, codes)
                    outcome.error_code = "quality_gate_failed"
                    outcome.error_message = codes

                repo.finish_channel_batch(
                    cur, batch_id=batch_id, status="succeeded",
                    quality_status=report.quality_status,
                    expected_count=det.expected_count, received_count=det.received_count,
                    request_count=yt.meter.request_count,
                    discovery_truncated=disc.truncated,
                    manifest_key=outcome.manifest_key,
                    error_code=outcome.error_code, error_message=outcome.error_message,
                )


# =============================================================================
# REPLAY - nạp lại từ manifest, KHÔNG gọi API
# =============================================================================

def replay_collection(cfg: Config, *, collection_id: str, attempt: int = 1) -> CollectionResult:
    """Nạp lại database từ raw đã có. TUYỆT ĐỐI không gọi YouTube.

    ⭐ ĐÂY LÀ THỨ CHỨNG MINH KIẾN TRÚC ELT CÓ GIÁ TRỊ.
    Sửa logic parse hay phân loại -> chạy replay -> dữ liệu đúng trở lại,
    tốn 0 quota, không phụ thuộc mạng. Nếu chỉ có database thì phải gọi lại API,
    mà YouTube KHÔNG trả về số liệu quá khứ -> dữ liệu sai VĨNH VIỄN.
    """
    secrets = cfg.secrets
    store = RawStore(endpoint_url=secrets.minio_endpoint,
                     access_key=secrets.minio_access_key,
                     secret_key=secrets.minio_secret_key)
    db = repo.Database(host=secrets.db_host, port=secrets.db_port, name=secrets.db_name,
                       user=secrets.db_user, password=secrets.db_password)

    result = CollectionResult(collection_id=collection_id,
                              scheduled_for=utc_now(), started_at=utc_now())

    for channel in cfg.enabled_channels():
        outcome = ChannelOutcome(channel_id=channel.channel_id, title=channel.title)

        # Không có manifest = batch CHƯA HOÀN CHỈNH -> bỏ qua.
        # Đây chính là lý do manifest tồn tại: phân biệt "xong" với "đang dở".
        if not store.manifest_exists(resource=RESOURCE, channel_id=channel.channel_id,
                                     collection_id=collection_id, attempt=attempt):
            logger.info("Bỏ qua %s: không có manifest (batch chưa hoàn chỉnh).", channel.title)
            continue

        result.outcomes.append(outcome)
        try:
            man = store.read_manifest(resource=RESOURCE, channel_id=channel.channel_id,
                                      collection_id=collection_id, attempt=attempt)
            raw_rows = []
            for o in man["objects"]:
                raw_rows += store.read_batch(o["key"])

            videos = [normalize_video(r) for r in raw_rows]
            outcome.expected_count = man["expected_count"]
            outcome.received_count = len(videos)
            observed_at = datetime.fromisoformat(man["objects"][0]["observed_at"])

            with db.connect() as conn:
                with conn:
                    with repo.dict_cursor(conn) as cur:
                        repo.insert_collection_run(
                            cur, collection_id=collection_id,
                            scheduled_for=datetime.fromisoformat(man["scheduled_for"]),
                            started_at=datetime.fromisoformat(man["started_at"]),
                            code_version=man.get("code_version"))
                        batch_id = repo.insert_channel_batch(
                            cur, batch_id=deterministic_batch_id(collection_id, channel.channel_id),
                            collection_id=collection_id, channel_id=channel.channel_id)
                        outcome.batch_id = batch_id

                        repo.clear_staging_batch(cur, batch_id=batch_id)
                        repo.load_staging_rows(cur, batch_id=batch_id,
                                               collection_id=collection_id,
                                               channel_id=channel.channel_id,
                                               observed_at=observed_at,
                                               source_object_key=man["objects"][0]["key"],
                                               videos=videos)
                        rows = repo.fetch_staging_rows(cur, batch_id=batch_id)
                        report = quality.check_batch(
                            batch_id=batch_id, rows=rows,
                            expected_count=man["expected_count"],
                            discovery_truncated=man["discovery_truncated"])
                        outcome.quality_status = report.quality_status

                        if report.passed:
                            pub = [from_staging_row(r) for r in rows]
                            d, f = repo.publish_batch(cur, batch_id=batch_id,
                                                      collection_id=collection_id, videos=pub)
                            outcome.published_videos, outcome.published_observations = d, f
                        repo.set_batch_quality(cur, batch_id=batch_id,
                                               quality_status=report.quality_status)
            outcome.status = "succeeded"

        except Exception as e:
            outcome.status = "failed"
            outcome.error_code = type(e).__name__
            outcome.error_message = str(e)[:500]
            logger.error("Replay kênh %s thất bại: %s", channel.title, e, exc_info=True)

    result.ended_at = utc_now()
    n_ok = len(result.succeeded)
    result.status = ("succeeded" if n_ok == len(result.outcomes) and n_ok
                     else "failed" if n_ok == 0 else "partial")
    return result
