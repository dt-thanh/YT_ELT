"""Validator LLM: KHÔNG TIN, HÃY KIỂM TRA."""
from youtube_intel.reporting import evidence_hash, render_markdown, template_payload, validate


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


def test_input_hash_ignores_run_time(evidence):
    # as_of là THỜI ĐIỂM CHẠY. Cùng dữ liệu, chạy hai lúc khác nhau -> cùng hash,
    # nếu không cột input_hash mất tác dụng so sánh.
    a = {**evidence, "as_of": "2026-09-24T14:00:00+00:00"}
    b = {**evidence, "as_of": "2026-09-25T14:00:00+00:00"}
    assert evidence_hash(a) == evidence_hash(b)
    assert evidence_hash(a) != evidence_hash({**a, "group": "khác"})


# ---- yti.llm_calls: each attempt logged and marked --------------------------
import dataclasses                                                   # noqa: E402
import json                                                          # noqa: E402

import pytest                                                        # noqa: E402

from youtube_intel.config import load_config                         # noqa: E402
from youtube_intel.llm import MemorySink                             # noqa: E402
from youtube_intel.reporting import generate_payload                 # noqa: E402

from unit.fakes import PRICING, FakeOpenAI, chat_reply               # noqa: E402
from youtube_intel.llm import LLMClient                              # noqa: E402


@pytest.fixture
def settings():
    s = load_config(require=()).settings
    return dataclasses.replace(s, llm_enabled=True, prompt_version="v3")


def _client(*replies):
    sink = MemorySink()
    return LLMClient(model="gpt-4o-mini", pricing=PRICING, sink=sink,
                     openai_client=FakeOpenAI(*replies)), sink


def _reply(*items):
    return chat_reply(json.dumps({"items": list(items)}, ensure_ascii=False))


GOOD = (item(), item(vid="REAL_VIDEO_2", ev=[103, 104]))
BAD = (item(why_selected="tăng 2 triệu view"), item(vid="REAL_VIDEO_2", ev=[103, 104]))


def test_valid_first_try_is_marked_passed(evidence, settings):
    client, sink = _client(_reply(*GOOD))
    _, meta, status = generate_payload(evidence, settings=settings, client=client,
                                       session_id="00000000-0000-0000-0000-0000000000aa")
    assert status == "validated" and meta["attempts"] == 1
    [rec] = sink.records
    assert (rec.validation_status, rec.fallback_used, rec.attempt) == ("passed", False, 1)
    assert rec.feature == "report" and rec.session_id.endswith("aa")


def test_fixed_on_second_try_is_marked_repaired(evidence, settings):
    client, sink = _client(_reply(*BAD), _reply(*GOOD))
    _, _, status = generate_payload(evidence, settings=settings, client=client)
    assert status == "validated"
    assert [(r.attempt, r.validation_status) for r in sink.records] == [(1, "failed"), (2, "repaired")]
    assert not any(r.fallback_used for r in sink.records)


def test_two_failures_fall_back_and_flag_last_call(evidence, settings):
    client, sink = _client(_reply(*BAD), _reply(*BAD))
    payload, _, status = generate_payload(evidence, settings=settings, client=client)
    assert status == "template_fallback" and payload["generated_by"] == "template_fallback"
    assert [r.validation_status for r in sink.records] == ["failed", "failed"]
    assert [r.fallback_used for r in sink.records] == [False, True]


def test_api_error_falls_back_and_is_logged(evidence, settings):
    client, sink = _client(RuntimeError("openai down"))
    _, _, status = generate_payload(evidence, settings=settings, client=client)
    assert status == "template_fallback"
    [rec] = sink.records
    assert (rec.status, rec.fallback_used) == ("error", True)


def test_no_client_means_template_and_no_calls(evidence, settings):
    _, meta, status = generate_payload(evidence, settings=settings, client=None)
    assert status == "template" and meta["attempts"] == 0
