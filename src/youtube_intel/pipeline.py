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
import os
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from youtube_intel.config import ChannelConfig, Config
from youtube_intel.normalize import from_staging_row, normalize_video
from youtube_intel.storage import BatchManifest, MANIFEST_SCHEMA_VERSION, RawStore, utc_now
from youtube_intel.youtube import (
    MAX_RESULTS,
    ConfigurationError,
    QuotaExceededError,
    YouTubeClient,
)
from youtube_intel import quality, repository as repo

logger = logging.getLogger(__name__)

RESOURCE = "videos"

# Số video mỗi file raw = đúng cỡ lô của videos.list (tối đa 50 ID/request).
RAW_BATCH_SIZE = MAX_RESULTS

# Định danh "tập dữ liệu" mà yt_collect GHI và yt_report ĐỌC. Airflow dùng nó
# để lập lịch theo dữ liệu (Dataset): report chạy khi bảng này vừa được cập nhật.
# Chỉ là một CHUỖI - package vẫn không import airflow.
# Đặt ở đây (một chỗ) để hai DAG không thể gõ lệch nhau: lệch một ký tự là
# report KHÔNG BAO GIỜ chạy, và không có lỗi nào báo ra.
OBSERVATIONS_DATASET_URI = "yti://published/video_observations"


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

def current_slot(settings, now: datetime | None = None) -> datetime:
    """Làm tròn XUỐNG về mốc lịch gần nhất, TÍNH THEO MÚI GIỜ BÁO CÁO.

    Trả về datetime aware ở UTC (để lưu database), nhưng mốc được xác định theo
    report_timezone - đúng như DAG.

    Ví dụ với snapshot_hours=24, anchor=21, tz=Asia/Ho_Chi_Minh:
        chạy lúc 22:30 ngày 23 (giờ VN)  ->  mốc 21:00 ngày 23
        chạy lúc 08:00 ngày 24 (giờ VN)  ->  mốc 21:00 ngày 23  (chưa tới mốc hôm nay)

    ⭐ VÌ SAO KHÔNG DÙNG THẲNG now()?
    Vì scheduled_for phải ỔN ĐỊNH: mọi lần chạy lại trong cùng một ô phải cho
    CÙNG một giá trị. Dùng now() thì chạy lại 5 phút sau sẽ ra giá trị khác ->
    hai collection cho cùng một ô lịch -> dữ liệu trùng.

    ⭐ VÀ VÌ SAO PHẢI THEO MÚI GIỜ, KHÔNG PHẢI UTC?
    Vì DAG dùng tz=Asia/Ho_Chi_Minh. Nếu hàm này tính theo UTC thì chạy tay và
    chạy theo lịch sẽ ra HAI MỐC KHÁC NHAU cho cùng một thời điểm - tức hai
    collection_id khác nhau, phá vỡ idempotency. Một khái niệm phải có MỘT
    định nghĩa. (Đây là nợ kỹ thuật từ Bước 9, nay đã trả.)
    """
    tz = ZoneInfo(settings.report_timezone)
    now_local = (now or utc_now()).astimezone(tz)
    midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)

    # Sinh mọi mốc của HÔM QUA và HÔM NAY, rồi lấy mốc muộn nhất đã trôi qua.
    # Phải xét cả hôm qua: lúc 08:00 với anchor 21 thì mốc gần nhất là 21:00
    # NGÀY HÔM TRƯỚC, không phải hôm nay.
    candidates = [
        midnight + timedelta(days=d, hours=h)
        for d in (-1, 0)
        for h in settings.slot_hours()
    ]
    past = [c for c in candidates if c <= now_local]
    if not past:                     # gần như không xảy ra, nhưng đừng để vỡ
        past = candidates[:1]
    return max(past).astimezone(timezone.utc)


