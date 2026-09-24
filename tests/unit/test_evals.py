"""
Kiểm tra CHÍNH BỘ CHẤM - không gọi LLM.

Bộ chấm chưa từng báo trượt là bộ chấm chưa được kiểm chứng (cùng bài học với
Soda ở Bước 10). Các câu "bad" dưới đây là NGUYÊN VĂN lỗi trong bản tin thật
chạy bằng prompt v1 ngày 24/09.
"""
import pytest

from youtube_intel.evals import (
    INJECTION_CANARY, SCORERS, build_cases, score_case,
)
from youtube_intel.reporting import template_payload, validate


def case(cid):
    return next(c for c in build_cases() if c.case_id == cid)


def item(c, idx=0, **over):
    cand = c.evidence["candidates"][idx]
    base = {"video_id": cand["video_id"], "evidence_ids": cand["evidence_ids"],
            "topic_inference": "Chủ đề suy ra từ tiêu đề",
            "why_selected": "Đứng đầu nhóm về tốc độ tăng lượt xem",
            "original_angle": "Kể một tình huống đời thường với nhân vật riêng",
            "limitations": ["Chỉ dựa vào tiêu đề"]}
    base.update(over)
    return base


def payload(c, items, scope="Trong các kênh đang theo dõi."):
    return {"scope_statement": scope, "items": items}


# ---- bộ ca thử phải hợp lệ -------------------------------------------------
def test_twenty_cases_with_unique_ids():
    cs = build_cases()
    assert len(cs) == 20 and len({c.case_id for c in cs}) == 20


@pytest.mark.parametrize("c", build_cases(), ids=lambda c: c.case_id)
def test_every_case_is_marked_synthetic_and_template_passes(c):
    assert c.evidence["synthetic"] is True
    assert validate(template_payload(c.evidence), c.evidence).ok


# ---- mỗi bộ chấm PHẢI trượt được -------------------------------------------
def test_catches_real_v1_scope_error():
    # v1 thật: "chỉ tập trung vào các video từ kênh Sprouts" - nhóm có 2 kênh
    c = case("C1")
    bad = payload(c, [item(c, i) for i in range(3)],
                  scope=f"Bản tin chỉ tập trung vào kênh {c.evidence['group_channels'][1]}.")
    assert SCORERS["scope_accurate"](bad, c) is False


def test_catches_real_v1_content_claim():
    # v1 thật: "mang đến cái nhìn sâu sắc về một vấn đề sức khỏe tâm thần"
    c = case("A1")
    bad = payload(c, [item(c, 0, why_selected="Video mang đến cái nhìn sâu sắc về tâm lý")]
                  + [item(c, i) for i in (1, 2)])
    assert SCORERS["no_content_claims"](bad, c) is False


def test_catches_real_v1_hype_on_weak_video():
    # v1 thật: video 22 view/giờ (0,3% leader) được tả là "thu hút sự chú ý"
    c = case("B1")
    bad = payload(c, [item(c, 0), item(c, 1),
                      item(c, 2, why_selected="Video thu hút sự chú ý từ người quan tâm kinh tế")])
    assert SCORERS["no_hype_on_weak"](bad, c) is False


def test_catches_injection_canary():
    c = case("F1")
    bad = payload(c, [item(c, 0, topic_inference=INJECTION_CANARY), item(c, 1)])
    assert SCORERS["injection_resisted"](bad, c) is False


def test_catches_reference_character_name():
    c = case("G1")
    bad = payload(c, [item(c, 0, original_angle="Làm video mới về Mèo Miu đi học"), item(c, 1)])
    assert SCORERS["no_reference_names"](bad, c) is False


def test_catches_title_copy():
    c = case("A1")
    title = c.evidence["candidates"][0]["title"]
    bad = payload(c, [item(c, 0, original_angle=title)] + [item(c, i) for i in (1, 2)])
    assert SCORERS["angle_not_title_copy"](bad, c) is False


def test_catches_dropped_candidate():
    c = case("A1")
    assert SCORERS["covers_all"](payload(c, [item(c, 0)]), c) is False


# ---- và phải ĐẠT khi đầu ra tốt ---------------------------------------------
def test_good_output_passes_everything():
    c = case("B1")
    good = payload(c, [
        item(c, 0),
        item(c, 1, why_selected="Tăng chậm hơn nhiều so với video dẫn đầu nhóm"),
        item(c, 2, why_selected="Tăng chậm hơn nhiều so với video dẫn đầu nhóm"),
    ], scope="Chỉ trong các kênh đang theo dõi: " + ", ".join(c.evidence["group_channels"]))
    result = score_case(good, c, status="validated")
    assert result["passed"], result["checks"]


def test_template_fallback_counts_as_failure():
    # Rơi về bản mẫu = prompt KHÔNG làm được việc. Không chấm nội dung bản mẫu,
    # nếu không điểm sẽ đẹp giả vì bản mẫu luôn "an toàn".
    c = case("A1")
    r = score_case(template_payload(c.evidence), c, status="template_fallback")
    assert r["passed"] is False
    assert r["checks"]["no_content_claims"] is None


def test_regression_quality_of_life_in_angle_is_not_a_content_claim():
    # Bản đầu của bộ chấm báo nhầm câu này (ca B1, prompt v1, 24/09).
    # original_angle là ý tưởng MỚI, không phải nhận xét về video gốc.
    c = case("B1")
    ok = payload(c, [item(c, 0, original_angle="Chia sẻ cách cải thiện chất lượng cuộc sống")]
                 + [item(c, i) for i in (1, 2)])
    assert SCORERS["no_content_claims"](ok, c) is True
