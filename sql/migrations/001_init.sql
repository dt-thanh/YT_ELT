-- =============================================================================
-- 001_init.sql  -  Schema khởi tạo cho YouTube Content Intelligence
--
-- QUY TẮC VÀNG CỦA MIGRATION (đọc trước khi sửa file này):
--   1. File migration ĐÃ CHẠY thì KHÔNG BAO GIỜ được sửa nội dung.
--      Muốn đổi schema -> tạo file MỚI 002_..., 003_...
--      Vì sao? Máy bạn đã chạy 001 rồi. Bạn sửa 001, đồng nghiệp chạy 001 bản
--      mới -> hai database khác nhau mà cùng nói "đã chạy 001". Không ai biết
--      thực tế đang ở trạng thái nào.
--   2. Migration chỉ TIẾN, không lùi (forward-only). Sai thì viết migration sửa.
--   3. Số thứ tự tăng dần, không nhảy cóc, không trùng.
--
-- CHẠY:
--   docker exec -i postgres psql -U <user> -d elt_db < sql/migrations/001_init.sql
-- =============================================================================

BEGIN;   -- Toàn bộ file chạy trong MỘT transaction: hoặc tất cả thành công,
         -- hoặc không gì cả. Không có trạng thái "nửa vời" khi lỡ tay Ctrl+C.
         -- Postgres hỗ trợ transactional DDL - MySQL thì KHÔNG (điểm khác biệt
         -- lớn, đáng nhớ khi phỏng vấn).


-- =============================================================================
-- 0. SỔ THEO DÕI MIGRATION
-- Bảng này ghi migration nào đã chạy. Đây chính là thứ mà Flyway / Alembic /
-- Liquibase làm. Ta viết tay để HIỂU CƠ CHẾ trước khi dùng công cụ.
-- =============================================================================
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- =============================================================================
-- 1. HAI SCHEMA - tách vùng "đang xử lý" khỏi vùng "đã công bố"
--
--   yti_staging : nơi dữ liệu MỚI ĐỔ VÀO, chưa qua kiểm tra chất lượng.
--                 Có thể bẩn, có thể thiếu. KHÔNG ai được đọc để làm báo cáo.
--   yti         : vùng ĐÃ CÔNG BỐ. Chỉ dữ liệu qua được quality gate mới vào.
--                 Dashboard, báo cáo, LLM chỉ đọc ở đây.
--
-- Vì sao phải tách? Vì nếu đổ thẳng vào bảng chính rồi mới kiểm tra, thì trong
-- lúc kiểm tra dữ liệu bẩn ĐÃ nằm trong bảng và dashboard ĐÃ đọc thấy. Tách ra
-- thì vùng công bố luôn sạch - "khóa cửa" chứ không phải "dọn sau khi mất trộm".
-- =============================================================================
CREATE SCHEMA IF NOT EXISTS yti;
CREATE SCHEMA IF NOT EXISTS yti_staging;


