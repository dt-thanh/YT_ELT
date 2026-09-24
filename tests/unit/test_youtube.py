"""Phân loại lỗi API - KHÔNG gọi mạng thật (dùng response giả)."""
import json

import pytest
import requests

from youtube_intel.youtube import (
    ConfigurationError, NotFoundError, QuotaExceededError, RetryableError,
    _classify_error,
)


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
