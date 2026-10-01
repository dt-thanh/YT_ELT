"""Client YouTube - KHÔNG gọi YouTube thật (response giả, hoặc cổng đóng trên máy)."""
import json

import pytest
import requests

from youtube_intel import youtube
from youtube_intel.youtube import (
    ConfigurationError, NotFoundError, QuotaExceededError, RetryableError,
    YouTubeClient, _classify_error,
)

SECRET = "FAKE_KEY_FOR_TEST_123"


def fake_response(status, reason=""):
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps(
        {"error": {"errors": [{"reason": reason}], "message": "m"}}).encode()
    return r


@pytest.mark.parametrize("status, reason, expected", [
    # ⭐ CÙNG mã 403, BA cách xử lý trái ngược - phải đọc `reason`
    (403, "quotaExceeded", QuotaExceededError),     # dừng cả run, retry vô ích
    (403, "rateLimitExceeded", RetryableError),     # chờ rồi thử lại
    (403, "forbidden", ConfigurationError),         # người phải sửa
    (404, "videoNotFound", NotFoundError),
    (429, "", RetryableError),
    (503, "backendError", RetryableError),
])
def test_error_classification(status, reason, expected):
    assert isinstance(_classify_error("videos", fake_response(status, reason)), expected)


def test_permanent_errors_are_not_retryable():
    # Nếu cây ngoại lệ sai, tenacity sẽ retry cả lỗi hết quota -> phí thời gian
    for exc in (QuotaExceededError, ConfigurationError, NotFoundError):
        assert not issubclass(exc, RetryableError)


# ---- API key KHÔNG được nằm trong URL -------------------------------------
def test_api_key_goes_in_header_not_in_query(monkeypatch):
    client = YouTubeClient(SECRET, max_attempts=1)
    seen = {}

    def fake_get(url, params=None, timeout=None):
        seen["url"], seen["params"] = url, params
        r = requests.Response()
        r.status_code, r._content = 200, b'{"items": []}'
        return r

    monkeypatch.setattr(client._session, "get", fake_get)
    client._get("videos", {"part": "snippet", "id": "abc"})
    assert "key" not in seen["params"] and SECRET not in seen["url"]
    assert client._session.headers["X-Goog-Api-Key"] == SECRET


def test_network_error_message_does_not_leak_key(monkeypatch):
    # Lỗi mạng THẬT của requests: cổng 9 trên chính máy này bị từ chối ngay,
    # không cần internet. Trước khi sửa, thông báo ConnectionError chứa nguyên
    # URL "...?part=...&key=<KEY>" và bị ghi thẳng vào log Airflow.
    monkeypatch.setattr(youtube, "YOUTUBE_API", "http://127.0.0.1:9")
    client = YouTubeClient(SECRET, timeout=2, max_attempts=1)
    with pytest.raises(RetryableError) as e:
        client._get("videos", {"part": "snippet"})
    assert SECRET not in str(e.value)


# ---- Discovery: phân trang, trần, khử trùng --------------------------------
def _page(ids, next_token=None, total=None):
    page = {"items": [{"contentDetails": {"videoId": v}} for v in ids],
            "pageInfo": {"totalResults": total}}
    if next_token:
        page["nextPageToken"] = next_token
    return page


def test_discovery_dedups_and_flags_page_cap(monkeypatch):
    client = YouTubeClient(SECRET)
    pages = iter([_page(["a", "b", "a", "c"], "p2", total=10),
                  _page(["d", "e"], "p3", total=10)])
    monkeypatch.setattr(client, "_get", lambda endpoint, params: next(pages))
    res = client.discover_video_ids("UUx", limit=50, page_limit=2)
    assert res.video_ids == ("a", "b", "c", "d", "e")    # 'a' lặp bị bỏ
    assert res.truncated is True and res.pages_fetched == 2   # còn trang p3 mà chạm trần


def test_discovery_cut_by_limit_is_flagged(monkeypatch):
    # Lấy 3/5 video là CẮT CÓ CHỦ Ý -> phải ghi truncated, không được giả vờ "đủ"
    client = YouTubeClient(SECRET)
    monkeypatch.setattr(client, "_get",
                        lambda endpoint, params: _page(["a", "b", "c", "d", "e"], total=5))
    res = client.discover_video_ids("UUx", limit=3, page_limit=2)
    assert res.video_ids == ("a", "b", "c") and res.truncated is True


def test_discovery_whole_playlist_is_not_truncated(monkeypatch):
    client = YouTubeClient(SECRET)
    monkeypatch.setattr(client, "_get", lambda endpoint, params: _page(["a", "b"], total=2))
    res = client.discover_video_ids("UUx", limit=50, page_limit=2)
    assert res.video_ids == ("a", "b") and res.truncated is False
