"""Quality gate: ERROR chặn, WARNING cho qua."""
from youtube_intel.quality import check_batch


def row(i, **over):
    base = {"stg_id": i, "video_id": f"vid{i:08d}", "title": f"Video {i}",
            "raw_view_count": "1000",
            "raw_like_count": "50", "raw_comment_count": "5",
            "raw_duration": "PT5M", "raw_published_at": "2026-01-01T00:00:00Z"}
    base.update(over)
    return base


def codes(report):
    return {i.code for i in report.issues}


def test_clean_batch_passes():
    r = check_batch(batch_id="b", rows=[row(1), row(2)], expected_count=2)
    assert r.passed and r.quality_status == "passed"


def test_empty_batch_is_error():
    # Publish "thành công" 0 dòng là nói dối.
    assert not check_batch(batch_id="b", rows=[]).passed


def test_duplicate_video_id_violates_grain():
    r = check_batch(batch_id="b", rows=[row(1), row(1)])
    assert not r.passed and "duplicate_video_id" in codes(r)


def test_present_but_unparsable_is_error():
    # "1,234" = YouTube ĐỔI ĐỊNH DẠNG -> phải chặn, không âm thầm bỏ qua
    r = check_batch(batch_id="b", rows=[row(1, raw_view_count="1,234")])
    assert not r.passed and "unparsable_view_count" in codes(r)


def test_absent_likes_is_only_warning():
    # ⭐ Vắng mặt (chủ kênh ẩn) = HIỂU ĐƯỢC -> cho qua.
    # Khác hẳn "có mà không đọc nổi" ở test trên.
    r = check_batch(batch_id="b", rows=[row(1, raw_like_count=None)])
    assert r.passed and "missing_like_count" in codes(r)


def test_likes_exceeding_views_is_impossible():
    r = check_batch(batch_id="b", rows=[row(1, raw_view_count="100", raw_like_count="999")])
    assert not r.passed and "engagement_exceeds_views" in codes(r)


def test_null_views_does_not_trigger_engagement_check():
    # "NULL > 100" là UNKNOWN, không phải TRUE - không được tính là vi phạm
    r = check_batch(batch_id="b", rows=[row(1, raw_view_count=None, raw_like_count="999")])
    assert "engagement_exceeds_views" not in codes(r)


def test_truncation_does_not_trigger_shortfall_alarm():
    # Cap 50/1274 video là CHỦ Ý -> không được báo "thiếu dữ liệu"
    r = check_batch(batch_id="b", rows=[row(1)], expected_count=1274,
                    discovery_truncated=True)
    assert r.passed and "received_below_expected" not in codes(r)


def test_fields_required_by_target_table_are_errors():
    # videos_current.title / published_at là NOT NULL. Để lọt thì publish gãy giữa
    # transaction -> rollback cả kênh, mất bằng chứng. Phải CÁCH LY ngay ở gate.
    r = check_batch(batch_id="b", rows=[row(1, title=None), row(2, raw_published_at=None)])
    assert not r.passed
    assert {"missing_title", "missing_published_at"} <= codes(r)


def test_video_id_length_matches_target_check():
    r = check_batch(batch_id="b", rows=[row(1, video_id="abc")])     # < 5 ký tự
    assert not r.passed and "invalid_video_id" in codes(r)
