"""
TẦNG LOGIC THUẦN — client gọi YouTube Data API v3.

QUY TẮC SỐ MỘT CỦA FILE NÀY: KHÔNG import airflow, KHÔNG import boto3.
Chỉ phụ thuộc requests + tenacity.

Vì sao tách riêng?
    - Test được bằng `pytest` thường, không cần dựng Airflow + Postgres + Redis.
      Test chạy trong mili giây thay vì hàng phút.
    - Chạy thử được ngay trong terminal khi debug.
    - Đổi Airflow sang Dagster/Prefect thì file này không sửa một dòng.
    - Người đọc biết ngay: muốn hiểu NGHIỆP VỤ thì đọc file này; muốn hiểu
      LỊCH CHẠY thì đọc video_stats.py và main.py.

Đây là ứng dụng của nguyên tắc "separation of concerns": mỗi module một lý do
để thay đổi. File này chỉ đổi khi YouTube API đổi. video_stats.py chỉ đổi khi
cách điều phối đổi.
"""

import logging

import requests
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

logger = logging.getLogger(__name__)

MAX_RESULTS = 50            # YouTube cho tối đa 50 item/request -> quyết định cỡ batch
YOUTUBE_API = "https://youtube.googleapis.com/youtube/v3"

# Ngưỡng đối soát. Nguồn dữ liệu thật LUÔN lệch nhau đôi chút
# (channels.videoCount = 999 nhưng playlistItems.totalResults = 1000).
# Việc của DE không phải bắt chúng bằng nhau, mà là ĐO mức lệch và quyết định
# ngưỡng nào chấp nhận được: dưới ngưỡng -> cảnh báo, trên ngưỡng -> gãy to.
MISSING_TOLERANCE = 0.05    # 5%

# Chặn vòng lặp phân trang chạy vô hạn nếu API trả token lặp/hỏng.
MAX_PAGES = 200


@retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    retry=retry_if_exception_type(requests.exceptions.RequestException),
)
def call_api(url: str) -> dict:
    """Gọi API -> có timeout -> lỗi mạng thì tự thử lại -> trả JSON.

    RETRY TẦNG TRONG: xử lý sự cố tạm thời ở mức MỘT request (mạng chớp,
    YouTube trả 503). wait_exponential = chờ tăng dần 2s, 4s, 8s... để không
    đấm liên tục vào server đang ốm.
    Tầng ngoài (Airflow `retries`) chạy lại CẢ TASK khi tầng này đã bó tay.
    """
    response = requests.get(url, timeout=15)   # không có timeout = treo vĩnh viễn
    response.raise_for_status()
    return response.json()


def fetch_playlist_id(api_key: str, channel_id: str = None, handle: str = None) -> tuple[str, str]:
    """Trả về (uploads_playlist_id, channel_id_thật).

    Mỗi kênh YouTube có một playlist đặc biệt tên "uploads" tự động chứa mọi
    video kênh đã đăng. Đây là đường rẻ nhất để liệt kê video của kênh:
    channels.list (1 unit) thay vì search.list (100 units).
    """
    # NGUYÊN TẮC KHÓA BỀN VỮNG (stable identifier)
    # Handle (@MrBeast) là thứ chủ kênh ĐỔI ĐƯỢC; channel ID (UCX6OQ3...) vĩnh viễn.
    # Xây pipeline trên handle = xây trên cát.
    if channel_id:
        selector = f"id={channel_id}"
    elif handle:
        selector = f"forHandle={handle}"
    else:
        raise ValueError("Phải cung cấp channel_id hoặc handle.")

    data = call_api(f"{YOUTUBE_API}/channels?part=contentDetails&{selector}&key={api_key}")

    # BIẾN SILENT FAILURE THÀNH LOUD FAILURE
    # Handle sai / kênh bị xóa -> YouTube vẫn trả HTTP 200 nhưng KHÔNG có "items".
    # Code cũ data["items"][0] ném KeyError trần trụi, đọc log không hiểu vì sao.
    items = data.get("items") or []
    if not items:
        raise ValueError(
            f"YouTube không trả về kênh nào cho {selector}. "
            "Kênh có thể đã đổi handle, bị xóa, hoặc cấu hình sai."
        )

    channel = items[0]
    playlist_id = channel["contentDetails"]["relatedPlaylists"]["uploads"]
    return playlist_id, channel["id"]


