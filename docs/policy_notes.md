# Ghi chú chính sách YouTube API

> **Trạng thái: ✅ đã đọc nguồn gốc ngày 2026-10-31** (đọc bằng công cụ tải trang trong phiên
> làm việc với Claude Code; trích dẫn bên dưới là nguyên văn). Người vận hành nên tự mở lại
> từng trang và kiểm tra trước khi đặt con số vào config. Đây là ghi chú kỹ thuật, **không phải
> tư vấn pháp lý**.
>
> Ngày cập nhật hiển thị trên trang lúc đọc: Developer Policies 2026-10-15 · Derived Metrics
> Policy 2026-09-14 · Quota cost 2026-10-15 · videos.insert 2026-10-15.

## Vì sao file này tồn tại

Hệ thống lưu dữ liệu từ YouTube Data API: raw JSON trên MinIO, số liệu trong Postgres.
YouTube có điều khoản về **lưu trữ bao lâu** và **được tính chỉ số dẫn xuất gì**.
Code KHÔNG được đặt con số theo trí nhớ hay theo tài liệu thứ cấp — sai thì hoặc vi
phạm điều khoản, hoặc xóa mất dữ liệu không lấy lại được.

**Quy trình:** đọc nguồn gốc → trích nguyên văn vào đây kèm ngày đọc → rồi mới đặt số
trong config. Không có trích dẫn = không có con số. Đọc lại mỗi quý (đặc tả mục 16).

## Trả lời

| # | Câu hỏi | Trả lời (trích nguyên văn) | Nguồn |
|---|---|---|---|
| 1 | Dữ liệu API lưu tối đa bao lâu? | Non-Authorized Data: *"may temporarily store limited amounts of Non-Authorized Data for as long as is necessary ... but not longer than 30 calendar days."* (III.E.4.d). Authorized Data khác: *"for no longer than 30 calendar days. After 30 calendar days, the API Client must either delete or refresh the stored data."* (III.E.4.c) | [S7] |
| 2 | Thống kê có khác metadata không? | Thống kê công khai: *"an API Client must not store statistics retrieved as Non-Authorized Data for more than 30 days."* (III.E.4.b). Chỉ Authorized Data (kênh của mình qua OAuth) mới được lưu thống kê lâu hơn, và *"the Client must still ensure every 30 days that it is still authorized by the user to access that data."* | [S7] |
| 3 | `views_per_hour` có phải "derived metric"? | Mặc định cấm: *"Your API Clients must not (i) replace API Data with similar, independently calculated data, or (ii) access or use API Data to create new or derived data or metrics."* (III.E.4.h). Ví dụ cấm: *"a score that factors in likes, total views, or any other API Data."* Ngoại lệ chỉ khi chấp nhận phần sửa đổi chính sách: *"subject to your acceptance of the amendment to the Developer Policies ... YouTube permits the calculation of specific additional metrics"*; và *"You must distinguish these metrics from metrics sourced from API Data."* | [S7], [S5] |
| 4 | Được xếp hạng video/kênh của người khác không? | Chỉ trong use case đã được chấp thuận: *"You may rank channel performance or track views between different channels for the purpose of comparison or historical analysis."* (Derived Metrics Policy, mục 5). III.L: *"These policies are only applicable to audited developers with analytics use cases that have explicitly applied for permission ..."* | [S5], [S7] |
| 5 | Xóa theo yêu cầu người dùng trong bao lâu? | *"If the user indicates that you should delete that data, you must then delete it as soon as possible and within 7 calendar days."* (III.E.4.g) | [S7] |
| 6 | Có ngoại lệ lưu vô thời hạn cho ID (video_id, channel_id) không? | **Không tìm thấy** ngoại lệ nào cho ID. Nguyên tắc II.4: *"Don't store user data indefinitely ..."* | [S7] |
| 7 | Gộp dữ liệu nhiều kênh? | *"Do not aggregate API Data except that you may only aggregate API Data relating to YouTube channels that are under the same content owner ..."* và *"Do not aggregate API Data or otherwise use API Data or YouTube API Services to gain insights into YouTube's usage, revenue, or any other aspects of YouTube's business."* (III.E.2) | [S7] |
| 8 | Lưu thống kê lâu hơn khi được chấp thuận? | *"... allow accepted API Clients to store metrics (e.g., views, likes, subs count, comment counts) from statistical endpoints for up to 36 calendar months."*; *"Derived metrics (such as sentiment analysis) ... may also be stored for up to 36 calendar months."*; *"Other data (such as video titles, creator names, descriptions, and comment text) must still follow the 30-day refresh and deletion policy"*. Embedding và tóm tắt: **trang không nói** — đặc tả xếp chúng theo hạn của văn bản gốc (30 ngày). | [S5] |
| 9 | Quota | *"Projects that enable the YouTube Data API have a default quota allocation of 100 `search.list` calls, 100 `videos.insert` calls, and 10,000 units per day combined for all other endpoints."*; *"The quota cost is 1 per call."*; *"Daily quotas reset at midnight Pacific Time (PT)."* | [S8] |
| 10 | Upload qua API | `status.privacyStatus`: *"If you don't specify a value for this property, the uploaded video's privacy status defaults to `public`."*; *"Videos uploaded from unverified API projects are not restricted to private viewing mode."* `status.containsSyntheticMedia` và `status.selfDeclaredMadeForKids` chỉ được liệt kê tên, **trang không mô tả**. | [S9] |