-- =============================================================================
-- 2. tracked_channels  -  DIMENSION (SCD type 1)
--
-- GRAIN: một dòng = MỘT KÊNH YouTube.
--
-- Đây là bảng DIMENSION: mô tả "cái gì/ai", ít dòng, đổi chậm.
-- SCD type 1 = khi title kênh đổi, ta GHI ĐÈ, không giữ lịch sử tên cũ.
-- (SCD type 2 sẽ giữ cả lịch sử bằng valid_from/valid_to - chưa cần ở MVP.)
--
-- Bảng này gộp 2 vai (bản 1.1 tách thành 2 bảng, đã hợp nhất ở bản 2.0):
--   - REGISTRY  : ta CHỌN theo dõi kênh nào (group_code, enabled)  <- người nhập
--   - METADATA  : YouTube nói gì về kênh đó (title, uploads_playlist_id) <- API ghi
-- =============================================================================
CREATE TABLE IF NOT EXISTS yti.tracked_channels (
    -- NATURAL KEY: dùng thẳng ID của YouTube, không tự sinh id riêng.
    -- Vì sao? Nó đã duy nhất toàn cầu, bất biến, và xuất hiện trong mọi response
    -- API. Tự sinh thêm surrogate key chỉ tạo ra một phép join vô ích.
    channel_id              TEXT PRIMARY KEY,

    -- CHECK thay cho ENUM: ENUM của Postgres rất khó sửa về sau
    -- (ALTER TYPE ... ADD VALUE không chạy được trong transaction ở nhiều bản).
    -- CHECK constraint dễ đổi bằng một migration thường.
    group_code              TEXT NOT NULL
                            CHECK (group_code IN ('books_learning', 'kids_animation')),

    source_url              TEXT,
    enabled                 BOOLEAN NOT NULL DEFAULT TRUE,

    -- Thời điểm gọi channels.list và API xác nhận kênh này có thật.
    -- Ghi lại "đã xác minh lúc nào" là kỷ luật quan trọng: mọi dữ liệu đều có
    -- hạn sử dụng, người đọc phải biết nó cũ đến mức nào.
    verified_at             TIMESTAMPTZ,

    -- Playlist "uploads" - đường vào để liệt kê video của kênh.
    -- NULL cho tới khi gọi API lần đầu. KHÔNG tự suy từ channel_id (UC->UU):
    -- đó là hành vi Google không cam kết.
    uploads_playlist_id     TEXT,

    title                   TEXT,
    description             TEXT,
    metadata_refreshed_at   TIMESTAMPTZ,

    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Định dạng channel ID: "UC" + 22 ký tự. Validate ở CẢ config.py LẪN database.
    -- Vì sao kiểm hai lần? Vì database là HÀNG RÀO CUỐI CÙNG: sau này có thể có
    -- script khác, người khác INSERT tay, hoặc một tool nạp dữ liệu - tất cả đều
    -- đi qua database. Ràng buộc đặt ở database thì KHÔNG AI lách được.
    CONSTRAINT tracked_channels_id_format CHECK (channel_id ~ '^UC[A-Za-z0-9_-]{22}$')
);

COMMENT ON TABLE yti.tracked_channels IS
    'Dimension (SCD type 1). Grain: một kênh YouTube. Gộp registry (ta chọn) + metadata (API trả).';


-- =============================================================================
-- 3. collection_runs  -  MỘT LẦN THU LÀ MỘT SỰ KIỆN CÓ TÊN
--
-- GRAIN: một dòng = MỘT LẦN CHẠY THU THẬP (cho cả 4 kênh).
--
-- Đây là bảng quan trọng nhất về mặt tư duy. Nó biến "lần chạy" từ một khái
-- niệm mơ hồ thành một THỰC THỂ CÓ ID - thứ mà mọi dữ liệu khác có thể trỏ tới.
-- =============================================================================
CREATE TABLE IF NOT EXISTS yti.collection_runs (
    -- SURROGATE KEY dạng UUID, KHÔNG dùng BIGSERIAL. Lý do rất cụ thể:
    -- đường dẫn file raw trên MinIO có chứa collection_id:
    --     youtube/resource=videos/collection_id=<id>/batch=0001.json
    -- Ta phải BIẾT id TRƯỚC KHI ghi file, tức trước khi chạm database.
    -- BIGSERIAL chỉ có giá trị SAU khi INSERT -> phải đi database một vòng rồi
    -- mới ghi được file. UUID sinh ngay trong Python, không cần hỏi ai.
    collection_id   UUID PRIMARY KEY,

    -- HAI CỘT THỜI GIAN KHÁC NHAU - đừng gộp:
    --   scheduled_for : ô lịch, ví dụ 14:00 đúng      (thời gian DỰ ĐỊNH)
    --   started_at    : thực sự bắt đầu lúc 14:03:27  (thời gian THỰC TẾ)
    -- Chênh lệch giữa hai cột này chính là ĐỘ TRỄ của scheduler - số liệu vận
    -- hành quý giá. Gộp làm một là mất luôn khả năng đo độ trễ, và là gốc rễ
    -- của bug "chạy lại cho ngày cũ nhưng dữ liệu ghi vào ngày hôm nay".
    scheduled_for   TIMESTAMPTZ NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL,
    ended_at        TIMESTAMPTZ,

    status          TEXT NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running', 'succeeded', 'failed', 'partial')),

    -- 'partial' là trạng thái RẤT quan trọng: 3/4 kênh thành công.
    -- Không được gọi là 'succeeded' (nói dối) cũng không phải 'failed' (phí dữ
    -- liệu đã lấy được). Hệ thống trung thực phải có chỗ cho "một phần".

    -- Commit hash của code lúc chạy. Sáu tháng sau nhìn dữ liệu lạ, ta biết nó
    -- được sinh bởi phiên bản code nào. Đây là "data lineage" ở mức đơn giản nhất.
    code_version    TEXT,

    notes           TEXT,

    CONSTRAINT collection_runs_time_order CHECK (ended_at IS NULL OR ended_at >= started_at)
);

