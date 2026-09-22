# Data Contract — YT_ELT

> Tài liệu này viết TRƯỚC khi code. Nó là "hợp đồng" giữa nguồn (YouTube API)
> và kho dữ liệu (core.yt_api). Mọi thay đổi schema phải cập nhật file này.

---

## 1. Mục đích nghiệp vụ

| Câu hỏi | Trả lời |
|---|---|
| Ai dùng? | Analyst theo dõi hiệu suất kênh YouTube |
| Trả lời câu hỏi gì? | Video nào tăng trưởng view/like nhanh nhất theo thời gian? Shorts hay video dài hiệu quả hơn? |
| **Grain của bảng cuối** | **1 dòng = 1 video × 1 ngày chụp số liệu** |
| Freshness (SLA) | Cập nhật 1 lần/ngày, chậm nhất 15:00 giờ địa phương |
| Hậu quả nếu chết 1 ngày | **Mất vĩnh viễn** — YouTube API KHÔNG trả về số liệu quá khứ |

> Vì hậu quả là "mất vĩnh viễn", pipeline bắt buộc phải có: retry nhiều tầng,
> alerting khi fail, và khả năng chạy lại (idempotent).

---

## 2. Nguồn: YouTube Data API v3

**Auth:** API key (query param `key=`). Không cần OAuth vì chỉ đọc dữ liệu công khai.

**Quota:** 10.000 units/ngày, reset 00:00 Pacific Time.

| Endpoint | Giá | Số call/lần chạy (~900 video) | Tổng |
|---|---|---|---|
| `channels.list` | 1 | 1 | 1 |
| `playlistItems.list` | 1 | ceil(900/50) = 18 | 18 |
| `videos.list` | 1 | ceil(900/50) = 18 | 18 |
| **Tổng mỗi lần chạy** | | | **≈ 37 units** |

- Dư địa: 10.000 / 37 ≈ **270 lần chạy/ngày**.
- Giới hạn scale: ~270 kênh cỡ MrBeast/ngày.
- **Tránh `search.list`** (100 units/call — đắt gấp 100 lần).

**Đường đi bắt buộc** (API không cho lấy thẳng video của kênh):

```
channels.list(forHandle)  ->  uploads playlist ID
playlistItems.list(playlistId, maxResults=50, pageToken)  ->  danh sách video ID  [PHÂN TRANG]
videos.list(id=<tối đa 50 id>)  ->  chi tiết video        [BATCH 50]
```

---

## 3. Schema nguồn — `videos.list`

| Trường JSON | Kiểu THẬT trả về | Có thể thiếu? | Ghi chú |
|---|---|---|---|
| `id` | string (11 ký tự) | Không | Khóa tự nhiên |
| `snippet.title` | string | Không | Có thể chứa emoji, xuống dòng |
| `snippet.publishedAt` | string RFC3339 UTC (`2024-01-01T00:00:00Z`) | Không | Luôn là UTC |
| `contentDetails.duration` | string ISO-8601 duration (`PT26M10S`) | Không | ⚠️ xem mục 4 |
| `statistics.viewCount` | **string** (`"1234"`), không phải number | **CÓ** | Thiếu nếu kênh ẩn số liệu |
| `statistics.likeCount` | **string** | **CÓ** | Thiếu nếu tác giả ẩn like |
| `statistics.commentCount` | **string** | **CÓ** | Thiếu nếu tắt bình luận |

### Ba cạm bẫy phải nhớ

1. **Số đếm trả về dạng STRING, không phải number.** JSON là `"viewCount": "381000000"`.
   Nguyên nhân: view có thể vượt 2^53, JavaScript sẽ mất chính xác nếu để number.
   → Trong Python phải ép `int()`, trong Postgres phải là `BIGINT` (không phải `INT`).

2. **`statistics.*` có thể VẮNG MẶT hoàn toàn**, không phải bằng `null`.
   → Bắt buộc dùng `.get("likeCount", None)`, không được `["likeCount"]`.

