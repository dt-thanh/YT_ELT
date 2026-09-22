"""
TẦNG TRUY CẬP DATABASE - nơi DUY NHẤT trong project chứa câu SQL.

Vì sao gom hết SQL về một file?
  - Đổi schema -> biết chính xác phải sửa ở đâu (grep một file, không phải cả repo)
  - Không lẫn SQL vào logic nghiệp vụ -> logic test được mà không cần database
  - Đây là mẫu REPOSITORY PATTERN: tầng trên gọi hàm có tên nghiệp vụ
    (load_staging_rows), không tự viết SQL.

HAI QUY TẮC KHÔNG ĐƯỢC PHÁ:
  1. LUÔN dùng tham số hóa (%s), TUYỆT ĐỐI không nối chuỗi vào SQL.
     Nối chuỗi = lỗ hổng SQL injection. Tiêu đề video là dữ liệu KHÔNG TIN CẬY
     do người lạ đặt - một tiêu đề có thể chứa   '); DROP TABLE ...; --
  2. Ghi theo LÔ, KHÔNG commit từng dòng. Xem bài giảng về execute_values.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

import psycopg2
from psycopg2.extras import RealDictCursor, execute_values

logger = logging.getLogger(__name__)

# Cỡ lô cho execute_values. Quá nhỏ -> nhiều vòng đi lại mạng.
# Quá lớn -> câu lệnh khổng lồ, tốn RAM cả client lẫn server.
# 500-1000 là vùng hợp lý cho hàng có vài chục cột.
PAGE_SIZE = 500


class RepositoryError(Exception):
    """Lỗi tầng database. Lớp riêng để nơi gọi phân biệt với lỗi API/storage."""


@dataclass(frozen=True)
class Database:
    """Thông tin kết nối. Nhận từ config.py, KHÔNG tự đọc os.environ."""
    host: str
    port: str
    name: str
    user: str
    password: str

    @contextmanager
    def connect(self):
        """Mở kết nối, đảm bảo ĐÓNG kể cả khi có ngoại lệ.

        ⚠️ BẪY KINH ĐIỂN CỦA psycopg2:
            with conn:        -> đây là TRANSACTION (commit/rollback), KHÔNG đóng kết nối
            conn.close()      -> mới là đóng
        Rất nhiều người tưởng `with conn:` tự đóng -> rò rỉ kết nối, Postgres cạn
        slot sau vài giờ chạy. Hàm này bọc lại cho đúng: finally luôn close.
        """
        conn = None
        try:
            conn = psycopg2.connect(
                host=self.host, port=self.port, dbname=self.name,
                user=self.user, password=self.password,
                # connect_timeout: không có thì kết nối tới host chết sẽ TREO VĨNH VIỄN
                connect_timeout=10,
                # Đặt tên ứng dụng -> nhìn pg_stat_activity biết ai đang giữ kết nối.
                # Chi tiết nhỏ, cứu rất nhiều thời gian khi điều tra sự cố.
                application_name="youtube_intel",
            )
            yield conn
        except psycopg2.Error as e:
            raise RepositoryError(f"Lỗi database: {e}") from e
        finally:
            if conn is not None:
                conn.close()


# =============================================================================
# DIMENSION: kênh
# =============================================================================

def upsert_channel(cur, *, channel_id: str, group_code: str, title: str | None,
                   description: str | None, uploads_playlist_id: str | None,
                   source_url: str | None, verified_at: datetime | None) -> None:
    """Ghi/cập nhật một kênh. IDEMPOTENT nhờ ON CONFLICT.

    Đây là SCD TYPE 1: title đổi thì GHI ĐÈ, không giữ lịch sử tên cũ.
    (Type 2 sẽ thêm valid_from/valid_to để giữ cả lịch sử - chưa cần ở MVP.)

    COALESCE(EXCLUDED.x, bảng.x): nếu lần này API không trả về title (None) thì
    GIỮ giá trị cũ, không ghi đè bằng NULL. Nguyên tắc: dữ liệu mới KHÔNG được
    phép XÓA dữ liệu cũ chỉ vì lần này nguồn im lặng.
    """
    cur.execute(
        """
        INSERT INTO yti.tracked_channels
            (channel_id, group_code, title, description,
             uploads_playlist_id, source_url, verified_at, metadata_refreshed_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (channel_id) DO UPDATE SET
            group_code            = EXCLUDED.group_code,
            title                 = COALESCE(EXCLUDED.title, yti.tracked_channels.title),
            description           = COALESCE(EXCLUDED.description, yti.tracked_channels.description),
            uploads_playlist_id   = COALESCE(EXCLUDED.uploads_playlist_id, yti.tracked_channels.uploads_playlist_id),
            source_url            = COALESCE(EXCLUDED.source_url, yti.tracked_channels.source_url),
            verified_at           = COALESCE(EXCLUDED.verified_at, yti.tracked_channels.verified_at),
            metadata_refreshed_at = now();
        """,
        (channel_id, group_code, title, description, uploads_playlist_id, source_url, verified_at),
    )


# =============================================================================
# SỔ THEO DÕI LẦN CHẠY
# =============================================================================

def insert_collection_run(cur, *, collection_id: str, scheduled_for: datetime,
                          started_at: datetime, code_version: str | None = None) -> None:
    """Mở một lần thu. Ghi NGAY khi bắt đầu, không đợi chạy xong.

    Vì sao ghi trước? Vì nếu chết giữa chừng, ta vẫn còn bằng chứng "đã từng có
    một lần chạy lúc X và nó không kết thúc". Chỉ ghi khi thành công thì mọi lần
    thất bại đều BIẾN MẤT không dấu vết - và bạn không bao giờ biết hệ thống
    hỏng bao nhiêu lần.
    """
    cur.execute(
        """
        INSERT INTO yti.collection_runs
            (collection_id, scheduled_for, started_at, status, code_version)
        VALUES (%s, %s, %s, 'running', %s)
        ON CONFLICT (collection_id) DO NOTHING;
        """,
        (collection_id, scheduled_for, started_at, code_version),
    )


def finish_collection_run(cur, *, collection_id: str, status: str,
                          ended_at: datetime, notes: str | None = None) -> None:
    """Đóng lần thu. status: succeeded | failed | partial.

    'partial' là trạng thái TRUNG THỰC cho trường hợp 3/4 kênh thành công.
    Không được gọi là 'succeeded' (nói dối) cũng không phải 'failed' (phí dữ
    liệu đã lấy được). Hệ thống nghiêm túc phải có chỗ cho 'một phần'.
    """
    cur.execute(
        """
        UPDATE yti.collection_runs
           SET status = %s, ended_at = %s, notes = COALESCE(%s, notes)
         WHERE collection_id = %s;
        """,
        (status, ended_at, notes, collection_id),
    )


def insert_channel_batch(cur, *, batch_id: str, collection_id: str, channel_id: str) -> str:
    """Mở batch cho một kênh. TRẢ VỀ batch_id THỰC SỰ đang có trong database.

    ⚠️ VÌ SAO PHẢI TRẢ VỀ, KHÔNG PHẢI None?
    Bảng có UNIQUE(collection_id, channel_id). Nếu cặp đó đã tồn tại (do lần
    chạy trước), batch_id ta muốn dùng sẽ KHÔNG được ghi vào. Nơi gọi không
    biết điều đó, vẫn dùng batch_id của mình để INSERT vào video_observations
    -> vi phạm khóa ngoại. Đây là bug thật đã xảy ra khi xây Bước 7.

    ⚠️ VÀ VÌ SAO `DO UPDATE` CHỨ KHÔNG PHẢI `DO NOTHING`?
    Bẫy Postgres: `ON CONFLICT DO NOTHING ... RETURNING` trả về 0 DÒNG khi có
    xung đột -> không lấy được id đang tồn tại. Chỉ `DO UPDATE` mới khiến
    RETURNING trả về dòng. Ở đây ta UPDATE một trường vô hại (status) để có
    được RETURNING - kỹ thuật này gọi là "upsert-and-return".

    BÀI HỌC TỔNG QUÁT: một hàm tên `insert_*` mà ÂM THẦM không insert là hàm
    nguy hiểm. Hàm phải nói ra kết quả thật của nó.
    """
    cur.execute(
        """
        INSERT INTO yti.channel_batches (batch_id, collection_id, channel_id, status)
        VALUES (%s, %s, %s, 'running')
        ON CONFLICT (collection_id, channel_id) DO UPDATE
            SET status = 'running'
        RETURNING batch_id;
        """,
        (batch_id, collection_id, channel_id),
    )
    row = cur.fetchone()
    effective = row["batch_id"] if isinstance(row, dict) else row[0]
    effective = str(effective)

    if effective != batch_id:
        logger.warning(
            "Batch cho (collection=%s, channel=%s) ĐÃ TỒN TẠI với batch_id=%s; "
            "bỏ qua batch_id mới %s và dùng lại cái cũ.",
            collection_id, channel_id, effective, batch_id,
        )
    return effective


def finish_channel_batch(cur, *, batch_id: str, status: str, quality_status: str,
                         expected_count: int | None, received_count: int | None,
                         request_count: int | None, discovery_truncated: bool,
                         manifest_key: str | None,
                         error_code: str | None = None,
                         error_message: str | None = None) -> None:
    """Đóng batch của một kênh, kèm số liệu đối soát.

    expected/received ghi ở đây chính là thứ biến 'silent data loss' thành con
    số nhìn thấy được: gửi 50 id mà chỉ nhận 48 thì bảng này nói ra điều đó.
    """
    cur.execute(
        """
        UPDATE yti.channel_batches
           SET status = %s, quality_status = %s,
               expected_count = %s, received_count = %s, request_count = %s,
               discovery_truncated = %s, manifest_key = %s,
               error_code = %s, error_message = %s
         WHERE batch_id = %s;
        """,
        (status, quality_status, expected_count, received_count, request_count,
         discovery_truncated, manifest_key, error_code, error_message, batch_id),
    )


# =============================================================================
# NẠP VÀO STAGING - ⭐ phần quan trọng nhất về hiệu năng
# =============================================================================

def load_staging_rows(cur, *, batch_id: str, collection_id: str, channel_id: str,
                      observed_at: datetime, source_object_key: str,
                      videos) -> int:
    """Nạp nhiều bản ghi vào staging bằng MỘT câu lệnh.

    ⭐ execute_values GOM nhiều dòng vào MỘT câu INSERT:
           INSERT INTO ... VALUES (...), (...), (...), ... ;
       thay vì gửi 50 câu INSERT riêng lẻ.

    Vì sao khác biệt lớn đến thế? Mỗi câu lệnh phải đi một vòng CLIENT -> SERVER
    -> CLIENT (round trip). Với 50 dòng:
           từng dòng một : 50 vòng mạng + 50 lần commit
           execute_values:  1 vòng mạng +  1 lần commit
    Code cũ của repo còn tệ hơn: conn.commit() SAU MỖI DÒNG. Mỗi commit bắt
    Postgres ghi WAL xuống đĩa và chờ fsync -> chậm gấp hàng chục lần, VÀ
    không atomic: chết giữa chừng để lại NỬA dữ liệu trong bảng.

    KHÔNG commit trong hàm này. Nơi gọi quyết định khi nào commit - vì nó mới
    biết ranh giới transaction nghiệp vụ nằm ở đâu.
    """
    rows = [
        (
            batch_id, collection_id, channel_id, v.video_id,
            v.raw_view_count, v.raw_like_count, v.raw_comment_count,
            v.raw_duration, v.raw_published_at, v.title, v.live_state,
            observed_at, source_object_key,
            # ### [migration 002] 5 cột bổ sung: staging phải mang ĐỦ mọi thứ
            # mà yti.videos_current cần, nếu không publish buộc phải quay lại
            # đọc MinIO -> hai nguồn sự thật cho cùng một bước.
            v.description, v.thumbnail_url, v.category_id, v.default_language,
            None if v.made_for_kids is None else str(v.made_for_kids),
        )
        for v in videos
    ]

    if not rows:
        return 0

    execute_values(
        cur,
        """
        INSERT INTO yti_staging.video_observations_stg
            (batch_id, collection_id, channel_id, video_id,
             raw_view_count, raw_like_count, raw_comment_count,
             raw_duration, raw_published_at, title, live_broadcast,
             observed_at, source_object_key,
             raw_description, raw_thumbnail_url, raw_category_id,
             raw_default_language, raw_made_for_kids)
        VALUES %s
        """,
        rows,
        page_size=PAGE_SIZE,
    )

    logger.info("Nạp %d dòng vào staging (batch=%s)", len(rows), batch_id)
    return len(rows)


def clear_staging_batch(cur, *, batch_id: str) -> int:
    """Xóa staging của một batch trước khi nạp lại.

    ⭐ ĐÂY LÀ THỨ LÀM CHO VIỆC NẠP TRỞ NÊN IDEMPOTENT.
    Staging KHÔNG có ràng buộc UNIQUE (cố ý - dữ liệu bẩn phải vào được để điều
    tra). Nên chạy lại cùng một batch sẽ nhân đôi số dòng.
    Cách chữa: xóa sạch phần của batch này RỒI mới nạp, cả hai trong CÙNG một
    transaction. Mẫu này tên là DELETE-INSERT (hay 'overwrite partition').
    """
    cur.execute("DELETE FROM yti_staging.video_observations_stg WHERE batch_id = %s;", (batch_id,))
    return cur.rowcount


# =============================================================================
# TRUY VẤN TIỆN ÍCH
# =============================================================================

def fetch_staging_rows(cur, *, batch_id: str) -> list[dict]:
    """Đọc mọi dòng staging của một batch, để quality gate đánh giá.

    Liệt kê TÊN CỘT tường minh thay vì SELECT *:
      - thêm cột mới không làm vỡ code phía sau
      - đọc câu SQL là biết ngay cần những cột nào
      - không kéo về cột khổng lồ (description) nếu không dùng... (ở đây có dùng)
    """
    cur.execute(
        """
        SELECT stg_id, batch_id, collection_id, channel_id, video_id,
               raw_view_count, raw_like_count, raw_comment_count,
               raw_duration, raw_published_at, title, live_broadcast,
               raw_description, raw_thumbnail_url, raw_category_id,
               raw_default_language, raw_made_for_kids,
               observed_at, source_object_key
          FROM yti_staging.video_observations_stg
         WHERE batch_id = %s
         ORDER BY stg_id;
        """,
        (batch_id,),
    )
    return [dict(r) for r in cur.fetchall()]


# =============================================================================
# ⭐ PUBLISH - chuyển staging sang vùng công bố
#
# ĐÂY LÀ HÀM QUAN TRỌNG NHẤT CỦA CẢ PROJECT về mặt tính đúng đắn.
#
# Nó ghi vào HAI bảng. Hai bảng đó phải LUÔN khớp nhau: không được có
# observation của một video mà video đó không có trong videos_current.
# Cách bảo đảm: cả hai nằm trong CÙNG MỘT TRANSACTION.
#
# Nơi gọi phải bọc bằng `with conn:` -> lỗi ở bất kỳ đâu thì Postgres ROLLBACK
# TOÀN BỘ, kể cả những dòng đã INSERT thành công trước đó. Không có trạng thái
# "publish được một nửa".
# =============================================================================

def publish_batch(cur, *, batch_id: str, collection_id: str, videos) -> tuple[int, int]:
    """Đẩy dữ liệu đã kiểm tra vào yti.videos_current + yti.video_observations.

    Trả về (số video dimension, số observation fact).

    THỨ TỰ GHI CÓ CHỦ Ý: DIMENSION TRƯỚC, FACT SAU.
    Lý do: fact (observation) trỏ tới một video; nếu ghi fact trước thì có
    khoảnh khắc tồn tại observation của video chưa có trong dimension.
    Trong một transaction thì không ai thấy khoảnh khắc đó, nhưng giữ đúng thứ
    tự vẫn là kỷ luật cần có - và nó bắt buộc nếu sau này ta thêm khóa ngoại
    từ video_observations sang videos_current.
    """
    if not videos:
        return 0, 0

    # ---- 1. DIMENSION: videos_current (SCD type 1 - ghi đè) ----------------
    dim_rows = [
        (v.video_id, v.channel_id, v.title, v.description, v.published_at,
         v.duration_seconds, v.thumbnail_url, v.category_id, v.default_language,
         v.live_state, v.made_for_kids, v.format_label, v.format_evidence)
        for v in videos
    ]

    execute_values(
        cur,
        """
        INSERT INTO yti.videos_current
            (video_id, channel_id, title, description, published_at,
             duration_seconds, thumbnail_url, category_id, default_language,
             live_state, made_for_kids, format_label, format_evidence)
        VALUES %s
        ON CONFLICT (video_id) DO UPDATE SET
            channel_id       = EXCLUDED.channel_id,
            title            = EXCLUDED.title,
            description      = EXCLUDED.description,
            published_at     = EXCLUDED.published_at,
            duration_seconds = EXCLUDED.duration_seconds,
            thumbnail_url    = COALESCE(EXCLUDED.thumbnail_url, yti.videos_current.thumbnail_url),
            category_id      = COALESCE(EXCLUDED.category_id, yti.videos_current.category_id),
            default_language = COALESCE(EXCLUDED.default_language, yti.videos_current.default_language),
            live_state       = EXCLUDED.live_state,
            made_for_kids    = COALESCE(EXCLUDED.made_for_kids, yti.videos_current.made_for_kids),
            format_label     = EXCLUDED.format_label,
            format_evidence  = EXCLUDED.format_evidence,
            refreshed_at     = now()
            -- KHÔNG cập nhật first_seen_at: nó phải giữ nguyên lần đầu ta thấy
            -- video này. Đó là thông tin chỉ có được MỘT LẦN.
        """,
        dim_rows,
        page_size=PAGE_SIZE,
    )
    dim_count = len(dim_rows)

    # ---- 2. FACT: video_observations (append-only, idempotent) -------------
    fact_rows = [
        (v.video_id, collection_id, batch_id, v.observed_at,
         v.view_count, v.like_count, v.comment_count, v.source_object_key)
        for v in videos
    ]

    execute_values(
        cur,
        """
        INSERT INTO yti.video_observations
            (video_id, collection_id, batch_id, observed_at,
             view_count, like_count, comment_count, source_object_key)
        VALUES %s
        -- ⭐ ĐÂY LÀ CƠ CHẾ IDEMPOTENCY.
        -- Khóa (video_id, collection_id) chính là GRAIN. Chạy lại cùng một
        -- collection -> ON CONFLICT kích hoạt -> CẬP NHẬT đúng dòng đó, KHÔNG
        -- sinh dòng trùng. Nhờ vậy retry/replay bao nhiêu lần cũng an toàn.
        ON CONFLICT (video_id, collection_id) DO UPDATE SET
            view_count        = EXCLUDED.view_count,
            like_count        = EXCLUDED.like_count,
            comment_count     = EXCLUDED.comment_count,
            batch_id          = EXCLUDED.batch_id,
            observed_at       = EXCLUDED.observed_at,
            source_object_key = EXCLUDED.source_object_key
        """,
        fact_rows,
        page_size=PAGE_SIZE,
    )
    fact_count = len(fact_rows)

    logger.info("Publish: %d video (dimension), %d observation (fact)", dim_count, fact_count)
    return dim_count, fact_count


def set_batch_quality(cur, *, batch_id: str, quality_status: str,
                      error_code: str | None = None,
                      error_message: str | None = None) -> None:
    """Ghi kết quả quality gate. quality_status: passed | quarantined.

    Batch bị 'quarantined' KHÔNG bị xóa: dữ liệu vẫn nằm nguyên trong staging
    để điều tra. Nó chỉ không được phép sang vùng công bố.
    Xóa dữ liệu lỗi = mất luôn bằng chứng để hiểu vì sao lỗi.
    """
    cur.execute(
        """
        UPDATE yti.channel_batches
           SET quality_status = %s,
               error_code     = COALESCE(%s, error_code),
               error_message  = COALESCE(%s, error_message)
         WHERE batch_id = %s;
        """,
        (quality_status, error_code, error_message, batch_id),
    )


def count_staging(cur, *, batch_id: str) -> int:
    cur.execute("SELECT count(*) AS n FROM yti_staging.video_observations_stg WHERE batch_id = %s;", (batch_id,))
    row = cur.fetchone()
    return row["n"] if isinstance(row, dict) else row[0]


def dict_cursor(conn):
    """Cursor trả về dict thay vì tuple -> đọc row['video_id'] thay vì row[3].

    Đọc theo TÊN CỘT thay vì theo VỊ TRÍ: thêm một cột vào SELECT không làm
    vỡ code phía sau. Đây là lý do nên tránh 'SELECT *' kết hợp với chỉ số.
    """
    return conn.cursor(cursor_factory=RealDictCursor)