CREATE INDEX IF NOT EXISTS ix_collection_runs_started
    ON yti.collection_runs (started_at DESC);

COMMENT ON TABLE yti.collection_runs IS
    'Grain: một lần chạy thu thập. scheduled_for (lịch) và started_at (thực tế) là hai cột khác nhau.';


-- =============================================================================
-- 4. channel_batches  -  TRẠNG THÁI TỪNG KÊNH TRONG MỘT LẦN THU
--
-- GRAIN: một dòng = MỘT KÊNH trong MỘT LẦN THU.
--
-- Vì sao cần bảng này? Vì "lần thu thành công" là câu hỏi sai. Câu đúng là
-- "kênh NÀO thành công trong lần thu NÀO". Một kênh lỗi không được phép làm
-- cả run báo hỏng, và cũng không được phép ẩn mình trong một run báo "OK".
-- =============================================================================
CREATE TABLE IF NOT EXISTS yti.channel_batches (
    batch_id        UUID PRIMARY KEY,

    -- ON DELETE CASCADE: xóa một collection thì mọi batch của nó biến mất theo.
    -- Hợp lý vì batch KHÔNG CÓ Ý NGHĨA nếu tách khỏi collection cha.
    collection_id   UUID NOT NULL
                    REFERENCES yti.collection_runs(collection_id) ON DELETE CASCADE,

    -- ON DELETE RESTRICT: KHÔNG cho xóa kênh nếu còn dữ liệu thu của nó.
    -- Khác biệt có chủ ý: xóa kênh khỏi registry là hành động của con người,
    -- và nó KHÔNG được phép âm thầm cuốn theo hàng nghìn bản ghi lịch sử.
    -- Bắt người dùng đối diện với việc "còn dữ liệu đấy, chắc chưa?".
    channel_id      TEXT NOT NULL
                    REFERENCES yti.tracked_channels(channel_id) ON DELETE RESTRICT,

    status          TEXT NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running', 'succeeded', 'failed')),

    -- Quality gate cho batch này. 'quarantined' = đã lấy được nhưng KHÔNG SẠCH,
    -- giữ lại để điều tra nhưng KHÔNG cho vào vùng công bố.
    quality_status  TEXT NOT NULL DEFAULT 'pending'
                    CHECK (quality_status IN ('pending', 'passed', 'quarantined')),

    -- ĐỐI SOÁT: kỳ vọng bao nhiêu, nhận được bao nhiêu.
    -- Gửi 50 id mà chỉ nhận về 48 (video vừa bị xóa/chuyển private) -> API vẫn
    -- trả HTTP 200, KHÔNG báo gì. Hai cột này biến 'silent data loss' thành
    -- con số nhìn thấy được.
    expected_count  INTEGER,
    received_count  INTEGER,
    request_count   INTEGER,          -- số request đã gọi -> ước lượng quota tiêu thụ

    -- TRUE nếu chạm trần số trang mà chưa duyệt hết playlist.
    -- Bắt buộc phải có: nếu không, ta sẽ vô tình báo cáo "toàn bộ kênh" trong
    -- khi thực tế chỉ lấy 50/1274 video của Peppa.
    discovery_truncated BOOLEAN NOT NULL DEFAULT FALSE,

    manifest_key    TEXT,             -- đường dẫn manifest trên MinIO -> để replay
    error_code      TEXT,
    error_message   TEXT,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Mỗi kênh CHỈ ĐƯỢC một batch trong một collection. Chặn lỗi gọi hai lần.
    CONSTRAINT channel_batches_uniq UNIQUE (collection_id, channel_id),
    CONSTRAINT channel_batches_counts_nonneg
        CHECK (COALESCE(expected_count, 0) >= 0 AND COALESCE(received_count, 0) >= 0)
);

-- Postgres KHÔNG tự tạo index cho cột khóa ngoại! (nhiều người tưởng có).
-- Không có index thì mỗi lần xóa/join theo channel_id phải quét toàn bảng.
CREATE INDEX IF NOT EXISTS ix_channel_batches_channel ON yti.channel_batches (channel_id);
CREATE INDEX IF NOT EXISTS ix_channel_batches_collection ON yti.channel_batches (collection_id);

COMMENT ON TABLE yti.channel_batches IS
    'Grain: một kênh trong một lần thu. Giữ trạng thái + đối soát expected/received.';


