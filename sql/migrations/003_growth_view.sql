-- =============================================================================
-- 003_growth_view.sql  -  VIEW tính tăng trưởng
--
-- VÌ SAO LÀ VIEW MÀ KHÔNG PHẢI BẢNG?
-- Bản brief 1.1 định tạo bảng `video_metrics`. Bản 2.0 cắt xuống thành VIEW.
-- Lý do:
--   - 200 video thì tính lại tức thì, không cần lưu sẵn.
--   - Bảng vật lý đẻ ra BA vấn đề mới: phải version công thức, phải backfill
--     khi đổi công thức, và dữ liệu có thể bị cũ (stale).
--   - VIEW luôn phản ánh dữ liệu MỚI NHẤT, sửa công thức là có hiệu lực ngay.
-- Chỉ vật lý hóa KHI ĐO ĐƯỢC là chậm. Đây là kỷ luật "đừng tối ưu trước khi đo".
-- Nếu sau này cần: CREATE TABLE ... AS SELECT * FROM yti.v_video_growth;
--
-- TRIẾT LÝ CỦA VIEW NÀY: KHÔNG GIẤU DỮ LIỆU THIẾU.
-- Mọi video đều xuất hiện trong kết quả, kèm cột validity_reason nói rõ vì sao
-- tính được hay không. Lọc là việc của người ĐỌC, không phải của VIEW.
-- View mà tự lọc thì người đọc không bao giờ biết mình đang thiếu bao nhiêu.
-- =============================================================================

BEGIN;

