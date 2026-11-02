"""R-06: daily market report. Fake tools; the report must pass its own grounding check."""
import dataclasses
import json
from datetime import date, datetime, timedelta, timezone

from studio.agents.report import ALLOWED_TOOLS, ReportAgent, validated_streak
from studio.agents.research import ToolBox
from studio.gateway import Gateway, load_gateway_config
from youtube_intel.assistant.tools import build_registry, finish
from youtube_intel.llm import LLMClient, MemorySink
from youtube_intel.studio_config import load_studio_config

from unit.fakes import PRICING, FakeOpenAI, chat_reply

NOW = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
GROUPS = ["books_learning", "kids_animation", "stoic", "east", "fable"]
SETTINGS = load_studio_config().report
EN, VI = "UC" + "e" * 22, "UC" + "v" * 22


class Exec:
    def __init__(self, channels=True):
        self.channels = channels
        self.calls = []

    def execute(self, tool, args):
        self.calls.append(tool.name)
        chans = [{"channel_id": EN, "channel_title": "Daily Stoic", "language": "en"},
                 {"channel_id": VI, "channel_title": "Khắc Kỷ 365", "language": "vi"}]
        videos = {EN: [{"video_id": "repVid00001", "video_title": "7 Stoic rules for anger",
                            "channel_id": EN, "published_at": NOW - timedelta(days=2),
                            "total_views": 88000, "likes": 4100, "delta_views": 29628,
                            "elapsed_hours": 24.0, "views_per_hour": 1234.5,
                            "end_observed_at": NOW}],
                  VI: []}
        rows = {"list_channels": chans if self.channels else [],
                "top_growing_videos": videos.get(args.get("channel_id"), []),
                "get_topic_clusters": [{"cluster_id": 3, "label": None, "n_videos": 6,
                                        "n_channels": 4, "languages": ["en", "zh"],
                                        "is_rising": True, "is_gap": True, "is_saturated": False}],
                "get_signals": [{"video_id": "repVid00001", "video_title": "7 Stoic rules for anger",
                                 "channel_id": EN, "s1": 3.2, "s1_label": "nổi trội",
                                 "views": 88000}],
                "get_comment_insights": [{"question_id": 1, "kind": "question",
                                          "paraphrase_vi": "Làm sao bớt nóng giận với con cái",
                                          "n_comments": 14, "n_videos": 3}]}[tool.name]
        return finish(tool.name, rows, max_rows=20, source="fake")


def agent(*, derived=False, channels=True, settings=SETTINGS, gateway=None):
    box = ToolBox(build_registry(GROUPS, derived_metrics=derived), Exec(channels),
                  allowed=ALLOWED_TOOLS)
    return ReportAgent(box, gateway, derived_metrics=derived, settings=settings, now=NOW)


def test_basic_report_lists_new_videos_per_channel_and_passes_grounding():
    rep = agent().run("stoic")
    assert rep.validated, rep.grounding_errors
    md = rep.markdown
    assert "88,000 view" in md and "29,628" in md and "1,234.5 view/giờ" in md
    assert "tính độc lập" in md and "youtube.com/watch?v=repVid00001" in md
    assert "Khắc Kỷ 365 (vi)" in md and "Không có video mới" in md
    assert "S4" not in md and "Người xem" not in md                  # advanced sections absent
    assert rep.data_date == "2026-11-02"


def test_derived_report_adds_topics_outliers_and_questions():
    rep = agent(derived=True).run("stoic")
    assert rep.validated, rep.grounding_errors
    md = rep.markdown
    assert "Khoảng trống EN/ZH so với VI (S5)" in md and "6 video, 4 kênh, ngôn ngữ en, zh" in md
    assert "S1 3.2 (nổi trội)" in md and "Làm sao bớt nóng giận với con cái (14 bình luận" in md


def test_a_wrong_number_in_the_rendering_is_caught():
    a = agent()
    a._render_orig = a._render

    def broken(pillar):
        lines, gaps = a._render_orig(pillar)
        return [ln.replace("29,628", "39,628") for ln in lines], gaps
    a._render = broken
    rep = a.run("stoic")
    assert not rep.validated and any("39,628" in e for e in rep.grounding_errors)


def test_no_channel_in_the_pillar_yet():
    rep = agent(channels=False).run("east")
    assert rep.validated and "Chưa có kênh east" in rep.markdown


def gateway_with(*replies):
    fake, sink = FakeOpenAI(*replies), MemorySink()
    client = LLMClient(model="gpt-4o-mini", pricing=PRICING, sink=sink, openai_client=fake)
    return Gateway(load_gateway_config(), client_for=lambda m: client, pricing=PRICING,
                   job_spent=lambda j: 0), fake


def test_llm_summary_is_off_by_default_and_never_called():
    gw, fake = gateway_with()
    rep = agent(gateway=gw).run("stoic")
    assert SETTINGS.llm_summary is False and rep.summary is None and fake.requests == []


def test_an_ungrounded_summary_is_dropped_but_the_report_stays():
    bad = chat_reply(json.dumps({"summary_vi": "Kênh Daily Stoic đạt 2 triệu lượt xem hôm nay."}))
    gw, fake = gateway_with(bad, bad)
    rep = agent(gateway=gw, settings=dataclasses.replace(SETTINGS, llm_summary=True)).run("stoic")
    assert rep.summary is None and rep.validated and len(fake.requests) == 2


def test_validated_streak_counts_back_from_today():
    today = date(2026, 11, 8)
    days = [today - timedelta(days=i) for i in range(7)] + [date(2026, 10, 20)]
    assert validated_streak(days, today) == 7
    assert validated_streak(days[1:], today) == 0


def test_a_pillar_without_channels_is_still_a_valid_tool_argument():
    # Found by a live run: groups came from configured channels only, so
    # list_channels(group="stoic") was rejected as an invalid argument.
    from youtube_intel.cli import _tool_groups
    from youtube_intel.config import load_config
    assert {"stoic", "east", "fable"} <= set(_tool_groups(load_config(require=())))
