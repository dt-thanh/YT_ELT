"""
Test biên giới Load - nơi DUY NHẤT ép kiểu.

Mỗi ca ở đây là một BUG ĐÃ GẶP hoặc một YÊU CẦU trong brief §13.
Test không phải để "cho đủ coverage" - mỗi ca canh giữ một lỗi cụ thể.
"""
import pytest

from youtube_intel.normalize import (
    classify_format, normalize_video, parse_iso8601_duration,
    parse_timestamp, to_bigint, to_bool_or_none,
)


# ---- duration --------------------------------------------------------------
@pytest.mark.parametrize("raw, expected", [
    ("PT12M38S", 758),
    ("PT25H", 90000),         # brief §13: 25 giờ
    ("P1DT1H", 90000),        # brief §13: cùng giá trị, viết khác - KHÔNG mất phần ngày
    ("P1DT2H3M4S", 93784),    # ⭐ bug repo cũ: kiểu TIME cho ra 02:03:04, mất 1 ngày
    ("PT2H55M30S", 10530),    # video Peppa thật
    ("P0D", 0),               # livestream: 0 = "chưa xác định", KHÔNG phải lỗi parse
    ("PT0S", 0),
])
def test_duration_parses_valid_iso8601(raw, expected):
    assert parse_iso8601_duration(raw) == expected


@pytest.mark.parametrize("raw", [
    None, "", "rác", "12M38S",
    "PT", "P",                # ⭐ bug tự tìm ra ở Bước 6: từng trả 0 thay vì None
    "PT12M38Sxxx",            # rác đuôi - regex phải có neo $
])
def test_duration_rejects_garbage_with_none_not_zero(raw):
    # None = "không biết". Trả 0 là BỊA dữ liệu.
    assert parse_iso8601_duration(raw) is None


# ---- số đếm ----------------------------------------------------------------
@pytest.mark.parametrize("raw, expected", [
    ("51372", 51372),
    ("0", 0),                          # brief §13: "0" -> 0 (có thật là không)
    ("139326389154", 139326389154),    # 139 tỷ - tràn INT32, phải là BIGINT
])
def test_to_bigint_parses(raw, expected):
    assert to_bigint(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "  ", "abc", "12.5", "1,234"])
def test_to_bigint_unknown_is_none_never_zero(raw):
    # brief §13: missing commentCount -> NULL. Ép 0 làm AVG sai (đã đo: 50 vs 25).
    assert to_bigint(raw) is None


def test_to_bigint_rejects_bool():
    # bool là lớp con của int trong Python: int(True) == 1. Phải chặn riêng.
    assert to_bigint(True) is None


# ---- ba trạng thái ---------------------------------------------------------
@pytest.mark.parametrize("raw, expected", [
    (True, True), (False, False), ("true", True), ("false", False),
    (None, None),       # "không biết" KHÔNG được thành False - hệ quả pháp lý
    ("maybe", None),
])
def test_made_for_kids_is_three_state(raw, expected):
    assert to_bool_or_none(raw) is expected


# ---- thời gian -------------------------------------------------------------
def test_timestamp_z_suffix_is_utc_and_aware():
    dt = parse_timestamp("2015-03-01T10:00:00Z")
    assert dt is not None and dt.tzinfo is not None
    assert dt.utcoffset().total_seconds() == 0


def test_timestamp_converts_offset_to_utc():
    dt = parse_timestamp("2026-09-21T15:31:49+07:00")
    assert (dt.hour, dt.minute) == (8, 31)


# ---- phân loại -------------------------------------------------------------
@pytest.mark.parametrize("dur, live, label", [
    (758, "none", "normal"),
    (181, "none", "normal"),     # ranh giới: > 180 là CHẮC CHẮN không phải Shorts
    (180, "none", "unknown"),    # <= 180 là CÓ THỂ, KHÔNG CHẮC -> không bịa nhãn shorts
    (68, "none", "unknown"),     # video Wolfoo '#shorts' 68s - luật 60s cũ sẽ gán sai
    (0, "none", "unknown"),      # P0D
    (None, "none", "unknown"),
    (0, "live", "live"),
    (None, "upcoming", "live"),
])
def test_classify_never_claims_shorts_from_duration_alone(dur, live, label):
    got, evidence = classify_format(dur, live)
    assert got == label
    assert evidence, "phải luôn có format_evidence giải thích vì sao"


def test_normalize_video_keeps_missing_stats_as_none():
    v = normalize_video({"video_id": "x", "duration": "PT5M",
                         "publishedAt": "2026-01-01T00:00:00Z",
                         "viewCount": "100"})           # likeCount/commentCount VẮNG
    assert v.view_count == 100
    assert v.like_count is None and v.comment_count is None
