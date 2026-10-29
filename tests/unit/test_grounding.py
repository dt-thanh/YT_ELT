"""Grounding validator: every number, date and video id must come from tool results."""
import pytest

from youtube_intel.assistant.grounding import extract_numbers, ground, number_readings
from youtube_intel.assistant.tools import ToolResult, error_result

V1, V2 = "aB3xYz12345", "Qq9_zz-AAAA"


def top(**extra_rows):
    return ToolResult(
        tool="top_growing_videos", source="yti.v_video_growth", row_count=2,
        video_ids=(V1, V2), as_of="2026-10-14T14:05:00+00:00",
        rows=({"rank": 1, "video_id": V1, "video_title": "A", "views_per_hour": 1234.56,
               "delta_views": 1234567, "elapsed_hours": 24.0, "total_views": 3456789,
               "pct_of_leader": 100.0, "likes": 7, "end_observed_at": "2026-10-14T14:05:00+00:00"},
              {"rank": 2, "video_id": V2, "video_title": "B", "views_per_hour": 556.1,
               "delta_views": 13346, "elapsed_hours": 24.0, "total_views": 98000,
               "pct_of_leader": 45.0, "likes": None, **extra_rows}))


def ok(answer, cited=(V1,), gaps=(), results=None, status="answered"):
    return ground(answer, list(cited), list(gaps), results or [top()], status=status)


# ---- number parsing --------------------------------------------------------
@pytest.mark.parametrize("token, expected", [
    ("1234", {1234}),
    ("1.234.567", {1234567}),             # Vietnamese thousands
    ("1,234,567", {1234567}),             # English thousands
    ("12,5", {12.5}),                     # Vietnamese decimal
    ("12.5", {12.5}),                     # English decimal
    ("1.234", {1234, 1.234}),             # ambiguous: both readings kept
    ("1,234", {1234, 1.234}),
    ("1.234.567,89", {1234567.89}),       # Vietnamese thousands + decimal
    ("1,234,567.89", {1234567.89}),       # English thousands + decimal
])
def test_number_readings(token, expected):
    assert {float(x) for x in number_readings(token)} == {float(x) for x in expected}


@pytest.mark.parametrize("text, value, percent", [
    ("1,2 triệu", 1_200_000, False),
    ("1.2M", 1_200_000, False),
    ("3.4K", 3_400, False),
    ("3 nghìn", 3_000, False),
    ("2 tỷ", 2_000_000_000, False),
    ("45%", 45, True),
    ("45 phần trăm", 45, True),
])
def test_scales_and_percent(text, value, percent):
    [m] = extract_numbers(text)
    assert float(value) in {float(x) for x in m.readings} and m.is_percent == percent


@pytest.mark.parametrize("text", ["gpt-4o-mini", "prompt v3", "kids_animation", "mp4"])
def test_identifiers_are_not_numbers(text):
    assert extract_numbers(text) == []


# ---- grounded answers pass ---------------------------------------------------
@pytest.mark.parametrize("answer", [
    "Video A tăng 1.234,56 view mỗi giờ.",                 # exact, Vietnamese format
    "Video A gains 1,234.56 views per hour.",              # exact, English format
    "Video A tăng khoảng 1.235 view/giờ.",                 # rounded to integer
    "Video A tăng thêm 1,2 triệu view trong 24 giờ.",      # rounded to millions
    "Video A has 3.5M total views.",                       # 3,456,789 -> 3.5M
    "Video B đạt 45% tốc độ của video dẫn đầu.",           # percent field
    "Video B tăng 556 view/giờ, đứng thứ 2.",              # rounded + rank
    "Có 2 video đủ điều kiện xếp hạng.",                   # small int = row_count
    "Video A có 7 lượt thích.",                            # small int = exact integer field
    "Số liệu quan sát ngày 2026-10-14.",                   # ISO date
    "Quan sát lúc 21:05 ngày 14/10/2026 (giờ Việt Nam).",  # VN time and date
])
def test_grounded_answers_pass(answer):
    report = ok(answer)
    assert report.ok, report.errors


def test_percent_from_fraction_field():
    r = ToolResult(tool="get_data_coverage", row_count=1, rows=({"error_rate": 0.25},))
    assert ground("Tỉ lệ lỗi là 25%.", [], [], [r]).ok


# ---- invented facts are rejected ------------------------------------------
@pytest.mark.parametrize("answer, fragment", [
    ("Video A tăng 2 triệu view.", "2 triệu"),             # invented big number
    ("Video A tăng 1.300 view/giờ.", "1.300"),             # off by more than rounding
    ("Có 5 video tăng mạnh.", "'5'"),                      # small int not a rank/count
    ("Video B đạt 46% so với dẫn đầu.", "46%"),            # wrong percent
    ("Quan sát ngày 2026-10-13.", "2026-10-13"),           # date not in results
    ("Cập nhật lúc 09:30.", "09:30"),                      # time not in results
    ("Xem tại https://youtu.be/aB3xYz12345", "URL"),       # URL
    ("Video ZZZZZZZZZ99 tăng mạnh.", "ZZZZZZZZZ99"),       # invented video id in text
])
def test_invented_facts_are_rejected(answer, fragment):
    report = ok(answer)
    assert not report.ok and any(fragment in e for e in report.errors), report.errors


def test_cited_video_must_come_from_a_tool():
    report = ok("Video A tăng nhanh nhất.", cited=(V1, "FAKEFAKE123"))
    assert not report.ok and any("FAKEFAKE123" in e for e in report.errors)


def test_ordinary_long_word_is_not_a_video_id():
    assert ok("This is a performance summary.").ok         # 'performance' has 11 letters


def test_ids_in_text_are_not_read_as_numbers():
    assert ok(f"Video {V1} dẫn đầu nhóm.").ok


def test_numbers_from_a_previous_tool_in_the_same_turn_count():
    cov = ToolResult(tool="get_data_coverage", row_count=1,
                     rows=({"group_code": "kids_animation", "total_videos": 105,
                            "rankable_videos": 2},))
    assert ground("Nhóm có 105 video, 2 video xếp hạng được.", [], [], [top(), cov]).ok


# ---- data gaps ---------------------------------------------------------------
def test_empty_result_requires_data_gaps():
    empty = ToolResult(tool="top_growing_videos", row_count=0)
    assert not ground("Chưa có video nào.", [], [], [empty]).ok
    assert ground("Chưa có video nào.", [], ["Không có video đủ điều kiện xếp hạng"], [empty]).ok


def test_tool_error_requires_data_gaps():
    assert not ground("Không lấy được dữ liệu.", [], [], [error_result("get_video_stats", "timeout", "x")]).ok


def test_not_rankable_video_requires_data_gaps():
    stats = ToolResult(tool="get_video_stats", row_count=1, video_ids=(V1,),
                       rows=({"video_id": V1, "validity_reason": "insufficient_history",
                              "views_per_hour": None},))
    assert not ground("Chưa tính được tăng trưởng.", [V1], [], [stats]).ok
    assert ground("Chưa tính được tăng trưởng.", [V1], ["Mới có một lần quan sát"], [stats]).ok


def test_declined_answer_needs_no_gaps_or_tools():
    report = ground("Xin lỗi, tôi chỉ trả lời về các kênh đang theo dõi.", [], [], [],
                    status="declined")
    assert report.ok and report.numbers_checked == 0


def test_declined_answer_still_cannot_invent_numbers():
    assert not ground("MrBeast có 300 triệu subscriber.", [], [], [], status="declined").ok
