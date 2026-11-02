"""R-05: Research agent -> TopicBrief. Fake tools, fake OpenAI, no network, no database."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from studio.agents.research import ResearchAgent, ToolBox, ToolNotAllowed
from studio.gateway import Gateway, load_gateway_config
from studio.schemas import ResearchRequest, TopicBrief
from youtube_intel.assistant.tools import ToolResult, build_registry, finish
from youtube_intel.llm import LLMClient, MemorySink

from unit.fakes import PRICING, FakeOpenAI, chat_reply

NOW = datetime(2026, 11, 2, tzinfo=timezone.utc)
GROUPS = ["books_learning", "kids_animation", "stoic", "east", "fable"]
V1, V2, OLD = "stoicVid001", "stoicVid002", "stoicOld001"


class FakeExecutor:
    """Canned rows per tool, shaped like the real SQL results."""
    def __init__(self):
        self.calls = []

    def execute(self, tool, args):
        self.calls.append(tool.name)
        rows = {
            "list_channels": [{"channel_id": "UCen", "channel_title": "Daily Stoic",
                               "language": "en"},
                              {"channel_id": "UCvi", "channel_title": "Khắc Kỷ VN",
                               "language": "vi"}],
            "top_growing_videos": [
                {"video_id": V1, "video_title": "Marcus Aurelius on anger", "channel_id": "UCen",
                 "channel_title": "Daily Stoic", "published_at": NOW - timedelta(days=3),
                 "views_per_hour": 1234.5, "delta_views": 29628, "total_views": 88000,
                 "likes": 4100, "comments": 310, "end_observed_at": NOW},
                {"video_id": V2, "video_title": "Kiểm soát cơn giận", "channel_id": "UCvi",
                 "channel_title": "Khắc Kỷ VN", "published_at": NOW - timedelta(days=5),
                 "views_per_hour": 52.0, "delta_views": 1248, "total_views": 9100,
                 "likes": None, "comments": 12, "end_observed_at": NOW},
                {"video_id": OLD, "video_title": "Old", "channel_id": "UCen",
                 "published_at": NOW - timedelta(days=40), "views_per_hour": 9.0,
                 "delta_views": 216, "total_views": 5000, "end_observed_at": NOW}],
            "get_signals": [{"video_id": V1, "channel_id": "UCen", "language": "en", "s1": 3.2,
                             "s1_label": "nổi trội", "views": 88000}],
            "get_topic_clusters": [{"cluster_id": 7, "languages": ["en", "zh"], "is_rising": True,
                                    "is_gap": True, "is_saturated": False,
                                    "member_video_ids": [V1, V2]}],
            "get_comment_insights": [{"question_id": 1, "kind": "question",
                                      "paraphrase_vi": "Làm sao giữ bình tĩnh khi bị sếp mắng"}],
        }[tool.name]
        return finish(tool.name, rows, max_rows=20, source="fake")


def agent(*replies, derived=False):
    reg = build_registry(GROUPS, derived_metrics=derived)
    ex = FakeExecutor()
    fake, sink = FakeOpenAI(*replies), MemorySink()
    client = LLMClient(model="gpt-4o-mini", pricing=PRICING, sink=sink, openai_client=fake)
    gw = Gateway(load_gateway_config(), client_for=lambda m: client, pricing=PRICING,
                 job_spent=lambda j: 0)
    return ResearchAgent(ToolBox(reg, ex), gw, derived_metrics=derived, now=NOW), ex, fake, sink


def drafts(*briefs):
    return chat_reply(json.dumps({"briefs": list(briefs)}, ensure_ascii=False))


GOOD = {"working_title": "Marcus Aurelius và cơn giận nơi công sở", "video_ids": [V1, V2],
        "angles_taken": ["Đọc trích dẫn kèm hình minh họa"],
        "proposed_angle": "Suy tưởng quyển hai, một buổi họp bị sếp mắng oan, bài tập thở chậm.",
        "confidence": "medium"}
REQ = ResearchRequest(pillar="stoic")


def test_basic_mode_brief_has_raw_numbers_from_tools_and_no_advanced_fields():
    a, ex, fake, sink = agent(drafts(GOOD))
    out = a.run(REQ)
    [b] = out.briefs
    assert isinstance(b, TopicBrief) and not b.derived_metrics and not out.repaired
    e1 = b.evidence[0]
    assert (e1.video_id, e1.views, e1.delta_views, e1.views_per_hour, e1.language) == \
        (V1, 88000, 29628, 1234.5, "en")
    assert e1.url.endswith(V1) and e1.s1 is None
    assert (b.languages_up, b.vi_saturation, b.viewer_questions) == ([], None, [])
    assert "get_signals" not in ex.calls                         # advanced tools never called
    evidence = fake.requests[0]["messages"][1]["content"]
    assert OLD not in evidence                                   # outside the 14-day window
    assert {r.agent for r in sink.records} == {"research"}


def test_a_brief_citing_an_unknown_video_gets_one_repair():
    bad = {**GOOD, "video_ids": ["notInEvidence"]}
    a, _, fake, _ = agent(drafts(bad), drafts(GOOD))
    out = a.run(REQ)
    assert out.repaired and len(out.briefs) == 1 and out.dropped == []
    assert "notInEvidence" in fake.requests[1]["messages"][-1]["content"]


def test_invented_numbers_in_prose_are_dropped_after_the_repair():
    invented = {**GOOD, "proposed_angle": "Chủ đề này đã có 2 triệu lượt xem."}
    a, _, _, _ = agent(drafts(invented), drafts(invented))
    out = a.run(REQ)
    assert out.briefs == [] and any("2 triệu" in e for e in out.dropped[0]["errors"])


def test_a_number_written_about_the_wrong_video_is_caught_by_grounding():
    swapped = {**GOOD, "angles_taken": ["Kiểm soát cơn giận tăng 29.628 view"]}  # that is V1's
    a, _, _, _ = agent(drafts(swapped), drafts(swapped))
    out = a.run(REQ)
    assert out.briefs == [] and "comes from another video" in out.dropped[0]["errors"][0]


def test_derived_mode_adds_languages_saturation_and_viewer_questions():
    a, ex, _, _ = agent(drafts(GOOD), derived=True)
    [b] = a.run(REQ).briefs
    assert b.derived_metrics and b.languages_up == ["en", "zh"]
    assert b.vi_saturation == "none"                              # the cluster is a gap (S5)
    assert b.viewer_questions == ["Làm sao giữ bình tĩnh khi bị sếp mắng"]
    assert b.evidence[0].s1 == 3.2 and b.evidence[0].s1_label == "nổi trội"


def test_no_recent_candidate_means_no_llm_call():
    a, ex, fake, _ = agent(drafts(GOOD))
    a.now = NOW + timedelta(days=60)
    out = a.run(REQ)
    assert out.briefs == [] and "no tracked stoic video" in out.note and fake.requests == []


def test_the_toolbox_enforces_the_agents_tool_list():
    reg = build_registry(GROUPS, derived_metrics=False)
    box = ToolBox(reg, FakeExecutor())
    with pytest.raises(ToolNotAllowed, match="not available"):
        box.call("get_signals", group="stoic")                    # flag off: not registered
    with pytest.raises(ToolNotAllowed, match="may not call"):
        ToolBox(reg, FakeExecutor(), allowed=("list_channels",)).call("top_growing_videos")
