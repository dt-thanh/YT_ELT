"""
Dashboard smoke tests: every screen renders, with data and empty, using a fake
`queries` module and a fake assistant - no database, no LLM.
"""
import dataclasses
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
PAGES = ["1 · Nguồn & độ tươi", "2 · Video & khoảng quan sát", "3 · Tăng view từng video",
         "4 · Bản tin AI", "5 · Trợ lý AI", "6 · LLM Observability", "7 · Hộp thư duyệt"]
PAGE3_DERIVED = "3 · Bảng xếp hạng tăng trưởng"
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


def fake_queries(*, empty=False, assistant=True, derived=False, payload=None):
    m = types.ModuleType("queries")
    cfg = load_config(require=())
    cfg = dataclasses.replace(cfg, settings=dataclasses.replace(
        cfg.settings, derived_metrics_enabled=derived))
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
    m.report_detail = lambda rid: {"validated_payload": payload or PAYLOAD,
                                   "evidence_bundle": EVIDENCE}
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
    m.InboxError = type("InboxError", (Exception,), {})
    m.decisions = []
    jobs = [] if empty else [
        {"job_id": 1, "state": "awaiting_topic_approval", "title": "Còn Bảy Ngày tập 2",
         "pillar": "ai_film", "format": "film_episode", "series_id": "con_bay_ngay", "episode": 2,
         "proposal": {"idea": "An thuyết phục lính canh.", "why": "Mô-típ xuyên không.",
                      "evidence": ["https://www.youtube.com/watch?v=vid00000001"]},
         "outputs": {}, "feedback": None, "minutes": 0.0, "script_path": None},
        {"job_id": 2, "state": "awaiting_script_approval", "title": "Tập 1", "pillar": "ai_film",
         "format": "film_episode", "series_id": "con_bay_ngay", "episode": 1,
         "proposal": {"idea": "x" * 12, "why": "y" * 12}, "outputs": {},
         "feedback": "hook yếu quá", "minutes": 3.0, "script_path": "content/films/x/ep01.yaml"},
        {"job_id": 3, "state": "awaiting_final_approval", "title": "Tập 0", "pillar": "ai_film",
         "format": "film_episode", "series_id": "s", "episode": 3,
         "proposal": {"idea": "x" * 12, "why": "y" * 12}, "feedback": None, "minutes": 5.0,
         "outputs": {"short": {"path": "/nowhere/short.mp4", "seconds": 47.3, "qc_ok": True}},
         "script_path": None},
        {"job_id": 4, "state": "ready_to_publish", "title": "Tập cũ", "pillar": "ai_film",
         "format": "film_episode", "series_id": "s", "episode": 4, "proposal": {},
         "outputs": {}, "feedback": None, "minutes": 9.0, "script_path": None}]
    m.inbox = lambda: jobs
    m.screenplay = lambda job: "CÒN BẢY NGÀY - TẬP 1\n1. MỞ ĐẦU - Phố cổ\n   Người dẫn: Còn bảy ngày."

    def decide(job_id, decision, reason, minutes):
        if decision != "approved" and not (reason or "").strip():
            raise m.InboxError("say why (--reason)")
        m.decisions.append((job_id, decision, reason, minutes))
        return "writing"
    m.decide = decide
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


@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("page", [p for p in PAGES if p[0] != "3"] + [PAGE3_DERIVED])
def test_every_screen_renders_with_derived_metrics(app, page, empty):
    at = app(empty=empty, derived=True).run()
    at.sidebar.radio[0].set_value(page).run()
    assert not at.exception, [e.value for e in at.exception]


def test_flag_off_growth_screen_has_no_ranking(app):
    at = app().run()
    assert PAGE3_DERIVED not in at.sidebar.radio[0].options
    at.sidebar.radio[0].set_value("3 · Tăng view từng video").run()
    assert not at.exception
    assert not at.get("vega_lite_chart")                          # no leaderboard chart
    assert not any("Bảng xếp hạng" in t.value for t in at.title)
    assert any("(tính độc lập)" in c for df in at.dataframe for c in df.value.columns)
    on = app(derived=True).run()
    on.sidebar.radio[0].set_value(PAGE3_DERIVED).run()
    assert on.get("vega_lite_chart")


def test_flag_off_basic_brief_is_labelled(app):
    from youtube_intel.reporting import basic_payload
    at = app(payload=basic_payload(EVIDENCE)).run()
    at.sidebar.radio[0].set_value("4 · Bản tin AI").run()
    at.selectbox[1].set_value(1).run()                             # the 'draft' report
    assert not at.exception
    assert any("Chế độ cơ bản" in i.value for i in at.info)
    assert "Chủ đề suy ra" not in " ".join(m.value for m in at.markdown)


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


def test_market_report_is_shown_as_rendered(app):
    payload = {"kind": "market_report", "markdown": "# Bản tin thị trường: stoic\n\n- 88,000 view",
               "grounding_errors": ["number '1' does not match"]}
    at = app(payload=payload).run()
    at.sidebar.radio[0].set_value("4 · Bản tin AI").run()
    assert not at.exception
    assert any("Bản tin thị trường: stoic" in m.value for m in at.markdown)
    assert any("Kiểm chứng" in c.value for c in at.caption)


def test_inbox_shows_each_gate_and_records_the_owners_click(app):
    at = app().run()
    at.sidebar.radio[0].set_value("7 · Hộp thư duyệt").run()
    assert not at.exception, [e.value for e in at.exception]
    heads = [h.value for h in at.header]
    assert heads[:3] == ["1 · Chủ đề (1)", "2 · Kịch bản (1)", "3 · Video (1)"]
    assert "Sẵn sàng đăng (1)" in heads
    assert any("Còn bảy ngày" in t.value for t in at.text)               # screenplay, not YAML
    assert any("hook yếu quá" in w.value for w in at.warning)            # open feedback shown
    assert any("/nowhere/short.mp4" in c.value for c in at.caption)      # missing file, no crash
    at.button[0].click().run()                                           # job 1: Đồng ý
    assert sys.modules["queries"].decisions == [(1, "approved", "", None)]


def test_inbox_refuses_a_no_without_a_reason(app):
    at = app().run()
    at.sidebar.radio[0].set_value("7 · Hộp thư duyệt").run()
    at.button[2].click().run()                                           # job 1: Bỏ, no reason
    assert any("Không ghi được" in e.value for e in at.error)
    assert sys.modules["queries"].decisions == []
