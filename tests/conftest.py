"""
Fixture dùng chung.

BA TẦNG TEST - chạy từ rẻ đến đắt:
    tests/unit/         hàm thuần, KHÔNG mạng/DB  -> chạy mọi nơi, mili giây
    tests/test_dags.py  cần airflow                -> chạy trong container
    tests/integration/  cần Postgres/MinIO thật    -> đánh dấu @integration

Vì sao tầng unit nhiều nhất? Vì nó RẺ và NHANH -> chạy mỗi lần sửa code.
Test chậm và cần hạ tầng thì người ta ngại chạy -> thành vật trang trí.
Hình dung: "test pyramid" - đáy rộng (unit), đỉnh hẹp (e2e).
"""
from datetime import datetime, timezone

import pytest


@pytest.fixture
def now_utc():
    return datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def evidence():
    """Bằng chứng giả cho test validator - 2 video, 4 observation."""
    return {
        "group": "test",
        "coverage": {"total_videos": 2, "rankable": 2, "insufficient_history": 0,
                     "latest_observed_at": "2026-09-24T12:00:00+00:00"},
        "candidates": [
            {"video_id": "REAL_VIDEO_1", "evidence_ids": [101, 102], "title": "A",
             "channel_title": "Kênh A", "url": "https://www.youtube.com/watch?v=REAL_VIDEO_1",
             "metrics": {"views_per_hour": 100.0, "delta_views": 2400,
                         "elapsed_hours": 24.0, "total_views": 10000,
                         "likes": 50, "comments": None}},
            {"video_id": "REAL_VIDEO_2", "evidence_ids": [103, 104], "title": "B",
             "channel_title": "Kênh B", "url": "https://www.youtube.com/watch?v=REAL_VIDEO_2",
             "metrics": {"views_per_hour": 50.0, "delta_views": 1200,
                         "elapsed_hours": 24.0, "total_views": 5000,
                         "likes": None, "comments": None}},
        ],
    }


def pytest_configure(config):
    """Đăng ký marker NGAY TRONG conftest, không chỉ trong pyproject.toml.

    Vì sao? Trong container, pytest chạy ở /opt/airflow - nơi KHÔNG có
    pyproject.toml -> marker không được đăng ký -> cảnh báo, và với
    --strict-markers thì thành LỖI. conftest.py luôn đi theo thư mục tests/,
    nên đăng ký ở đây thì chạy ở đâu cũng đúng.
    """
    config.addinivalue_line("markers", "integration: cần database/MinIO thật")
    config.addinivalue_line("markers", "live: gọi API thật, TỐN QUOTA")
