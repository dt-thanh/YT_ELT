"""
Client gọi YouTube Data API v3 - TẦNG LOGIC THUẦN.

QUY TẮC SỐ MỘT: file này KHÔNG import airflow, KHÔNG import boto3, KHÔNG import
psycopg2. Chỉ requests + tenacity. Nhờ vậy test chạy trong mili giây và debug
được ngay trong terminal khi hạ tầng chưa lên.

BA NÂNG CẤP SO VỚI dags/api/youtube_client.py:
  1. PHÂN LOẠI LỖI: 403 của YouTube có nhiều nguyên nhân khác hẳn nhau.
     quotaExceeded -> dừng hẳn (retry chỉ tốn thời gian, không bao giờ thành công)
     keyInvalid    -> dừng hẳn (lỗi cấu hình, người phải sửa)
     rateLimit/5xx -> retry (lỗi tạm thời)
     Retry mù mọi lỗi vừa chậm, vừa che mất nguyên nhân thật.
  2. ĐẾM QUOTA: biết mỗi lần chạy tiêu bao nhiêu unit trong hạn mức 10.000/ngày.
     Không đo thì không quản được.
  3. LẤY THÊM status.madeForKids - trường ba trạng thái (true/false/KHÔNG BIẾT).

VỀ MẶT DI CHUYỂN CODE: file cũ dags/api/youtube_client.py vẫn GIỮ NGUYÊN cho tới
khi DAG mới thay xong DAG cũ. Xây cái mới song song, chuyển dần, rồi mới xóa cái
cũ - kỹ thuật này tên là STRANGLER FIG PATTERN. Đập cũ trước khi mới chạy được
là cách nhanh nhất để có một hệ thống không chạy được.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import requests
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

logger = logging.getLogger(__name__)

YOUTUBE_API = "https://youtube.googleapis.com/youtube/v3"

# YouTube cho tối đa 50 item mỗi request -> quyết định cỡ batch và cỡ trang.
MAX_RESULTS = 50

# Ngưỡng đối soát: nguồn thật LUÔN lệch nhẹ mà vẫn hợp lệ (video bị xóa giữa hai
# bước gọi API, totalResults của YouTube vốn là ước lượng). So bằng tuyệt đối ->
# pipeline đỏ mỗi ngày -> người ta tắt cảnh báo -> cảnh báo mất tác dụng.
# Cảnh báo kêu sai quá nhiều còn tệ hơn không có cảnh báo.
MISSING_TOLERANCE = 0.05

# Bảng giá quota (units/request) - nguồn: developers.google.com/youtube/v3/determine_quota_cost
# search.list giá 100 -> ĐẮT GẤP 100 LẦN, tuyệt đối tránh.
QUOTA_COST = {"channels": 1, "playlistItems": 1, "videos": 1, "search": 100}


# =============================================================================
# CÂY NGOẠI LỆ - mỗi loại lỗi một cách xử lý khác nhau
#
# Vì sao phải tự định nghĩa lỗi riêng thay vì dùng requests.RequestException?
# Vì nơi gọi cần QUYẾT ĐỊNH dựa trên loại lỗi:
#     except QuotaExceededError  -> dừng cả run, hẹn mai chạy lại
#     except ConfigurationError  -> báo người vận hành, đừng thử lại
#     except RetryableError      -> để tenacity lo
# Nếu tất cả cùng một kiểu exception thì nơi gọi phải đọc chuỗi thông báo để
# đoán - cách làm mong manh nhất có thể.
# =============================================================================

class YouTubeError(Exception):
    """Gốc của mọi lỗi từ YouTube API."""


class RetryableError(YouTubeError):
    """Lỗi TẠM THỜI - thử lại có khả năng thành công.
    Gồm: 5xx, timeout, đứt kết nối, rateLimitExceeded."""


class QuotaExceededError(YouTubeError):
    """Hết quota ngày. KHÔNG retry: quota chỉ reset lúc nửa đêm giờ Thái Bình Dương.
    Nơi gọi phải DỪNG CẢ RUN, ghi trạng thái, và hẹn lần chạy sau."""


class ConfigurationError(YouTubeError):
    """Key sai / API chưa bật / thiếu quyền. KHÔNG retry: cần CON NGƯỜI sửa."""


class NotFoundError(YouTubeError):
    """Tài nguyên không tồn tại (404). KHÔNG retry."""


# Những `reason` mà Google trả kèm HTTP 403 nhưng bản chất là TẠM THỜI.
# Cùng mã 403, hai nhóm ý nghĩa trái ngược -> phải đọc `reason`, không đọc mã số.
TRANSIENT_403_REASONS = {
    "rateLimitExceeded",
    "userRateLimitExceeded",
    "backendError",
    "internalError",
}

QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded"}


# =============================================================================
# ĐO QUOTA
# =============================================================================

@dataclass
class QuotaMeter:
    """Đếm số request và số unit đã tiêu.

    Vì sao cần? Hạn mức là 10.000 unit/ngày, KHÔNG mua thêm được bằng tiền
    (phải nộp đơn xin Google duyệt tay). Nên quota là tài nguyên phải QUẢN LÝ,
    và muốn quản thì phải ĐO. Con số này được ghi vào channel_batches.request_count.
    """
    request_count: int = 0
    estimated_units: int = 0
    by_endpoint: dict[str, int] = field(default_factory=dict)

    def charge(self, endpoint: str) -> None:
        cost = QUOTA_COST.get(endpoint, 1)
        self.request_count += 1
        self.estimated_units += cost
        self.by_endpoint[endpoint] = self.by_endpoint.get(endpoint, 0) + cost

    def snapshot(self) -> dict:
        return {
            "request_count": self.request_count,
            "estimated_units": self.estimated_units,
            "by_endpoint": dict(self.by_endpoint),
        }


# =============================================================================
# KIỂU TRẢ VỀ - trả cả DỮ LIỆU lẫn SIÊU DỮ LIỆU VỀ CHẤT LƯỢNG
#
# Đây là điểm khác biệt lớn so với code cũ. Code cũ trả về list[str] trần trụi:
# nơi gọi KHÔNG BIẾT danh sách đó đã đầy đủ hay bị cắt giữa chừng.
# Giờ trả về một object mang theo: đã cắt chưa (truncated), nguồn báo bao nhiêu
# (total_reported), nhận được bao nhiêu (received_count).
#
# NGUYÊN TẮC: dữ liệu phải đi kèm thông tin về ĐỘ TIN CẬY CỦA CHÍNH NÓ.
# =============================================================================

@dataclass(frozen=True)
class ChannelInfo:
    channel_id: str
    uploads_playlist_id: str
    title: str
    description: str
    video_count: int | None


@dataclass(frozen=True)
class DiscoveryResult:
    video_ids: tuple[str, ...]
    total_reported: int | None      # YouTube nói playlist có bao nhiêu video
    truncated: bool                 # TRUE = chạm trần trang, CHƯA duyệt hết
    pages_fetched: int


@dataclass(frozen=True)
class VideoDetailsResult:
    videos: tuple[dict, ...]
    expected_count: int             # số id đã gửi đi
    received_count: int             # số bản ghi nhận về
    missing_ids: tuple[str, ...]    # id gửi đi mà không thấy trả về


# =============================================================================
# CLIENT
# =============================================================================

class YouTubeClient:
    """Bọc YouTube Data API v3.

    Dùng CLASS thay vì các hàm rời vì có trạng thái dùng chung thật sự:
    api_key, quota meter, và requests.Session.

    requests.Session không chỉ cho gọn: nó GIỮ KẾT NỐI TCP/TLS để tái sử dụng
    (HTTP keep-alive). Mỗi request mới không phải bắt tay TLS lại từ đầu.
    Với 41 request mỗi lần chạy, đây là khác biệt đo được.
    """

    def __init__(
        self,
        api_key: str,
        *,
        timeout: int = 30,
        max_attempts: int = 4,
        meter: QuotaMeter | None = None,
    ) -> None:
        if not api_key:
            raise ConfigurationError("Thiếu API key.")
        self._api_key = api_key
        self._timeout = timeout
        self._max_attempts = max_attempts
        self.meter = meter or QuotaMeter()
        self._session = requests.Session()

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "YouTubeClient":
        return self

    def __exit__(self, *exc) -> None:
        # Context manager -> `with YouTubeClient(...) as yt:` tự đóng session
        # kể cả khi có ngoại lệ. Rò rỉ kết nối là loại lỗi chỉ lộ ra khi chạy lâu.
        self.close()

    # ---------------------------------------------------------------- HTTP --

    def _get(self, endpoint: str, params: dict) -> dict:
        """Gọi một endpoint, có retry cho lỗi tạm thời và phân loại lỗi vĩnh viễn."""

        @retry(
            stop=stop_after_attempt(self._max_attempts),
            wait=wait_exponential(multiplier=1, min=2, max=30),
            # ⭐ CHỈ retry RetryableError. QuotaExceededError / ConfigurationError
            # bay thẳng lên trên, không thử lại lần nào.
            retry=retry_if_exception_type(RetryableError),
            # ⭐ GHI LOG MỖI LẦN RETRY. Không có dòng này, retry diễn ra HOÀN TOÀN
            # IM LẶNG: bạn chỉ thấy quota tiêu nhiều hơn dự kiến mà không hiểu vì
            # sao. Retry ẩn là retry không quan sát được - và thứ không quan sát
            # được thì không gỡ lỗi được.
            before_sleep=before_sleep_log(logger, logging.WARNING),
            reraise=True,
        )
        def _call() -> dict:
            url = f"{YOUTUBE_API}/{endpoint}"
            query = {**params, "key": self._api_key}
            self.meter.charge(endpoint)

            try:
                response = self._session.get(url, params=query, timeout=self._timeout)
            except (requests.Timeout, requests.ConnectionError) as e:
                # Mạng chập chờn -> tạm thời -> đáng retry.
                raise RetryableError(f"Lỗi mạng khi gọi {endpoint}: {e}") from e

            if response.status_code == 200:
                return response.json()

            raise _classify_error(endpoint, response)

        return _call()

    # ------------------------------------------------------------- API ------

    def fetch_channel(self, *, channel_id: str | None = None, handle: str | None = None) -> ChannelInfo:
        """Xác minh kênh và lấy uploads playlist.

        KHÔNG tự suy uploads playlist bằng cách đổi 'UC' thành 'UU'. Quy luật đó
        đúng 4/4 kênh đã kiểm tra, nhưng vẫn là hành vi Google KHÔNG cam kết.
        Đọc contentDetails.relatedPlaylists.uploads là hợp đồng; suy luận chuỗi
        là may rủi.
        """
        if channel_id:
            params = {"part": "snippet,contentDetails,statistics", "id": channel_id}
            selector = f"id={channel_id}"
        elif handle:
            params = {"part": "snippet,contentDetails,statistics", "forHandle": handle}
            selector = f"forHandle={handle}"
        else:
            raise ConfigurationError("Phải cung cấp channel_id hoặc handle.")

        data = self._get("channels", params)

        # HTTP 200 KHÔNG có nghĩa là tìm thấy. Handle sai / kênh bị xóa -> YouTube
        # vẫn trả 200 nhưng không có "items". Biến im lặng thành tiếng ồn.
        items = data.get("items") or []
        if not items:
            raise NotFoundError(
                f"YouTube không trả về kênh nào cho {selector}. "
                "Kênh có thể đã đổi handle, bị xóa, hoặc cấu hình sai."
            )

        item = items[0]
        snippet = item.get("snippet", {})
        stats = item.get("statistics", {})

        return ChannelInfo(
            channel_id=item["id"],
            uploads_playlist_id=item["contentDetails"]["relatedPlaylists"]["uploads"],
            title=snippet.get("title", ""),
            description=snippet.get("description", ""),
            video_count=_to_int(stats.get("videoCount")),
        )

    def discover_video_ids(
        self, playlist_id: str, *, limit: int = 50, page_limit: int = 2
    ) -> DiscoveryResult:
        """Duyệt uploads playlist lấy video ID, có TRẦN rõ ràng.

        HAI cái trần, khác nhau:
          limit      - lấy tối đa bao nhiêu video (quyết định nghiệp vụ)
          page_limit - gọi tối đa bao nhiêu trang (chặn vòng lặp vô hạn)
        Chạm bất kỳ trần nào -> truncated=True. Bắt buộc phải báo, nếu không ta
        sẽ vô tình khẳng định "đã lấy toàn bộ kênh" trong khi mới lấy 50/1274.
        """
        video_ids: list[str] = []
        page_token: str | None = None
        total_reported: int | None = None
        pages = 0
        truncated = False

        while True:
            params = {
                "part": "contentDetails",
                "maxResults": MAX_RESULTS,
                "playlistId": playlist_id,
            }
            if page_token:
                params["pageToken"] = page_token

            data = self._get("playlistItems", params)
            pages += 1

            if total_reported is None:
                total_reported = data.get("pageInfo", {}).get("totalResults")

            for entry in data.get("items", []):
                video_ids.append(entry["contentDetails"]["videoId"])
                if len(video_ids) >= limit:
                    break

            if len(video_ids) >= limit:
                # Đủ số lượng cần. Nếu playlist còn video nữa thì ta ĐÃ CẮT.
                truncated = bool(data.get("nextPageToken")) or (
                    total_reported is not None and total_reported > len(video_ids)
                )
                break

            page_token = data.get("nextPageToken")
            if not page_token:
                break                       # hết trang thật -> lấy đủ, không cắt

            if pages >= page_limit:
                truncated = True            # chạm trần trang mà playlist còn nữa
                break

        # Chỉ đối soát khi KHÔNG cắt. Cắt có chủ đích thì "thiếu" là đúng ý,
        # không phải lỗi -> cảnh báo ở đây sẽ là cảnh báo sai.
        if not truncated:
            _reconcile(len(video_ids), total_reported, "video ID từ playlist")

        return DiscoveryResult(
            video_ids=tuple(video_ids),
            total_reported=total_reported,
            truncated=truncated,
            pages_fetched=pages,
        )

    def fetch_video_details(self, video_ids: list[str] | tuple[str, ...]) -> VideoDetailsResult:
        """Lấy chi tiết video theo lô 50 id/request.

        Không batch thì 1000 video = 1000 unit thay vì 20. Đắt gấp 50 lần.
        Batching không phải để nhanh - để SỐNG trong hạn mức quota.
        """
        video_ids = tuple(video_ids)
        collected: list[dict] = []
        seen: set[str] = set()

        for batch in _chunks(video_ids, MAX_RESULTS):
            data = self._get(
                "videos",
                {
                    # `status` là phần MỚI so với code cũ -> lấy madeForKids.
                    # Thêm part KHÔNG tốn thêm quota (vẫn 1 unit/request) nhưng
                    # response nặng hơn. Vẫn nên chỉ lấy part thực sự dùng.
                    "part": "snippet,contentDetails,statistics,status",
                    "id": ",".join(batch),
                },
            )

            for item in data.get("items", []):
                stats = item.get("statistics", {})
                snippet = item.get("snippet", {})
                status = item.get("status", {})
                seen.add(item["id"])

                collected.append({
                    "video_id": item["id"],
                    "channel_id": snippet.get("channelId"),
                    "title": snippet.get("title"),
                    "description": snippet.get("description"),
                    "publishedAt": snippet.get("publishedAt"),
                    "duration": item.get("contentDetails", {}).get("duration"),
                    "thumbnail_url": _pick_thumbnail(snippet),
                    "categoryId": snippet.get("categoryId"),
                    # Cả 4 kênh pilot đều KHÔNG trả defaultLanguage -> None là bình thường.
                    "defaultLanguage": snippet.get("defaultLanguage"),
                    # "none" | "live" | "upcoming" - nguồn sự thật để nhận livestream,
                    # thay cho suy đoán từ duration == 0.
                    "liveBroadcastContent": snippet.get("liveBroadcastContent"),
                    # BA TRẠNG THÁI: True / False / None(=không biết).
                    # .get() trả None khi vắng mặt - KHÔNG ép thành False.
                    "madeForKids": status.get("madeForKids"),
                    # GIỮ NGUYÊN dạng string như API trả. Raw phải trung thực với
                    # nguồn; ép kiểu là việc của biên giới Load (Bước 6).
                    # statistics.* CÓ THỂ VẮNG MẶT HOÀN TOÀN (không phải null):
                    # đã đo thật - 7/1000 video thiếu likeCount.
                    "viewCount": stats.get("viewCount"),
                    "likeCount": stats.get("likeCount"),
                    "commentCount": stats.get("commentCount"),
                })

        # Gửi 50 id có thể chỉ nhận về 48: video vừa bị xóa hoặc chuyển private.
        # API vẫn trả HTTP 200 và KHÔNG báo gì -> 'silent data loss', loại nguy
        # hiểm nhất vì pipeline vẫn xanh. Ghi ra để nhìn thấy được.
        missing = tuple(v for v in video_ids if v not in seen)
        _reconcile(len(collected), len(video_ids), "chi tiết video")

        return VideoDetailsResult(
            videos=tuple(collected),
            expected_count=len(video_ids),
            received_count=len(collected),
            missing_ids=missing,
        )


# =============================================================================
# HÀM PHỤ TRỢ
# =============================================================================

def _classify_error(endpoint: str, response: requests.Response) -> YouTubeError:
    """Biến một HTTP response lỗi thành ĐÚNG loại ngoại lệ.

    ⭐ Đây là hàm quan trọng nhất file này.
    Mã 403 KHÔNG đủ để quyết định - phải đọc `error.errors[0].reason`:
        quotaExceeded      -> hết quota ngày, dừng hẳn
        rateLimitExceeded  -> gọi quá nhanh, chờ rồi thử lại
        keyInvalid         -> lỗi cấu hình, người phải sửa
    Ba thứ này cùng mã 403 nhưng cách xử lý trái ngược nhau.
    """
    try:
        payload = response.json()
        err = payload.get("error", {})
        reason = (err.get("errors") or [{}])[0].get("reason", "")
        message = err.get("message", "")
    except ValueError:
        reason, message = "", response.text[:200]

    code = response.status_code
    detail = f"{endpoint} -> HTTP {code} reason={reason!r}: {message}"

    if code == 403:
        if reason in QUOTA_REASONS:
            return QuotaExceededError(
                f"HẾT QUOTA YouTube API. {detail}. "
                "Quota reset lúc 00:00 giờ Thái Bình Dương. KHÔNG thử lại hôm nay."
            )
        if reason in TRANSIENT_403_REASONS:
            return RetryableError(f"Bị giới hạn tốc độ tạm thời: {detail}")
        return ConfigurationError(
            f"Bị từ chối quyền: {detail}. "
            "Kiểm tra: API key còn hiệu lực? YouTube Data API v3 đã Enable? Key có bị Restrict sai?"
        )

    if code == 400 and "keyInvalid" in reason:
        return ConfigurationError(f"API key không hợp lệ: {detail}")

    if code == 404:
        return NotFoundError(f"Không tìm thấy: {detail}")

    if code == 429:
        return RetryableError(f"Quá nhiều request: {detail}")

    if code >= 500:
        # Lỗi phía Google -> gần như luôn tạm thời.
        return RetryableError(f"Lỗi phía server: {detail}")

    # 4xx còn lại: lỗi do REQUEST của ta sai -> thử lại y hệt vẫn sai.
    return YouTubeError(f"Lỗi không xử lý được: {detail}")


def _chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i: i + size]


def _to_int(value) -> int | None:
    """Ép sang int, trả None nếu không được - KHÔNG trả 0.
    0 và 'không biết' là hai thứ khác nhau."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _pick_thumbnail(snippet: dict) -> str | None:
    """Chọn thumbnail độ phân giải cao nhất có sẵn.
    Không phải video nào cũng có đủ mọi cỡ -> duyệt từ cao xuống thấp."""
    thumbs = snippet.get("thumbnails") or {}
    for size in ("maxres", "standard", "high", "medium", "default"):
        if size in thumbs:
            return thumbs[size].get("url")
    return None


def _reconcile(actual: int, expected, what: str) -> None:
    """So số lấy được với số kỳ vọng; dưới ngưỡng thì cảnh báo, trên thì gãy."""
    if not expected:
        return

    missing = expected - actual
    if missing <= 0:
        logger.info("Đối soát %s: %d/%d - đủ.", what, actual, expected)
        return

    ratio = missing / expected
    if ratio > MISSING_TOLERANCE:
        logger.error("Đối soát %s: chỉ có %d/%d (thiếu %.1f%%)", what, actual, expected, ratio * 100)
        raise YouTubeError(
            f"Thiếu {ratio:.1%} {what} - vượt ngưỡng {MISSING_TOLERANCE:.0%}. "
            "Nghi ngờ lỗi phân trang, quota, hoặc sự cố phía nguồn."
        )

    logger.warning(
        "Đối soát %s: chỉ có %d/%d (thiếu %.1f%%) - trong ngưỡng, vẫn chạy tiếp.",
        what, actual, expected, ratio * 100,
    )
