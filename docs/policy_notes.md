# Ghi chú chính sách YouTube API

> **Trạng thái: ⚠️ CHƯA XÁC MINH.** Chưa ai đọc tài liệu gốc và ghi trích dẫn.
> Vì vậy `raw_retention_days: null` trong `config/channels.yaml` (= không xóa raw),
> và `derived_metrics_enabled: false`.

## Vì sao file này tồn tại

Hệ thống lưu dữ liệu từ YouTube Data API: raw JSON trên MinIO, số liệu trong Postgres.
YouTube có điều khoản về **lưu trữ bao lâu** và **được tính chỉ số dẫn xuất gì**.
Code KHÔNG được đặt con số theo trí nhớ hay theo tài liệu thứ cấp — sai thì hoặc vi
phạm điều khoản, hoặc xóa mất dữ liệu không lấy lại được.

**Quy trình:** đọc nguồn gốc → trích nguyên văn vào đây kèm ngày đọc → rồi mới đặt số
trong config. Không có trích dẫn = không có con số.

## Cần trả lời

| # | Câu hỏi | Nguồn | Trả lời (trích nguyên văn) | Ngày đọc |
|---|---|---|---|---|
| 1 | Dữ liệu API lưu tối đa bao lâu thì phải refresh hoặc xóa? | [S7] | _chưa đọc_ | |
| 2 | Quy định có khác nhau giữa metadata và statistics không? | [S7] | _chưa đọc_ | |
| 3 | Tính `views_per_hour` có phải "derived metric" cần điều kiện riêng? | [S5] | _chưa đọc_ | |
| 4 | Được hiển thị xếp hạng video của kênh người khác không? | [S5], [S7] | _chưa đọc_ | |
| 5 | Có yêu cầu hiển thị ghi công (attribution) YouTube không? | [S7] | _chưa đọc_ | |

## Nguồn

- **[S5]** Derived metrics policy — https://developers.google.com/youtube/terms/derived-metrics-policy
- **[S7]** Developer Policies — https://developers.google.com/youtube/terms/developer-policies

## Sau khi đọc xong

1. Điền bảng trên, **trích nguyên văn** câu liên quan.
2. Đặt `raw_retention_days` trong `config/channels.yaml` (chừa biên an toàn vài ngày).
3. Chạy `python -m youtube_intel.cli retention` (xem trước) rồi mới `--apply`.
4. Đổi trạng thái đầu file thành ✅ kèm ngày.
