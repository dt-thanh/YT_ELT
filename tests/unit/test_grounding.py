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


# ---- G-01: a number or rank must belong to the entity it is written about -----
C1, C2 = "UC" + "a" * 22, "UC" + "b" * 22


def channels():
    return ToolResult(
        tool="compare_channels", row_count=2,
        rows=({"rank": 1, "channel_id": C1, "channel_title": "Sprouts",
               "median_views_per_hour": 550.0, "pct_of_leader": 100.0, "gap_to_leader": 0},
              {"rank": 2, "channel_id": C2, "channel_title": "FightMediocrity",
               "median_views_per_hour": 260.0, "pct_of_leader": 47.3, "gap_to_leader": 290}))


@pytest.mark.parametrize("answer, fragment", [
    ("Video A tăng 556,1 view/giờ, video B tăng 1.234,56 view/giờ.", "'556,1'"),   # A <-> B swapped
    ("Video B có tổng 3.456.789 lượt xem.", "3.456.789"),                        # A's total put on B
    (f"Video {V2} tăng 1.234,56 view mỗi giờ.", "1.234,56"),                    # swap, entity by id
    ("Video B có 7 lượt thích.", "'7'"),                                          # A's likes on B
    ("Video A đứng thứ 2.", "rank 2"),                                            # wrong rank
    ("Video B dẫn đầu nhóm.", "rank 1"),                                          # B is not the leader
    ("Video B đứng thứ nhất với 556 view/giờ.", "rank 1"),
    ("Video A is ranked 2 in the group.", "rank 2"),
    ("Sprouts có trung vị 260 view/giờ.", "'260'"),                               # channels swapped
    ("FightMediocrity đứng thứ 1 về tốc độ.", "rank 1"),
])
def test_g01_number_or_rank_on_the_wrong_entity_is_rejected(answer, fragment):
    report = ground(answer, [V1], [], [top(), channels()])
    assert not report.ok and any(fragment in e for e in report.errors), report.errors


@pytest.mark.parametrize("answer", [
    "Video A tăng 1.234,56 view/giờ, video B tăng 556,1 view/giờ.",
    "Video B đứng thứ 2 với 556 view/giờ.",
    "Video A dẫn đầu, video B đứng thứ hai.",
    "Video đứng thứ 2 là B, đạt 45% tốc độ của video dẫn đầu.",
    "Có 2 video xếp hạng được; video A dẫn đầu.",
    f"Video {V2} tăng 556 view/giờ.",
    "Sprouts có trung vị 550 view/giờ, cao hơn FightMediocrity 290 view/giờ.",
    "Sprouts cao hơn 290 view/giờ so với FightMediocrity.",       # gap is a relation of both
    "Kênh dẫn đầu là Sprouts với 550 view/giờ.",
])
def test_g01_numbers_on_the_right_entity_pass(answer):
    report = ground(answer, [V1], [], [top(), channels()])
    assert report.ok, report.errors


def test_g01_digits_inside_a_title_are_not_numbers():
    r = ToolResult(tool="get_video_stats", row_count=1, video_ids=(V1,),
                   rows=({"video_id": V1, "video_title": "10 bài học từ Seneca",
                          "views_per_hour": 321.0, "validity_reason": "ok"},))
    assert ground("Video 10 bài học từ Seneca tăng 321 view/giờ.", [V1], [], [r]).ok


def test_g01_rank_claim_without_any_rank_from_tools_is_rejected():
    stats = ToolResult(tool="get_video_stats", row_count=1, video_ids=(V1,),
                       rows=({"video_id": V1, "video_title": "A", "views_per_hour": 1.5,
                              "validity_reason": "ok"},))
    report = ground("Video A dẫn đầu nhóm.", [V1], [], [stats])
    assert not report.ok and any("rank 1" in e for e in report.errors)


# ---- G-02: direction (tăng/giảm) and sign ----------------------------------
def falling():
    return ToolResult(tool="get_video_stats", row_count=1, video_ids=(V1,),
                      rows=({"video_id": V1, "video_title": "A", "start_view_count": 5000,
                             "total_views": 3800, "delta_views": -1200, "views_per_hour": -50.0,
                             "elapsed_hours": 24.0, "validity_reason": "view_count_decreased"},))


GAP = ["YouTube điều chỉnh giảm lượt xem nên không xếp hạng được"]


@pytest.mark.parametrize("answer", [
    "Video A giảm 1.200 lượt xem.",                        # the acceptance criterion
    "Lượt xem của video A sụt khoảng 1,2 nghìn.",
    "Video A mất 50 view mỗi giờ.",
    "Video A lost 1,200 views.",
    "Video A dropped by 1,200 views.",
    "Delta lượt xem của video A là -1.200.",               # explicit minus sign
    "Video A giảm xuống còn 3.800 lượt xem.",              # a level, not a change
    "Lượt xem video A giảm từ 5.000 xuống 3.800.",
])
def test_g02_decrease_matches_a_negative_delta(answer):
    report = ground(answer, [V1], GAP, [falling()])
    assert report.ok, report.errors


@pytest.mark.parametrize("answer", [
    "Video A tăng 1.200 lượt xem.",                        # the acceptance criterion
    "Video A gained 1,200 views.",
    "Video A tăng thêm 50 view mỗi giờ.",
    "Video A thay đổi 1.200 lượt xem.",                    # no direction: sign unknown
])
def test_g02_wrong_or_missing_direction_is_rejected(answer):
    report = ground(answer, [V1], GAP, [falling()])
    assert not report.ok, answer


def test_g02_decrease_is_rejected_for_a_positive_value():
    report = ok("Video A giảm 1.234.567 lượt xem.")        # delta_views is +1,234,567
    assert not report.ok and any("direction" in e for e in report.errors), report.errors


@pytest.mark.parametrize("answer", [
    "Video A mất 24 giờ để đạt mức này.",                  # time spent, not a loss
    "Lượt xem video A: 5.000 - 3.800.",                    # a range dash, not a minus
])
def test_g02_words_that_look_like_a_decrease_but_are_not(answer):
    report = ground(answer, [V1], GAP, [falling()])
    assert report.ok, report.errors


def test_g02_increase_still_matches_positive_values():
    assert ok("Video A tăng thêm 1,2 triệu view trong 24 giờ.").ok
    assert ok("Video A tăng lên 3.456.789 lượt xem.").ok


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