-- =============================================================================
-- 5. videos_current  -  DIMENSION (SCD type 1)
--
-- GRAIN: một dòng = MỘT VIDEO (trạng thái metadata HIỆN HÀNH).
--
-- Metadata của video (tiêu đề, thời lượng, ngày đăng) gần như không đổi ->
-- lưu một dòng, ghi đè khi refresh. Số liệu view/like THÌ ĐỔI LIÊN TỤC ->
-- nằm ở bảng khác (video_observations).
--
-- TÁCH CÁI ĐỔI CHẬM KHỎI CÁI ĐỔI NHANH - đây là nguyên tắc nền của
-- dimensional modeling. Nếu nhét chung, mỗi lần thu phải lặp lại cả title và
-- description dài hàng nghìn ký tự -> phình database vô ích.
-- =============================================================================
CREATE TABLE IF NOT EXISTS yti.videos_current (
    video_id            TEXT PRIMARY KEY,
    channel_id          TEXT NOT NULL
                        REFERENCES yti.tracked_channels(channel_id) ON DELETE RESTRICT,

    title               TEXT NOT NULL,
    description         TEXT,

    -- TIMESTAMPTZ chứ không phải TIMESTAMP. Khác biệt sống còn:
    --   TIMESTAMP   : "14:00" - không biết 14:00 ở đâu. Vô nghĩa khi có nhiều múi giờ.
    --   TIMESTAMPTZ : lưu mốc thời gian TUYỆT ĐỐI (thực chất là UTC), hiển thị
    --                 theo múi giờ người xem.
    -- QUY TẮC: lưu UTC, đổi múi giờ CHỈ lúc hiển thị. Không bao giờ lưu giờ địa phương.
    published_at        TIMESTAMPTZ NOT NULL,

    -- BIGINT GIÂY, KHÔNG dùng kiểu TIME. Đây là bug đã chứng minh ở repo cũ:
    --   'P1DT2H3M4S' (1 ngày 2 giờ...) -> kiểu TIME cho ra '02:03:04', MẤT phần ngày.
    -- Nguyên nhân sâu: TIME là "thời điểm trong ngày", thứ ta cần là "khoảng thời
    -- gian". Hai khái niệm khác nhau. Livestream 26 tiếng là có thật.
    duration_seconds    BIGINT CHECK (duration_seconds IS NULL OR duration_seconds >= 0),

    thumbnail_url       TEXT,
    category_id         TEXT,

    -- NULL = KHÔNG BIẾT, khác hẳn 'unknown' hay ''. Đã kiểm chứng thật:
    -- cả 4 kênh pilot đều KHÔNG trả về defaultLanguage.
    default_language    TEXT,

    -- Từ snippet.liveBroadcastContent. Đây là NGUỒN SỰ THẬT để biết livestream,
    -- thay cho việc suy đoán từ duration == 0 (bug cũ: livestream 3 tiếng bị
    -- xếp là "Shorts").
    live_state          TEXT CHECK (live_state IS NULL OR live_state IN ('none', 'live', 'upcoming')),

    -- BOOLEAN NULL-able = BA TRẠNG THÁI: true / false / CHƯA BIẾT.
    -- Tuyệt đối không ép NULL thành FALSE: "không biết có phải nội dung trẻ em
    -- không" khác hoàn toàn "chắc chắn không phải".
    made_for_kids       BOOLEAN,

    -- format_label là KẾT LUẬN; format_evidence là LÝ DO dẫn tới kết luận đó.
    -- Luôn lưu cả hai. Sáu tháng sau nhìn thấy nhãn lạ, ta cần biết vì sao hệ
    -- thống kết luận như vậy. Đây là "explainability" ở tầng dữ liệu.
    format_label        TEXT NOT NULL DEFAULT 'unknown'
                        CHECK (format_label IN ('shorts', 'normal', 'live', 'unknown')),
    format_evidence     TEXT,

    first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    refreshed_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT videos_current_id_len CHECK (char_length(video_id) BETWEEN 5 AND 32)
);

CREATE INDEX IF NOT EXISTS ix_videos_current_channel   ON yti.videos_current (channel_id);
CREATE INDEX IF NOT EXISTS ix_videos_current_published ON yti.videos_current (published_at DESC);

COMMENT ON TABLE yti.videos_current IS
    'Dimension (SCD type 1). Grain: một video, metadata hiện hành. Số liệu đổi nhanh nằm ở video_observations.';