def deterministic_batch_id(collection_id: str, channel_id: str) -> str:
    """batch_id TẤT ĐỊNH từ (collection, channel).

    uuid5 = hàm băm: cùng đầu vào luôn cho cùng UUID. Nhờ vậy chạy lại cùng một
    collection sẽ sinh ra CÙNG batch_id -> clear_staging_batch xóa đúng chỗ ->
    không nhân đôi dòng. Dùng uuid4 (ngẫu nhiên) là mất tính chất này.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{collection_id}/{channel_id}"))


def git_version() -> str | None:
    """Phiên bản code đang chạy, ghi vào collection_runs.code_version.

    Sáu tháng sau nhìn thấy dữ liệu lạ, ta biết nó do phiên bản code nào sinh ra.
    Đây là data lineage ở mức đơn giản nhất - và rẻ nhất.

    HAI NGUỒN, THEO THỨ TỰ ƯU TIÊN:
      1. APP_VERSION  - nướng vào image lúc build (Dockerfile ARG GIT_SHA).
                        Đây là nguồn ĐÚNG ở mọi nơi triển khai: image là artifact
                        bất biến, một image = một phiên bản code, mãi mãi.
      2. `git rev-parse` - chỉ dùng khi chạy từ repo trên máy dev.
                        Trong container KHÔNG có .git -> luôn thất bại.

    Vì sao không chỉ dùng git? Vì production không có repo git cạnh ứng dụng,
    và nếu có thì repo đó có thể đã checkout sang commit khác - báo cáo sai
    còn tệ hơn báo cáo trống.
    """
    baked = os.environ.get("APP_VERSION", "").strip()
    if baked and baked != "unknown":
        return baked

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
    scheduled_for = scheduled_for or current_slot(settings)
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
            # (YouTubeError, RepositoryError, StorageError... đều là Exception)
            except Exception as e:
                outcome.status = "failed"
                outcome.error_code = type(e).__name__
                outcome.error_message = str(e)[:500]
                # exc_info=True -> ghi cả stack trace vào log. Không có nó,
                # lỗi bất ngờ chỉ còn một dòng chữ và không truy được nguồn.
                logger.error("Kênh %s thất bại: %s", channel.title, e, exc_info=True)

            finally:
                # `finally` chạy cả khi nhánh except ở trên `break`.
                outcome.quota_units = yt.meter.estimated_units - units_before
                if outcome.status == "failed" and not dry_run:
                    # Transaction của kênh đã rollback -> không có gì được công bố.
                    outcome.quality_status = "pending"
                    _record_channel_failure(db, collection_id=collection_id,
                                            channel=channel, outcome=outcome)

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
    # yt.meter đếm cho CẢ tiến trình. Chạy tay nhiều kênh một lượt thì con số cộng
    # dồn qua các kênh -> ghi HIỆU SỐ để manifest/batch nói đúng quota của kênh này.
    requests_before = yt.meter.request_count
    units_before = yt.meter.estimated_units

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

    if dry_run:
        logger.info("[DRY RUN] %s: %d video, truncated=%s",
                    channel.title, len(det.videos), disc.truncated)
        return

    # Chia lô MỘT lần, dùng chung cho file raw VÀ staging -> mỗi dòng staging trỏ
    # đúng file raw đã sinh ra nó (data lineage), kể cả khi một kênh có > 1 file.
    chunks = [list(det.videos[s:s + RAW_BATCH_SIZE])
              for s in range(0, len(det.videos), RAW_BATCH_SIZE)]

    # ---- 4-5. RAW TRƯỚC, rồi MANIFEST ------------------------------------
    # Thứ tự này là quyết định thiết kế: raw KHÔNG THỂ tạo lại (YouTube không
    # trả số liệu quá khứ), còn database thì LUÔN dựng lại được từ raw.
    # Luôn bảo vệ thứ không thể tạo lại trước.
    objects = []
    for i, chunk in enumerate(chunks, start=1):
        obj = store.put_batch(resource=RESOURCE, channel_id=channel.channel_id,
                              collection_id=collection_id, attempt=1, batch_index=i,
                              payload=chunk, observed_at=observed_at)
        objects.append(obj.__dict__)
    requests_used = yt.meter.request_count - requests_before

    manifest = BatchManifest(
        schema_version=MANIFEST_SCHEMA_VERSION, collection_id=collection_id,
        channel_id=channel.channel_id, attempt=1, resource=RESOURCE,
        scheduled_for=scheduled_for.isoformat(), started_at=started_at.isoformat(),
        completed_at=utc_now().isoformat(),
        expected_count=det.expected_count, received_count=det.received_count,
        missing_ids=list(det.missing_ids),
        discovery_total_reported=disc.total_reported,
        discovery_truncated=disc.truncated,
        request_count=requests_used,
        estimated_units=yt.meter.estimated_units - units_before,
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
                for chunk, obj in zip(chunks, objects):
                    repo.load_staging_rows(cur, batch_id=batch_id, collection_id=collection_id,
                                           channel_id=channel.channel_id,
                                           observed_at=observed_at,
                                           source_object_key=obj["key"],
                                           videos=[normalize_video(v) for v in chunk])

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
                # Ghi báo cáo chất lượng vào log - kể cả khi PASSED. Trước đây các
                # WARNING (vd bao nhiêu video thiếu like) chỉ nằm trong bộ nhớ.
                logger.info("Quality gate %s: %s", channel.title, report.summary())

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
                    request_count=requests_used,
                    discovery_truncated=disc.truncated,
                    manifest_key=outcome.manifest_key,
                    error_code=outcome.error_code, error_message=outcome.error_message,
                )


def _record_channel_failure(db, *, collection_id: str, channel: ChannelConfig,
                            outcome: ChannelOutcome) -> None:
    """Ghi dòng channel_batches 'failed' bằng một transaction RIÊNG.

    ⭐ VÌ SAO CẦN?
    Transaction của kênh đã ROLLBACK (hoặc chưa từng mở, nếu hỏng ở bước gọi API),
    nên dòng batch biến mất cùng nó. Không ghi riêng thì lỗi chỉ còn trong log, và
    câu hỏi "tuần này kênh nào lỗi, lỗi gì" không trả lời được bằng SQL. Nó cũng
    là thứ giúp finish_collection_run tính đúng 'partial'/'failed'.

    BEST-EFFORT: nếu chính database đang sập thì không ghi được - chỉ log, KHÔNG
    ném lỗi mới đè lên lỗi gốc (lỗi gốc mới là thứ người vận hành cần thấy).
    """
    try:
        with db.connect() as conn:
            with conn:
                with repo.dict_cursor(conn) as cur:
                    # Lần thu ĐẦU TIÊN của kênh hỏng ngay ở API -> kênh chưa có trong
                    # registry -> dòng batch sẽ vi phạm khóa ngoại. Bảo đảm trước.
                    repo.ensure_channel(cur, channel_id=channel.channel_id,
                                        group_code=channel.group,
                                        source_url=channel.source_url)
                    batch_id = repo.insert_channel_batch(
                        cur, batch_id=deterministic_batch_id(collection_id, channel.channel_id),
                        collection_id=collection_id, channel_id=channel.channel_id)
                    repo.finish_channel_batch(
                        cur, batch_id=batch_id, status="failed", quality_status="pending",
                        expected_count=outcome.expected_count or None,
                        received_count=outcome.received_count or None,
                        request_count=None, discovery_truncated=outcome.truncated,
                        manifest_key=outcome.manifest_key,
                        error_code=outcome.error_code, error_message=outcome.error_message)
    except Exception as e:
        logger.error("Không ghi được trạng thái lỗi của kênh %s vào DB: %s", channel.title, e)


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
    run_created = False          # replay có phải là người TẠO dòng collection_runs?

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
            # Summary phải in ô lịch GỐC của lần thu, không phải giờ chạy replay.
            result.scheduled_for = datetime.fromisoformat(man["scheduled_for"])
            manifest_key = store.manifest_key(resource=RESOURCE, channel_id=channel.channel_id,
                                              collection_id=collection_id, attempt=attempt)
            # Đọc TỪNG file kèm key và observed_at của CHÍNH nó -> mỗi dòng staging
            # trỏ đúng file raw đã sinh ra nó (lineage), giống đường thu thật.
            parts = [(o["key"], datetime.fromisoformat(o["observed_at"]), store.read_batch(o["key"]))
                     for o in man["objects"]]
            outcome.expected_count = man["expected_count"]
            outcome.received_count = sum(len(raw_rows) for _, _, raw_rows in parts)
            outcome.manifest_key = manifest_key

            with db.connect() as conn:
                with conn:
                    with repo.dict_cursor(conn) as cur:
                        created = repo.insert_collection_run(
                            cur, collection_id=collection_id,
                            scheduled_for=datetime.fromisoformat(man["scheduled_for"]),
                            started_at=datetime.fromisoformat(man["started_at"]),
                            code_version=man.get("code_version"))
                        run_created = run_created or created
                        batch_id = repo.insert_channel_batch(
                            cur, batch_id=deterministic_batch_id(collection_id, channel.channel_id),
                            collection_id=collection_id, channel_id=channel.channel_id)
                        outcome.batch_id = batch_id

                        repo.clear_staging_batch(cur, batch_id=batch_id)
                        for key, observed_at, raw_rows in parts:
                            repo.load_staging_rows(cur, batch_id=batch_id,
                                                   collection_id=collection_id,
                                                   channel_id=channel.channel_id,
                                                   observed_at=observed_at,
                                                   source_object_key=key,
                                                   videos=[normalize_video(r) for r in raw_rows])
                        rows = repo.fetch_staging_rows(cur, batch_id=batch_id)
                        report = quality.check_batch(
                            batch_id=batch_id, rows=rows,
                            expected_count=man["expected_count"],
                            discovery_truncated=man["discovery_truncated"])
                        outcome.quality_status = report.quality_status
                        logger.info("Quality gate %s: %s", channel.title, report.summary())

                        if report.passed:
                            pub = [from_staging_row(r) for r in rows]
                            d, f = repo.publish_batch(cur, batch_id=batch_id,
                                                      collection_id=collection_id, videos=pub)
                            outcome.published_videos, outcome.published_observations = d, f
                        else:
                            outcome.error_code = "quality_gate_failed"
                            outcome.error_message = ",".join(i.code for i in report.errors)

                        # Đóng batch GIỐNG HỆT đường thu thật. Trước đây replay chỉ cập
                        # nhật quality_status, mà insert_channel_batch (mẹo upsert-and-
                        # return) đã đặt status='running' -> batch kẹt 'running' mãi mãi,
                        # và replay một lần thu đã xong còn KÉO LÙI nó về 'running'.
                        repo.finish_channel_batch(
                            cur, batch_id=batch_id, status="succeeded",
                            quality_status=report.quality_status,
                            expected_count=man["expected_count"],
                            received_count=outcome.received_count,
                            request_count=man.get("request_count"),
                            discovery_truncated=man["discovery_truncated"],
                            manifest_key=manifest_key,
                            error_code=outcome.error_code, error_message=outcome.error_message)
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

    # Đóng sổ lần thu. Trạng thái được tính lại từ channel_batches, nên replay cứu
    # được kênh từng hỏng ở bước DB thì lần thu tự chuyển partial -> succeeded.
    # ended_at chỉ ghi khi CHÍNH replay tạo ra dòng lần thu (database dựng lại từ
    # đầu); lần thu thật đã có ended_at thì giữ nguyên lịch sử của nó.
    # Trước đây replay không đóng sổ -> dòng do replay tạo kẹt 'running' mãi mãi.
    if result.outcomes:
        try:
            with db.connect() as conn:
                with conn:
                    with repo.dict_cursor(conn) as cur:
                        repo.finish_collection_run(
                            cur, collection_id=collection_id, status=result.status,
                            ended_at=result.ended_at if run_created else None,
                            notes="dựng lại bằng replay" if run_created else None)
        except Exception as e:
            logger.error("Không đóng được sổ lần thu %s: %s", collection_id, e)
    return result