3. **`videos.list` gửi 50 id có thể trả về ÍT hơn 50 item.**
   Video bị xóa / chuyển private giữa hai bước sẽ biến mất.
   → Phải đối soát `len(video_ids)` với `len(extracted_data)` và ghi log chênh lệch.

---

## 4. ⚠️ Cạm bẫy `duration` — BUG ĐANG TỒN TẠI TRONG REPO

`contentDetails.duration` theo chuẩn ISO-8601 duration:

| Loại video | Giá trị trả về |
|---|---|
| Video thường 26 phút 10 giây | `PT26M10S` |
| Shorts 45 giây | `PT45S` |
| Video 1 giờ 2 phút | `PT1H2M` |
| **Livestream đang phát / sắp phát** | **`P0D`** ⚠️ |

**Bug:** `parse_duration()` trong `dags/datawarehouse/data_transformation.py`
biến `P0D` thành `timedelta(0)`, rồi `transform_data()` gán:

```python
row["Video_Type"] = "Shorts" if duration_td.total_seconds() <= 60 else "Normal"
```

→ Một **livestream 3 tiếng bị phân loại là "Shorts"**.

**Ảnh hưởng nghiệp vụ:** mọi phân tích so sánh Shorts vs video thường đều sai lệch.

**Hướng sửa (Buổi 6):** thêm loại thứ ba.

```python
if duration_td.total_seconds() == 0:
    row["Video_Type"] = "Live"        # hoặc "Unknown"
elif duration_td.total_seconds() <= 60:
    row["Video_Type"] = "Shorts"
else:
    row["Video_Type"] = "Normal"
```

> Bài học tổng quát: **ranh giới phân loại phải xử lý giá trị đặc biệt TRƯỚC**,
> không để nó rơi vào nhánh mặc định.
> `duration = 0` KHÔNG có nghĩa "dài 0 giây", mà là "CHƯA XÁC ĐỊNH".
> Gộp "bằng 0" với "chưa biết" là nguồn gốc của vô số lỗi phân tích.

**Trạng thái:** ĐÃ SỬA (Buổi 1) — thêm `classify_video_type()`, loại thứ ba `"Live"`.
Nâng cấp chuẩn hơn (Buổi 2): lấy thêm `snippet.liveBroadcastContent`
(`"live"` / `"upcoming"` / `"none"`) để xác định trực tiếp thay vì suy từ duration.

---

## 4b. ⚠️ BUG #2 — `Duration TIME` không chứa nổi video > 24 giờ

```
Input  P1DT2H3M4S  (1 ngày 2 giờ 3 phút 4 giây)
Output 02:03:04    -> MẤT TRẮNG phần "1 ngày"
```

**Hai tầng nguyên nhân:**

1. **Code:** `(datetime.min + duration_td).time()` cắt bỏ phần ngày.
2. **Schema (gốc rễ):** kiểu `TIME` của Postgres chỉ biểu diễn `00:00:00`–`24:00:00`.
   Sai về khái niệm: `TIME` là *thời điểm trong ngày*, thứ ta cần là *khoảng thời gian*.

**Ai bị ảnh hưởng:** livestream dài được archive (MrBeast có nhiều video > 24h? hiếm,
nhưng "24 hours challenge" là format phổ biến trên YouTube).

**Hướng sửa (Buổi 4 — cần bàn migration):**
- Phương án A: `Duration INTERVAL` — đúng khái niệm nhất.
- Phương án B: `Duration_Seconds BIGINT` — dễ tính toán, dễ port sang hệ khác. **Khuyến nghị.**

**Trạng thái:** CHƯA SỬA — chờ Buổi 4 (Data Modeling), vì đổi kiểu cột
kéo theo migration cho dữ liệu đã có trong bảng.

---

## 5. Cam kết đầu ra — `core.yt_api`