-- =============================================================================
-- 6. video_observations  -  ⭐ FACT TABLE - TRÁI TIM CỦA CẢ HỆ THỐNG
--
-- GRAIN: một dòng = SỐ LIỆU CỦA MỘT VIDEO TẠI MỘT LẦN THU.
--
-- Đây là bảng FACT: nhiều dòng, chỉ thêm không sửa (append-only), chứa SỐ ĐO
-- kèm các khóa trỏ tới dimension.
--
-- KHÓA DUY NHẤT LÀ (video_id, collection_id) - KHÔNG phải (video_id, ngày):
--   - Thu 2 lần/ngày -> hai collection khác nhau -> hai dòng, không đâm nhau ✅
--   - Chạy LẠI cùng một collection (retry/replay) -> ON CONFLICT -> cập nhật
--     đúng dòng đó, không sinh bản trùng ✅  <- đây chính là IDEMPOTENCY
-- =============================================================================
CREATE TABLE IF NOT EXISTS yti.video_observations (
    -- BIGSERIAL ở đây (khác collection_id dùng UUID). Vì sao khác nhau?
    --   collection_id : phải biết TRƯỚC khi ghi file raw -> UUID sinh ở Python
    --   observation_id: chỉ cần sau khi INSERT, và bảng này rất nhiều dòng ->
    --                   BIGSERIAL (8 byte) gọn hơn UUID (16 byte) và index nhanh hơn.
    -- Chọn kiểu khóa theo NHU CẦU, không theo thói quen.
    observation_id  BIGSERIAL PRIMARY KEY,

    video_id        TEXT NOT NULL,
    collection_id   UUID NOT NULL
                    REFERENCES yti.collection_runs(collection_id) ON DELETE CASCADE,
    batch_id        UUID NOT NULL
                    REFERENCES yti.channel_batches(batch_id) ON DELETE CASCADE,

    -- observed_at = thời điểm THỰC SỰ gọi API lấy con số này.
    -- KHÔNG phải scheduled_for, KHÔNG phải thời điểm INSERT vào database.
    -- Mọi phép tính tốc độ tăng trưởng đều dựa vào cột này, nên nó phải là
    -- thời điểm CON SỐ ĐƯỢC ĐỌC, không phải thời điểm nó được lưu.
    observed_at     TIMESTAMPTZ NOT NULL,

    -- ⚠️ CẢ BA ĐỀU NULL-ABLE, VÀ ĐÓ LÀ CỐ Ý.
    -- Đã kiểm chứng thật trên MrBeast: 7/1000 video thiếu likeCount,
    -- 1/1000 thiếu commentCount (tác giả ẩn like / tắt bình luận).
    -- NULL = "YouTube không cho biết". 0 = "có đúng 0 lượt thích".
    -- Ép NULL thành 0 là BỊA DỮ LIỆU: nó kéo giá trị trung bình xuống và làm
    -- sai mọi bảng xếp hạng. Đây là lỗi phân tích phổ biến nhất mà người mới mắc.
    view_count      BIGINT CHECK (view_count    IS NULL OR view_count    >= 0),
    like_count      BIGINT CHECK (like_count    IS NULL OR like_count    >= 0),
    comment_count   BIGINT CHECK (comment_count IS NULL OR comment_count >= 0),

    -- Video có trong danh sách nhưng API không trả về -> ghi LÝ DO.
    -- KHÔNG được biến thành view = 0, cũng KHÔNG khẳng định "đã xóa vĩnh viễn"
    -- chỉ từ một lần vắng mặt.
    unavailable_reason  TEXT,

    -- Trỏ ngược về file raw trên MinIO đã sinh ra dòng này.
    -- Đây là DATA LINEAGE: từ bất kỳ con số nào trong database, truy ngược được
    -- về đúng byte JSON mà YouTube đã trả. Khi có tranh cãi "số này ở đâu ra",
    -- đây là câu trả lời.
    source_object_key   TEXT,

    inserted_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- ⭐ RÀNG BUỘC QUAN TRỌNG NHẤT CỦA CẢ SCHEMA.
    -- Nó vừa định nghĩa GRAIN, vừa là cơ chế bảo đảm IDEMPOTENCY (qua ON CONFLICT).
    CONSTRAINT video_observations_grain UNIQUE (video_id, collection_id)
);