-- Cửa sổ mục tiêu 24 giờ (khớp snapshot_hours=24), chấp nhận 18-30 giờ.
-- Vì sao cần biên? Vì lần chạy thực tế không bao giờ đúng từng giây: scheduler
-- trễ, retry, máy tắt muộn. Đòi đúng 24h thì gần như không video nào hợp lệ.
-- Vì sao không nới rộng hơn? Vì so sánh "tăng trong 20 giờ" với "tăng trong
-- 72 giờ" là so hai thứ khác nhau -> bảng xếp hạng vô nghĩa.
CREATE OR REPLACE VIEW yti.v_video_growth AS
WITH latest AS (
    -- Quan sát MỚI NHẤT của mỗi video.
    -- DISTINCT ON là cú pháp riêng của Postgres: lấy dòng đầu tiên của mỗi
    -- nhóm sau khi ORDER BY. Gọn hơn ROW_NUMBER() + lọc rn = 1.
    SELECT DISTINCT ON (video_id)
           video_id, observation_id, observed_at, view_count, like_count,
           comment_count, collection_id
      FROM yti.video_observations
     ORDER BY video_id, observed_at DESC
),
paired AS (
    SELECT
        l.video_id,
        l.observation_id  AS end_observation_id,
        l.observed_at     AS end_observed_at,
        l.view_count      AS end_view_count,
        l.like_count      AS end_like_count,
        l.comment_count   AS end_comment_count,
        e.observation_id  AS start_observation_id,
        e.observed_at     AS start_observed_at,
        e.view_count      AS start_view_count
    FROM latest l
    -- LEFT JOIN LATERAL: với MỖI dòng của `latest`, chạy truy vấn con bên phải.
    -- Giống một vòng lặp for trong SQL - truy vấn con ĐƯỢC PHÉP tham chiếu l.
    -- LEFT (thay vì INNER) để video chỉ có 1 quan sát VẪN xuất hiện, với
    -- start_* = NULL -> ta gán nhãn insufficient_history thay vì làm nó BIẾN MẤT.
    LEFT JOIN LATERAL (
        SELECT o.observation_id, o.observed_at, o.view_count
          FROM yti.video_observations o
         WHERE o.video_id = l.video_id
           AND o.observed_at < l.observed_at
         -- Chọn quan sát GẦN MỐC 24 GIỜ TRƯỚC NHẤT, không phải quan sát liền kề.
         -- Vì sao? Nếu hôm qua bạn bấm Trigger tay 3 lần trong 10 phút, quan sát
         -- liền kề chỉ cách 10 phút -> delta nhiễu, chia cho 0.17 giờ ra số vô lý.
         -- Sắp theo |khoảng cách - 24h| rồi lấy cái nhỏ nhất = gần 24h nhất.
         ORDER BY abs(EXTRACT(EPOCH FROM (l.observed_at - o.observed_at)) - 86400)
         LIMIT 1
    ) e ON TRUE
),
computed AS (
    SELECT
        p.*,
        -- Chia cho 3600 để ra giờ. ::numeric vì EXTRACT trả double precision,
        -- mà ROUND(double, int) KHÔNG tồn tại trong Postgres (bài học Bước 3).
        ROUND((EXTRACT(EPOCH FROM (p.end_observed_at - p.start_observed_at)) / 3600)::numeric, 2)
            AS elapsed_hours,
        p.end_view_count - p.start_view_count AS delta_views
    FROM paired p
)
SELECT
    v.video_id,
    v.channel_id,
    c.group_code,
    c.title                AS channel_title,
    v.title                AS video_title,
    v.published_at,
    v.duration_seconds,
    v.format_label,
    v.live_state,
    v.made_for_kids,

    g.start_observation_id,
    g.end_observation_id,
    g.start_observed_at,
    g.end_observed_at,
    g.start_view_count,
    g.end_view_count,
    g.end_like_count,
    g.end_comment_count,
    g.elapsed_hours,
    g.delta_views,

    -- views_per_hour CHỈ tính khi cửa sổ hợp lệ. Ngoài ra để NULL.
    -- NULL ở đây nghĩa là "KHÔNG TÍNH ĐƯỢC", khác hẳn 0 = "không tăng chút nào".
    CASE
        WHEN g.delta_views IS NULL OR g.elapsed_hours IS NULL THEN NULL
        WHEN g.elapsed_hours <= 0 THEN NULL
        ELSE ROUND((g.delta_views / g.elapsed_hours)::numeric, 2)
    END AS views_per_hour,

    -- ⭐ CỘT QUAN TRỌNG NHẤT CỦA VIEW NÀY.
    -- Nói rõ VÌ SAO một video tính được hay không tính được.
    -- Thứ tự CASE quan trọng: kiểm tra từ nguyên nhân CƠ BẢN nhất trở đi.
    CASE
        WHEN g.start_observation_id IS NULL
            THEN 'insufficient_history'      -- mới có 1 quan sát
        WHEN g.start_view_count IS NULL OR g.end_view_count IS NULL
            THEN 'missing_view_count'        -- YouTube ẩn số liệu
        WHEN g.elapsed_hours < 18
            THEN 'window_too_short'          -- hai lần thu quá gần nhau
        WHEN g.elapsed_hours > 30
            THEN 'window_too_long'           -- có khoảng trống (máy tắt?)
        WHEN g.delta_views < 0
            THEN 'view_count_decreased'      -- YouTube điều chỉnh/lọc view giả
        ELSE 'ok'
    END AS validity_reason,

    -- Quan sát cuối đã quá cũ chưa? Lịch 24h -> quá 30h là bất thường.
    (g.end_observed_at < now() - interval '30 hours') AS is_stale,

    -- Có được phép lên bảng xếp hạng không?
    -- Gom mọi điều kiện vào MỘT cột boolean để tầng trên (dashboard, báo cáo)
    -- không phải tự viết lại logic - và không thể viết SAI khác nhau ở mỗi chỗ.
    (
        g.start_observation_id IS NOT NULL
        AND g.start_view_count IS NOT NULL AND g.end_view_count IS NOT NULL
        AND g.elapsed_hours BETWEEN 18 AND 30
        AND g.delta_views >= 0
        -- Livestream KHÔNG được trộn vào bảng xếp hạng video thường:
        -- chúng tăng view theo cơ chế hoàn toàn khác.
        AND COALESCE(v.live_state, 'none') = 'none'
        AND g.end_observed_at >= now() - interval '30 hours'
    ) AS is_rankable

FROM yti.videos_current v
JOIN yti.tracked_channels c USING (channel_id)
-- LEFT JOIN: video chưa có quan sát nào VẪN hiện ra (với mọi cột g.* là NULL).
-- Giấu chúng đi sẽ khiến người đọc tưởng mình có đủ dữ liệu cho mọi video.
LEFT JOIN computed g USING (video_id);

COMMENT ON VIEW yti.v_video_growth IS
    'Tăng trưởng view giữa hai quan sát cách nhau ~24h. Grain: một video. '
    'KHÔNG lọc sẵn - dùng validity_reason và is_rankable để lọc ở tầng trên.';

INSERT INTO schema_migrations (version) VALUES ('003_growth_view')
ON CONFLICT (version) DO NOTHING;

COMMIT;
