"""
Dashboard smoke tests: every screen renders, with data and empty, using a fake
`queries` module and a fake assistant - no database, no LLM.
"""
import sys
import types
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("streamlit")
import pandas as pd                                                   # noqa: E402
from streamlit.testing.v1 import AppTest                              # noqa: E402

from youtube_intel.assistant.agent import AssistantAnswer, Citation, TraceEntry  # noqa: E402
from youtube_intel.config import load_config                          # noqa: E402

APP = str(Path(__file__).resolve().parents[2] / "dashboard" / "app.py")
NOW = datetime.now(timezone.utc)
PAGES = ["1 · Nguồn & độ tươi", "2 · Video & khoảng quan sát", "3 · Bảng xếp hạng tăng trưởng",
         "4 · Bản tin AI", "5 · Trợ lý AI", "6 · LLM Observability"]
EVIDENCE = {"group": "kids_animation", "group_channels": ["Kênh A"],
            "coverage": {"total_videos": 2, "rankable": 1, "insufficient_history": 1,
                         "latest_observed_at": NOW.isoformat()},
            "candidates": [{"video_id": "vid00000001", "title": "Video A", "channel_title": "Kênh A",
                            "url": "https://www.youtube.com/watch?v=vid00000001",
                            "evidence_ids": [1, 2],
                            "metrics": {"views_per_hour": 100.0, "delta_views": 2400,
                                        "elapsed_hours": 24.0, "total_views": 9000,
                                        "likes": None, "comments": None}}]}
PAYLOAD = {"scope_statement": "Chỉ trong 1 kênh.", "items": [{
    "video_id": "vid00000001", "evidence_ids": [1, 2], "topic_inference": "Chủ đề",
    "why_selected": "Dẫn đầu nhóm", "original_angle": "Một nhân vật mới",
    "limitations": ["Chỉ dựa trên tiêu đề"]}]}


class FakeAssistant:
    def __init__(self):
        self.questions = []

    def ask(self, question, *, history=(), session_id=None, **_):
        self.questions.append((question, list(history)))
        return AssistantAnswer(
            question=question, text="Video A tăng 100 view/giờ.", status="answered",
            citations=(Citation("vid00000001", "Video A", "Kênh A",
                                "https://www.youtube.com/watch?v=vid00000001"),),
            tool_trace=(TraceEntry(1, "top_growing_videos", {"limit": 1}, 1, 12),),
            steps=2, input_tokens=300, output_tokens=40, est_cost_usd=Decimal("0.00007"),
            latency_ms=850, data_as_of=NOW.isoformat(), session_id=session_id or "s")


