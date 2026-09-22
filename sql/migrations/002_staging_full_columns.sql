-- =============================================================================
-- 002_staging_full_columns.sql
--
-- VÌ SAO CÓ FILE NÀY THAY VÌ SỬA 001_init.sql?
-- Vì 001 ĐÃ CHẠY trên database. Sửa nó sẽ tạo ra tình trạng:
--   máy A: đã chạy 001 (bản cũ)
--   máy B: đã chạy 001 (bản mới)
--   cả hai đều khai "đã chạy 001" nhưng schema KHÁC NHAU.
-- Không ai biết database thật đang ở trạng thái nào -> mất kiểm soát hoàn toàn.
-- Quy tắc: migration đã chạy là BẤT BIẾN. Muốn đổi -> file mới, số tăng dần.
--
-- VẤN ĐỀ CẦN SỬA:
-- Bảng staging thiếu 5 cột mà yti.videos_current cần (description, thumbnail,
-- category, language, made_for_kids). Thiếu chúng thì bước publish buộc phải
-- quay lại đọc MinIO -> HAI NGUỒN SỰ THẬT cho cùng một bước, và replay từ
-- staging sẽ cho kết quả khác replay từ raw.
--
-- NGUYÊN TẮC: staging phải mang ĐỦ mọi thứ tầng dưới cần. Nếu không, nó không
-- phải staging mà chỉ là một bản sao thiếu hụt.
--
-- CHẠY:
--   docker exec -i postgres psql -U <user> -d elt_db < sql/migrations/002_staging_full_columns.sql
-- =============================================================================

BEGIN;

-- ADD COLUMN IF NOT EXISTS: chạy lại file này lần nữa cũng không lỗi (idempotent).
-- Thêm cột nullable KHÔNG có DEFAULT là thao tác RẤT NHANH ở Postgres 11+:
-- chỉ đổi metadata, không viết lại từng dòng dữ liệu. (Thêm cột NOT NULL kèm
-- DEFAULT trên bảng hàng trăm triệu dòng thì mới là chuyện lớn - nó phải
-- rewrite cả bảng và giữ lock.)
ALTER TABLE yti_staging.video_observations_stg
    ADD COLUMN IF NOT EXISTS raw_description       TEXT,
    ADD COLUMN IF NOT EXISTS raw_thumbnail_url     TEXT,
    ADD COLUMN IF NOT EXISTS raw_category_id       TEXT,
    ADD COLUMN IF NOT EXISTS raw_default_language  TEXT,
    ADD COLUMN IF NOT EXISTS raw_made_for_kids     TEXT;

-- Vì sao made_for_kids để TEXT ở staging mà BOOLEAN ở published?
-- Staging phải NHẬN ĐƯỢC mọi giá trị, kể cả thứ không phải boolean
-- (ví dụ YouTube đổi sang trả "unknown"). Nếu khai BOOLEAN, giá trị lạ sẽ làm
-- gãy INSERT và ta mất luôn bằng chứng để điều tra.
-- Ép kiểu là việc của biên giới publish, không phải của staging.

COMMENT ON COLUMN yti_staging.video_observations_stg.raw_made_for_kids IS
    'TEXT có chủ ý: staging nhận mọi giá trị; ép sang BOOLEAN ở bước publish.';

INSERT INTO schema_migrations (version) VALUES ('002_staging_full_columns')
ON CONFLICT (version) DO NOTHING;

COMMIT;
