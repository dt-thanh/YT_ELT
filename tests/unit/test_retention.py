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


# ---- R3: hạn lưu theo lớp dữ liệu (đặc tả R3, migration 008) ------------------
import dataclasses                                                     # noqa: E402
from datetime import datetime, timedelta, timezone                    # noqa: E402

import pytest                                                          # noqa: E402

from youtube_intel import retention as R                               # noqa: E402
from youtube_intel.config import load_config                           # noqa: E402

SETTINGS = load_config(require=()).settings
OFF = R.R3Limits.from_settings(SETTINGS)
ON = R.R3Limits.from_settings(dataclasses.replace(SETTINGS, derived_metrics_enabled=True))


def test_shipped_limits_stay_within_30_days_with_the_flag_off():
    assert SETTINGS.derived_metrics_enabled is False
    assert OFF.api_days <= 30 and OFF.stats_days == OFF.api_days
    assert SETTINGS.raw_retention_days is not None and SETTINGS.raw_retention_days <= 30


def test_flag_on_keeps_statistics_up_to_36_months_but_not_text():
    assert ON.stats_days == SETTINGS.approved_stats_retention_days <= 1095
    assert ON.api_days == OFF.api_days


class SqlCursor:
    def __init__(self):
        self.calls = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return {"n": 0, "oldest": None, "title": None}

    def fetchall(self):
        return []


# One rule per data class: table it touches and which limit applies.
@pytest.mark.parametrize("what, data_class, table, limit", [
    ("thống kê công khai", "stats_non_authorized", "yti.video_observations", "stats"),
    ("embedding tìm kiếm", "derived", "yti.video_chunks", "api"),
    ("metadata video", "metadata_text", "yti.videos_current", "api"),
    ("metadata kênh", "metadata_text", "yti.tracked_channels", "api"),
    ("staging (mọi batch)", "metadata_text", "yti_staging.video_observations_stg", "api"),
    ("bản tin (evidence có tiêu đề + số liệu)", "metadata_text", "yti.reports", "api"),
])
def test_every_data_class_has_a_rule_on_fetched_at(what, data_class, table, limit):
    rule = next(r for r in R.R3_RULES if r.what == what)
    assert (rule.data_class, rule.limit) == (data_class, limit)
    assert table in rule.delete_sql and "fetched_at <" in rule.delete_sql
    cur = SqlCursor()
    R.delete_r3(cur, OFF)
    [(sql, params)] = [c for c in cur.calls if c[0] == rule.delete_sql]
    assert params == {"days": OFF.api_days}                  # flag off: everything <= 30 days


def test_flag_on_only_statistics_get_the_longer_limit():
    cur = SqlCursor()
    R.delete_r3(cur, ON)
    days = {sql: p["days"] for sql, p in cur.calls}
    for rule in R.R3_RULES:
        assert days[rule.delete_sql] == (ON.stats_days if rule.data_class == "stats_non_authorized"
                                         else ON.api_days)


def test_staging_rule_ignores_quality_status():
    # R3 overrides "keep quarantined batches as evidence".
    rule = next(r for r in R.R3_RULES if "staging" in r.what)
    assert "quality_status" not in rule.delete_sql


def test_embeddings_expire_with_their_source_text():
    rule = next(r for r in R.R3_RULES if r.data_class == "derived")
    assert "v.fetched_at" in rule.delete_sql                 # the video's, not the chunk's
    order = [r.what for r in R.R3_RULES]
    assert order.index("embedding tìm kiếm") < order.index("metadata video")


class OldNewStore:
    """MinIO with one raw object older than the limit and one newer."""
    prefix = "youtube"

    def __init__(self):
        now = datetime.now(timezone.utc)
        self.objects = [
            {"key": "youtube/resource=videos/channel_id=UCa/collection_id=1/attempt=1/batch=0001.json",
             "size": 10, "last_modified": now - timedelta(days=40)},
            {"key": "youtube/resource=videos/channel_id=UCb/collection_id=2/attempt=1/batch=0001.json",
             "size": 10, "last_modified": now - timedelta(days=1)}]

    def bucket_exists(self):
        return True

    def list_objects(self, prefix):
        return [o for o in self.objects if o["key"].startswith(prefix)]


def test_raw_snapshots_older_than_the_limit_are_planned_for_deletion():
    plan = plan_raw_cleanup(OldNewStore(), days=SETTINGS.raw_retention_days)
    assert plan.keys == [OldNewStore().objects[0]["key"]]


def test_channel_deletion_covers_every_table_and_its_raw_objects():
    cur = SqlCursor()
    plans = R.plan_channel_deletion(cur, OldNewStore(), "UCa")
    assert plans[-1].keys == [OldNewStore().objects[0]["key"]]          # only that channel's raw
    R.delete_channel_data(cur, "UCa")
    sql = " ".join(c[0] for c in cur.calls)
    for table in ("yti.reports", "yti.video_observations", "yti.video_chunks",
                  "yti.videos_current", "yti_staging.video_observations_stg",
                  "yti.tracked_channels"):
        assert f"DELETE FROM {table}" in sql or f"UPDATE {table}" in sql
    assert "enabled = FALSE" in sql