-- Index cho câu hỏi chính: "lấy các observation của video X theo thứ tự thời gian"
-- -> dùng để tính delta_views giữa hai lần quan sát.
CREATE INDEX IF NOT EXISTS ix_obs_video_time  ON yti.video_observations (video_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS ix_obs_collection  ON yti.video_observations (collection_id);
CREATE INDEX IF NOT EXISTS ix_obs_batch       ON yti.video_observations (batch_id);

COMMENT ON TABLE yti.video_observations IS
    'FACT table. Grain: một video trong một lần thu. UNIQUE(video_id, collection_id) - KHÔNG dùng ngày.';


-- =============================================================================
-- 7. reports
-- GRAIN: một dòng = MỘT BÁO CÁO cho MỘT NHÓM tại MỘT MỐC THỜI GIAN.
-- =============================================================================
CREATE TABLE IF NOT EXISTS yti.reports (
    report_id       UUID PRIMARY KEY,
    group_code      TEXT NOT NULL
                    CHECK (group_code IN ('books_learning', 'kids_animation')),
    as_of           TIMESTAMPTZ NOT NULL,

    status          TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft', 'validated', 'published', 'failed')),

    -- JSONB chứ không phải JSON. JSON lưu nguyên văn chuỗi; JSONB lưu dạng nhị
    -- phân đã phân tích -> truy vấn được, index được (GIN), so sánh được.
    -- Gần như luôn chọn JSONB.
    coverage            JSONB,      -- kênh nào đủ dữ liệu, kênh nào thiếu
    evidence_bundle     JSONB,      -- dữ liệu ĐƯA CHO LLM (để tái lập được)
    validated_payload   JSONB,      -- kết quả LLM ĐÃ qua validator (gồm cả suggestions)

    -- Lưu model + prompt version: cùng một evidence, hai model cho hai kết quả.
    -- Không lưu thì không bao giờ giải thích được vì sao báo cáo hôm nay khác hôm qua.
    model_name      TEXT,
    prompt_version  TEXT,
    input_hash      TEXT,
    tokens_used     INTEGER,
    latency_ms      INTEGER,

    -- Vòng đời dữ liệu: chính sách YouTube yêu cầu refresh/xóa theo thời hạn.
    -- Ngày cụ thể để ở CONFIG, không hardcode - và phải đọc [S7] trước khi đặt số.
    expiry_at       TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT reports_uniq UNIQUE (group_code, as_of)
);

CREATE INDEX IF NOT EXISTS ix_reports_group_time ON yti.reports (group_code, as_of DESC);


-- =============================================================================
-- 8. VÙNG STAGING - nơi dữ liệu đáp xuống TRƯỚC khi qua quality gate
--
-- Không ràng buộc chặt như vùng công bố: dữ liệu bẩn PHẢI VÀO ĐƯỢC ĐÂY thì mới
-- kiểm tra được nó bẩn ở đâu. Ràng buộc chặt ở staging = dữ liệu lỗi bị chặn
-- ngay lúc insert và ta không bao giờ nhìn thấy nó để điều tra.
-- =============================================================================
CREATE TABLE IF NOT EXISTS yti_staging.video_observations_stg (
    stg_id          BIGSERIAL PRIMARY KEY,
    batch_id        UUID NOT NULL,
    collection_id   UUID NOT NULL,
    channel_id      TEXT NOT NULL,
    video_id        TEXT,

    -- Ở staging, các cột số để TEXT: API trả về chuỗi ("1234"), và nếu có giá trị
    -- không parse được thì ta vẫn GIỮ NGUYÊN để điều tra, thay vì gãy lúc insert.
    -- Ép kiểu là việc của bước chuyển từ staging sang vùng công bố.
    raw_view_count      TEXT,
    raw_like_count      TEXT,
    raw_comment_count   TEXT,
    raw_duration        TEXT,
    raw_published_at    TEXT,
    title               TEXT,
    live_broadcast      TEXT,

    observed_at     TIMESTAMPTZ NOT NULL,
    source_object_key TEXT,
    loaded_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_obs_stg_batch ON yti_staging.video_observations_stg (batch_id);

COMMENT ON TABLE yti_staging.video_observations_stg IS
    'Vùng đáp. Cột số để TEXT có chủ ý: giữ được giá trị lỗi để điều tra thay vì gãy lúc insert.';


-- =============================================================================
-- GHI NHẬN MIGRATION ĐÃ CHẠY
-- =============================================================================
INSERT INTO schema_migrations (version) VALUES ('001_init')
ON CONFLICT (version) DO NOTHING;

COMMIT;
