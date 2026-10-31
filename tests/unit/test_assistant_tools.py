"""Tool registry: schema checks happen before anything runs; errors go back to the LLM."""
import pytest

from youtube_intel.assistant.tools import (
    SUBMIT_ANSWER, ToolResult, build_registry, check_args, finish,
)

GROUPS = ["books_learning", "kids_animation"]


class RecordingExecutor:
    """Stands in for DbToolExecutor; records what would have run."""

    def __init__(self, result=None, exc=None):
        self.result, self.exc, self.ran = result, exc, []

    def execute(self, tool, args):
        self.ran.append((tool.name, args))
        if self.exc:
            raise self.exc
        return self.result or ToolResult(tool=tool.name)


@pytest.fixture
def registry():
    return build_registry(GROUPS, derived_metrics=True)


# ---- derived_metrics_enabled (spec §8, R2) -------------------------------------
class SqlCursor:
    """Records SQL; returns the given rows."""
    def __init__(self, rows=(), one=None):
        self.sql, self._rows, self._one = [], list(rows), one

    def execute(self, sql, params=None):
        self.sql.append(sql)

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._one


def test_flag_off_has_no_channel_comparison_tool():
    assert "compare_channels" not in build_registry(GROUPS).names
    assert "compare_channels" in build_registry(GROUPS, derived_metrics=True).names


def test_flag_off_top_growing_videos_does_not_compute_rank_or_share():
    from youtube_intel.assistant.tools import top_growing_videos
    off, on = SqlCursor(), SqlCursor()
    top_growing_videos(off, max_rows=5)
    top_growing_videos(on, max_rows=5, derived_metrics=True)
    assert "pct_of_leader" not in off.sql[0] and "row_number" not in off.sql[0]
    assert "ORDER BY views_per_hour DESC" in off.sql[0]                  # sorting stays
    assert "pct_of_leader" in on.sql[0] and "row_number" in on.sql[0]


def test_flag_off_registry_binds_the_flag_into_the_handler():
    off = SqlCursor()
    tool = build_registry(GROUPS).get("top_growing_videos")
    tool.handler(off, max_rows=5)
    assert "pct_of_leader" not in off.sql[0]
    assert "share of the leader" not in tool.description


def test_flag_off_latest_brief_hides_llm_topic_notes():
    from youtube_intel.assistant.tools import get_latest_brief
    row = {"as_of": None, "status": "validated", "model_name": "m", "prompt_version": "v3",
           "validated_payload": {"items": [{"video_id": "aB3xYz12345", "topic_inference": "t",
                                            "why_selected": "w", "original_angle": "o"}]},
           "evidence_bundle": {"candidates": [{"video_id": "aB3xYz12345", "metrics": {}}]}}
    off = get_latest_brief(SqlCursor(one=row), max_rows=5, group="g").rows[0]
    on = get_latest_brief(SqlCursor(one=row), max_rows=5, group="g", derived_metrics=True).rows[0]
    assert not {"topic_inference", "why_selected", "original_angle"} & set(off)
    assert on["topic_inference"] == "t"


def test_every_tool_has_a_strict_schema(registry):
    for spec in registry.specs():
        params = spec["function"]["parameters"]
        assert params["type"] == "object" and params["additionalProperties"] is False


def test_no_tool_accepts_free_sql(registry):
    for spec in registry.specs():
        props = spec["function"]["parameters"]["properties"]
        assert not {"sql", "query_sql", "statement"} & set(props)


@pytest.mark.parametrize("name, args, problem", [
    ("top_growing_videos", {"group": "music"}, "must be one of"),          # not a tracked group
    ("top_growing_videos", {"limit": 50}, "<= 10"),                        # above the cap
    ("top_growing_videos", {"limit": "5"}, "expected integer"),
    ("top_growing_videos", {"limit": True}, "expected integer"),           # bool is not int
    ("top_growing_videos", {"sql": "DROP TABLE x"}, "unknown argument"),
    ("get_video_stats", {}, "required"),
    ("get_video_stats", {"video_id": "x'; DROP--"}, "invalid format"),
    ("compare_channels", {"metric": "revenue"}, "must be one of"),
    ("get_video_history", {"video_id": "aB3xYz12345", "limit": 0}, ">= 1"),
])
def test_bad_arguments_never_reach_the_database(registry, name, args, problem):
    ex = RecordingExecutor()
    res = registry.run(name, args, ex)
    assert ex.ran == []
    assert res.error["error"] == "invalid_args" and problem in str(res.error["detail"])


def test_invalid_json_and_unknown_tool_are_structured_errors(registry):
    ex = RecordingExecutor()
    assert registry.run("top_growing_videos", "{not json", ex).error["error"] == "invalid_args"
    assert registry.run("delete_everything", {}, ex).error["error"] == "unknown_tool"
    assert ex.ran == []


def test_valid_arguments_run(registry):
    ex = RecordingExecutor()
    registry.run("top_growing_videos", '{"group": "kids_animation", "limit": 3}', ex)
    assert ex.ran == [("top_growing_videos", {"group": "kids_animation", "limit": 3})]


def test_database_failure_becomes_a_tool_error(registry):
    res = registry.run("list_channels", {}, RecordingExecutor(exc=RuntimeError("canceling statement due to statement timeout")))
    assert res.error["error"] == "timeout"
    res = registry.run("list_channels", {}, RecordingExecutor(exc=RuntimeError("relation missing")))
    assert res.error["error"] == "tool_failed"


def test_submit_answer_is_not_a_data_tool(registry):
    assert registry.run(SUBMIT_ANSWER, {"answer": "x", "cited_video_ids": [], "data_gaps": [],
                                        "status": "answered"}, RecordingExecutor()).error


def test_finish_detects_truncation_and_as_of():
    from datetime import datetime, timezone
    t = datetime(2026, 10, 14, 14, 5, tzinfo=timezone.utc)
    rows = [{"video_id": f"vid{i:08d}", "end_observed_at": t} for i in range(4)]
    r = finish("top_growing_videos", rows, max_rows=3, source="v")
    assert (r.row_count, r.truncated, r.as_of) == (3, True, t.isoformat())
    assert len(r.video_ids) == 3


def test_tool_output_is_json_safe():
    import json
    from decimal import Decimal
    r = ToolResult(tool="t", rows=({"v": Decimal("1.5")},), row_count=1)
    assert json.loads(json.dumps(r.to_llm()))["rows"][0]["v"] == 1.5


def test_check_args_nested_array():
    schema = {"type": "array", "maxItems": 2, "items": {"type": "string"}}
    assert check_args(schema, ["a", 1]) and check_args(schema, ["a", "b", "c"])
    assert check_args(schema, ["a"]) == []