def fake_queries(*, empty=False, assistant=True):
    m = types.ModuleType("queries")
    cfg = load_config(require=())
    m.config = lambda: cfg
    m.health = lambda: pd.Series({"last_obs": NOW - timedelta(hours=2), "n_obs": 0 if empty else 10,
                                  "n_videos": 0 if empty else 2, "n_runs": 0 if empty else 2,
                                  "n_quarantined": 0})
    growth = pd.DataFrame([] if empty else [
        {"video_id": "vid00000001", "video_title": "Video A", "channel_title": "Kênh A",
         "group_code": "kids_animation", "format_label": "normal", "is_rankable": True,
         "validity_reason": "ok", "start_observed_at": NOW - timedelta(hours=24),
         "end_observed_at": NOW, "elapsed_hours": 24.0, "start_view_count": 6600,
         "end_view_count": 9000, "delta_views": 2400, "views_per_hour": 100.0,
         "end_like_count": None},
        {"video_id": "vid00000002", "video_title": "Video B", "channel_title": "Kênh A",
         "group_code": "kids_animation", "format_label": "unknown", "is_rankable": False,
         "validity_reason": "insufficient_history", "start_observed_at": None,
         "end_observed_at": NOW, "elapsed_hours": None, "start_view_count": None,
         "end_view_count": 50, "delta_views": None, "views_per_hour": None,
         "end_like_count": 3}])
    m.channels = lambda: pd.DataFrame([] if empty else [
        {"channel_id": "UC" + "A" * 22, "title": "Kênh A", "group_code": "kids_animation",
         "enabled": True, "videos": 2, "observations": 3, "last_observed": NOW}])
    m.runs = lambda: pd.DataFrame([] if empty else [
        {"collection_id": "c1", "scheduled_for": NOW - timedelta(hours=1), "started_at": NOW,
         "ended_at": NOW, "status": "succeeded", "code_version": "abc", "passed": 4,
         "quarantined": 0, "rows_received": 200}])
    m.growth = lambda: growth
    m.growth_rankable = lambda: growth[growth["is_rankable"]] if not empty else growth
    m.growth_count = lambda: len(growth)
    m.reports = lambda: pd.DataFrame([] if empty else [
        {"report_id": "00000000-0000-0000-0000-000000000001", "group_code": "kids_animation",
         "as_of": NOW, "status": "validated", "model_name": "gpt-4o-mini-2024-07-18",
         "prompt_version": "v3", "tokens_used": 2048, "latency_ms": 2930, "input_hash": "abc"},
        {"report_id": "00000000-0000-0000-0000-000000000002", "group_code": "kids_animation",
         "as_of": NOW - timedelta(days=1), "status": "draft", "model_name": None,
         "prompt_version": "v3", "tokens_used": None, "latency_ms": None, "input_hash": None}])
    m.report_detail = lambda rid: {"validated_payload": PAYLOAD, "evidence_bundle": EVIDENCE}
    m.report_attempts = lambda rid: {"calls": 2, "attempts": 2, "fallback_used": False,
                                     "est_cost_usd": 0.0004, "validations": "failed → repaired"}
    summary = {} if empty else {"calls": 12, "est_cost_usd": Decimal("0.0123"),
                                "latency_p95_ms": 2900.0, "error_rate": 0.0833,
                                "fallback_rate": 0.0, "validation_fail_rate": None}
    m.llm_summary = lambda days, feats: summary
    m.llm_daily = lambda days, feats: pd.DataFrame([] if empty else [
        {"day": NOW.date(), "feature": "report", "calls": 4, "tokens": 8000, "est_cost_usd": 0.004},
        {"day": NOW.date(), "feature": "assistant", "calls": 8, "tokens": 12000, "est_cost_usd": 0.008}])
    m.llm_by_model = lambda days, feats: pd.DataFrame([] if empty else [
        {"model": "gpt-4o-mini", "prompt_version": "a1", "feature": "assistant", "calls": 8,
         "est_cost_usd": 0.008, "p50_ms": 900, "p95_ms": 2900, "error_rate": 0.0, "fallback_rate": 0.0}])
    m.llm_errors = lambda days, feats: pd.DataFrame([] if empty else [
        {"called_at": NOW, "feature": "report", "model": "gpt-4o-mini", "status": "error",
         "error_type": "APITimeoutError", "validation_status": "not_applicable",
         "fallback_used": True, "latency_ms": 60000}])
    m.eval_results = lambda: pd.DataFrame([] if empty else [
        {"kind": "brief", "version": "v3", "result": "20/20", "detail": "20 synthetic cases",
         "model": "gpt-4o-mini", "run_at": NOW.isoformat(), "file": "v3.json"}])
    fake = FakeAssistant()
    m.assistant = lambda: (fake, None) if assistant else (None, "Thiếu biến môi trường: OPENAI_API_KEY")
    m.fake_assistant = fake
    return m


@pytest.fixture
def app(monkeypatch):
    def make(**kw):
        monkeypatch.setitem(sys.modules, "queries", fake_queries(**kw))
        return AppTest.from_file(APP, default_timeout=30)
    return make


@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("page", PAGES)
def test_every_screen_renders(app, page, empty):
    at = app(empty=empty).run()
    at.sidebar.radio[0].set_value(page).run()
    assert not at.exception, [e.value for e in at.exception]


def test_brief_screen_shows_numbers_from_evidence(app):
    at = app().run()
    at.sidebar.radio[0].set_value("4 · Bản tin AI").run()
    text = " ".join(m.value for m in at.markdown)
    assert "100" in text and "Chủ đề" in text                         # number from evidence, prose from LLM


def test_assistant_answers_with_sources_and_trace(app):
    at = app().run()
    at.sidebar.radio[0].set_value("5 · Trợ lý AI").run()
    at.chat_input[0].set_value("Video nào tăng nhanh nhất?").run()
    assert not at.exception
    text = " ".join(m.value for m in at.markdown)
    assert "100 view/giờ" in text and "youtube.com/watch?v=vid00000001" in text
    assert any("đã kiểm chứng" in c.value for c in at.caption)


def test_assistant_keeps_history_within_the_session(app):
    at = app().run()
    at.sidebar.radio[0].set_value("5 · Trợ lý AI").run()
    at.chat_input[0].set_value("Câu 1").run()
    at.chat_input[0].set_value("Câu 2").run()
    fake = sys.modules["queries"].fake_assistant
    assert fake.questions[1] == ("Câu 2", [("Câu 1", "Video A tăng 100 view/giờ.")])


def test_assistant_without_configuration_shows_guidance(app):
    at = app(assistant=False).run()
    at.sidebar.radio[0].set_value("5 · Trợ lý AI").run()
    assert not at.exception
    assert any("OPENAI_API_KEY" in m.value for m in at.markdown)
