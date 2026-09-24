"""Validator LLM: KHÔNG TIN, HÃY KIỂM TRA."""
from youtube_intel.reporting import render_markdown, template_payload, validate


def item(vid="REAL_VIDEO_1", ev=None, **over):
    base = {"video_id": vid, "evidence_ids": ev or [101, 102],
            "topic_inference": "Chủ đề thói quen hằng ngày",
            "why_selected": "Tăng nhanh nhất nhóm",
            "original_angle": "Tình huống đời thường với nhân vật riêng",
            "limitations": ["Chưa xem nội dung video"]}
    base.update(over)
    return base


def payload(*items):
    return {"scope_statement": "Trong các nguồn đang theo dõi", "items": list(items)}


def test_valid_output_accepted(evidence):
    assert validate(payload(item()), evidence).ok


def test_rejects_invented_video_id(evidence):
    assert not validate(payload(item(vid="FAKE_999")), evidence).ok


def test_rejects_invented_evidence_id(evidence):
    assert not validate(payload(item(ev=[999])), evidence).ok


def test_rejects_any_digit_in_prose(evidence):
    # ⭐ Ràng buộc CẤU TRÚC: không có chữ số thì không thể bịa số sai.
    r = validate(payload(item(why_selected="tăng 2 triệu view")), evidence)
    assert not r.ok and any("CHỮ SỐ" in e for e in r.errors)


def test_rejects_url_in_prose(evidence):
    assert not validate(payload(item(topic_inference="xem https://evil.example")), evidence).ok


def test_rejects_prompt_injection_outcome(evidence):
    # Tiêu đề độc hại khiến LLM trả id lạ -> hậu quả vẫn bị chặn
    assert not validate(payload(item(vid="ADMIN_OVERRIDE")), evidence).ok


def test_rejects_non_dict():
    assert not validate("không phải json", {"candidates": []}).ok


def test_template_always_passes_validation(evidence):
    # Lưới an toàn cuối cùng phải LUÔN hợp lệ - nếu không thì nó vô dụng
    assert validate(template_payload(evidence), evidence).ok


def test_numbers_rendered_from_evidence_not_llm(evidence):
    # LLM viết gì cũng được - con số trên bản tin phải khớp evidence
    md = render_markdown(payload(item()), evidence)
    assert "2,400" in md and "100" in md


# ---- [v3] chặn dùng lại tên nhân vật/kênh tham khảo --------------------------
def _with_names(evidence, names):
    return {**evidence, "forbidden_names": names}


def test_rejects_reference_name_in_original_angle(evidence):
    ev = _with_names(evidence, ["Wolfoo"])
    r = validate(payload(item(original_angle="Làm video mới về Wolfoo đi cắm trại")), ev)
    assert not r.ok and any("Wolfoo" in e for e in r.errors)


def test_reference_name_check_is_case_insensitive(evidence):
    ev = _with_names(evidence, ["Heo Peppa"])
    assert not validate(payload(item(original_angle="kể chuyện heo peppa đi biển")), ev).ok


def test_reference_name_allowed_when_describing_source_video(evidence):
    # topic_inference NÓI VỀ video gốc -> nhắc tên là đúng, không được chặn
    ev = _with_names(evidence, ["Wolfoo"])
    r = validate(payload(item(topic_inference="Tình huống gia đình của Wolfoo")), ev)
    assert r.ok, r.errors