## Hệ quả cho hệ thống

- **Chế độ cơ bản (mặc định):** `derived_metrics_enabled: false`. Không tính, không lưu, không
  trả chỉ số tự tính ngoài mức tăng view của từng video; không xếp hạng hay so sánh kênh.
  Đây là cách phân ranh thận trọng của đặc tả (R2), vì III.E.4.h cấm cả "derived data" nói
  chung: **ngay cả `views_per_hour` của một video cũng là số tự tính**. Đặc tả chấp nhận nó ở chế
  độ cơ bản với điều kiện luôn đứng cạnh số thô và ghi "tính độc lập". Người dùng nên tự đánh giá
  rủi ro này (xem câu hỏi mở bên dưới).
- **Lưu trữ (R3):** dữ liệu API công khai (raw, thống kê, metadata, văn bản bình luận và embedding
  suy ra từ chúng) xóa hoặc làm mới sau tối đa 30 ngày; xóa theo yêu cầu trong 7 ngày.
- **Gộp dữ liệu (III.E.2):** tín hiệu S4 (đếm số kênh khác nhau trong một cụm chủ đề) gộp dữ liệu
  của nhiều chủ sở hữu nội dung — có thể vướng III.E.2 kể cả khi cờ bật. Chỉ triển khai sau cờ và
  phải được người dùng quyết định (đặc tả mục 8.2, P1).
- **Upload:** luôn gửi `privacyStatus = private` (P7).

## Câu hỏi mở cho người dùng

1. Chế độ cơ bản vẫn hiển thị `views_per_hour`/`delta_views` (theo đặc tả). III.E.4.h đọc sát chữ
   thì cấm cả số này nếu chưa được chấp thuận. Giữ như đặc tả, hay tắt luôn khi chưa có chấp thuận?
2. Nhãn RAG eval (`evals/rag_queries.yaml`) và các file eval lưu `video_id` của kênh người khác
   vô thời hạn trong git. Không có ngoại lệ cho ID (câu 6). Chấp nhận, hay đổi cách lưu?

## Nguồn

- **[S5]** Derived Metrics Policy — https://developers.google.com/youtube/terms/derived-metrics-policy
- **[S7]** Developer Policies — https://developers.google.com/youtube/terms/developer-policies
- **[S8]** Quota cost — https://developers.google.com/youtube/data/determine_quota_cost
- **[S9]** videos.insert — https://developers.google.com/youtube/v3/docs/videos/insert