**Grain:** 1 dòng = 1 `Video_ID` × 1 `Snapshot_Date`
**Primary key:** `("Video_ID", "Snapshot_Date")`

| Cột | Kiểu | Null? | Nguồn |
|---|---|---|---|
| `Video_ID` | `VARCHAR(11)` | Không | `id` |
| `Snapshot_Date` | `DATE` | Không | Ngày chạy pipeline (logical date) |
| `Video_Title` | `TEXT` | Không | `snippet.title` |
| `Upload_Date` | `TIMESTAMP` | Không | `snippet.publishedAt` |
| `Duration` | `TIME` | Không | `contentDetails.duration` đã parse |
| `Video_Type` | `VARCHAR(10)` | Không | Dẫn xuất: Shorts / Normal / Live |
| `Video_Views` | `BIGINT` | **Có** | `statistics.viewCount` |
| `Likes_Count` | `BIGINT` | **Có** | `statistics.likeCount` |
| `Comments_Count` | `BIGINT` | **Có** | `statistics.commentCount` |

**Bất biến (invariants) — được Soda kiểm tra:**
- `Video_ID` không bao giờ null
- `(Video_ID, Snapshot_Date)` không bao giờ trùng
- `Likes_Count <= Video_Views`
- `Comments_Count <= Video_Views`

**Ngữ nghĩa:** các cột đếm là **giá trị tích lũy tại thời điểm chụp**, không phải
giá trị phát sinh trong ngày. Muốn tính tăng trưởng phải dùng `LAG()`.

---

## 6. Bằng chứng thực nghiệm — chạy thật 2026-09-08

Kênh `@MrBeast`, chạy toàn bộ tầng extract trong 15,9 giây (41 units quota).

| Quan sát | Số liệu | Kết luận |
|---|---|---|
| `channels.videoCount` | 999 | Hai endpoint của cùng Google **lệch nhau 1** (0,1%). |
| `playlistItems.totalResults` | 1000 | Không có "một nguồn chân lý duy nhất" — phải ĐO mức lệch, không so bằng. |
| Video ID lấy được | 1000/1000 | Đối soát đủ. |
| Chi tiết lấy được | 1000/1000 | Đối soát đủ. |
| **`likeCount` VẮNG MẶT** | **7 video** | ✅ Xác nhận: bắt buộc `.get(...)`. Dùng `[...]` là pipeline chết. |
| **`commentCount` VẮNG MẶT** | **1 video** | ✅ Xác nhận. |
| `viewCount` vắng mặt | 0 video | Hiếm khi bị ẩn, nhưng vẫn phải phòng. |
| `liveBroadcastContent` | `"none"` × 1000 | Không có livestream trong snapshot này -> bug `P0D` thuộc loại **hiếm nhưng chí mạng**. |
| `viewCount` của kênh | `"139326389154"` (139 tỷ) | ✅ Xác nhận `BIGINT`: `INT` (trần ~2,1 tỷ) sẽ tràn số. |

**Hệ quả xuống tầng dưới:** `Likes_Count` / `Comments_Count` sẽ có `NULL` thật trong
bảng. Mọi phép tính tổng hợp phải xử lý NULL tường minh (`COALESCE`, hoặc chấp nhận
`AVG` bỏ qua NULL). Soda check `Likes_Count > Video_Views` vẫn đúng vì so sánh với
NULL cho kết quả UNKNOWN (không tính là vi phạm).

---

## 7. Lịch sử thay đổi

| Ngày | Thay đổi |
|---|---|
| 2026-09-07 | Tạo contract. Ghi nhận bug `P0D` -> Shorts (ĐÃ SỬA) và bug `Duration TIME` mất phần ngày (chờ Buổi 4). |
| 2026-09-08 | Thêm mục 6 (bằng chứng thực nghiệm). Extract nay có đối soát + `liveBroadcastContent`. |