def fetch_video_ids(api_key: str, playlist_id: str) -> list[str]:
    """Duyệt hết các trang của uploads playlist, trả về danh sách video ID.

    YouTube chỉ trả tối đa 50 item/trang; muốn trang sau phải gửi kèm
    nextPageToken của trang trước -> đây là 'cursor pagination'.
    """
    video_ids: list[str] = []
    page_token = None
    total_reported = None
    pages = 0

    while True:
        url = (
            f"{YOUTUBE_API}/playlistItems"
            f"?part=contentDetails&maxResults={MAX_RESULTS}"
            f"&playlistId={playlist_id}&key={api_key}"
        )
        if page_token:
            url += f"&pageToken={page_token}"

        data = call_api(url)

        # totalResults chỉ đọc ở TRANG ĐẦU - dùng để đối soát ở cuối hàm.
        if total_reported is None:
            total_reported = data.get("pageInfo", {}).get("totalResults")

        for item in data.get("items", []):
            video_ids.append(item["contentDetails"]["videoId"])

        page_token = data.get("nextPageToken")
        if not page_token:
            break                     # hết trang -> dừng

        # Chặn vòng lặp vô hạn: nếu API trả token lỗi/lặp mãi, vòng while sẽ
        # quay đến khi cháy hết quota. LUÔN đặt trần cho vòng lặp mà điều kiện
        # dừng do BÊN NGOÀI quyết định.
        pages += 1
        if pages >= MAX_PAGES:
            raise RuntimeError(
                f"Vượt {MAX_PAGES} trang khi duyệt playlist {playlist_id} - "
                "nghi ngờ phân trang bị lặp vô hạn."
            )

    reconcile(len(video_ids), total_reported, "video ID lấy từ playlist")
    return video_ids


def fetch_video_details(api_key: str, video_ids: list[str]) -> list[dict]:
    """Lấy chi tiết từng video, gửi theo lô 50 id/request.

    Vì sao phải batch? Không batch thì 999 video = 999 units thay vì 20 units.
    Đắt gấp 50 lần. Batching không phải để nhanh - để SỐNG trong hạn mức quota.
    """
    extracted_data: list[dict] = []

    for batch in batch_list(video_ids, MAX_RESULTS):
        url = (
            f"{YOUTUBE_API}/videos"
            f"?part=snippet,contentDetails,statistics&id={','.join(batch)}&key={api_key}"
        )
        data = call_api(url)

        for item in data.get("items", []):
            statistics = item.get("statistics", {})
            extracted_data.append({
                "video_id": item["id"],
                "title": item["snippet"]["title"],
                "publishedAt": item["snippet"]["publishedAt"],
                "duration": item["contentDetails"]["duration"],
                # "none" | "live" | "upcoming" - NGUỒN SỰ THẬT để phân loại
                # livestream, thay cho việc suy đoán từ duration == 0.
                # Xem docs/data_contract.md mục 4.
                "liveBroadcastContent": item["snippet"].get("liveBroadcastContent"),
                # statistics.* CÓ THỂ VẮNG MẶT HOÀN TOÀN (không phải null) khi
                # tác giả ẩn like / tắt bình luận -> bắt buộc .get(..., None).
                # Giữ NGUYÊN dạng string như API trả về, KHÔNG ép int ở đây:
                # raw trong lake phải trung thực tuyệt đối với nguồn.
                # Việc ép kiểu thuộc về biên giới Load (Buổi 5).
                "viewCount": statistics.get("viewCount", None),
                "likeCount": statistics.get("likeCount", None),
                "commentCount": statistics.get("commentCount", None),
            })

    # ĐỐI SOÁT: gửi 50 id có thể chỉ nhận về 48 (video vừa bị xóa hoặc chuyển
    # private) - API vẫn trả HTTP 200, KHÔNG báo gì. Đây là 'silent data loss',
    # loại lỗi nguy hiểm nhất vì DAG vẫn xanh.
    reconcile(len(extracted_data), len(video_ids), "chi tiết video")
    return extracted_data


# ---------------------------- hàm phụ trợ ----------------------------

def batch_list(items, size):
    """Cắt danh sách thành từng lô `size` phần tử."""
    for i in range(0, len(items), size):
        yield items[i : i + size]


def reconcile(actual: int, expected, what: str) -> None:
    """So số lấy được với số kỳ vọng; dưới ngưỡng thì cảnh báo, trên thì gãy.

    Vì sao không so bằng tuyệt đối? Vì nguồn thật luôn lệch nhẹ mà vẫn hợp lệ:
    video bị xóa giữa hai bước gọi API, totalResults của YouTube vốn là ước lượng.
    So bằng tuyệt đối -> pipeline đỏ mỗi ngày -> người ta tắt cảnh báo -> mất
    tác dụng. Cảnh báo kêu sai quá nhiều còn tệ hơn không có cảnh báo.
    """
    if not expected:
        return

    missing = expected - actual
    if missing <= 0:
        logger.info("Đối soát %s: %d/%d - đủ.", what, actual, expected)
        return

    ratio = missing / expected
    message = "Đối soát %s: chỉ có %d/%d (thiếu %d = %.1f%%)"
    args = (what, actual, expected, missing, ratio * 100)

    if ratio > MISSING_TOLERANCE:
        logger.error(message, *args)
        raise ValueError(
            f"Thiếu {ratio:.1%} {what} - vượt ngưỡng {MISSING_TOLERANCE:.0%}. "
            "Nghi ngờ lỗi phân trang, quota, hoặc sự cố phía nguồn."
        )

    logger.warning(message + " - trong ngưỡng cho phép, vẫn chạy tiếp.", *args)
