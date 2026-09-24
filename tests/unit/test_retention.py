"""Retention trên hệ thống MỚI TINH - lỗi do mô phỏng CI phát hiện (24/09)."""
from youtube_intel.retention import plan_orphan_cleanup, plan_raw_cleanup


class FreshStore:
    """MinIO chưa có bucket nào - như ngay sau khi dựng hệ thống lần đầu."""
    prefix = "youtube"

    def bucket_exists(self):
        return False

    def list_objects(self, prefix):
        raise AssertionError("không được liệt kê bucket chưa tồn tại (NoSuchBucket)")


def test_orphan_plan_on_fresh_system_does_not_crash():
    plan = plan_orphan_cleanup(FreshStore(), hours=24)
    assert plan.count == 0 and "chưa có bucket" in plan.note


def test_raw_plan_on_fresh_system_does_not_crash():
    plan = plan_raw_cleanup(FreshStore(), days=30)
    assert plan.count == 0 and "chưa có bucket" in plan.note


def test_raw_plan_disabled_still_reports_disabled_first():
    # raw_retention_days=None phải báo "TẮT" - không phụ thuộc bucket có hay không
    plan = plan_raw_cleanup(FreshStore(), days=None)
    assert "TẮT" in plan.note
