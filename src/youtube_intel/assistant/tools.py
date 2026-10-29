"""
Read-only tools the assistant's LLM may call.

Rules every tool follows:
  * Pre-written SQL with bound parameters. No tool accepts SQL from the LLM.
  * Arguments are checked against the tool's JSON Schema before anything runs;
    bad arguments come back to the LLM as {"error": "invalid_args", ...}, never
    as an exception.
  * Executed by DbToolExecutor in a READ ONLY transaction, with a statement
    timeout, as the SELECT-only login yti_assistant (migration 005).
  * At most `max_rows` rows; `truncated` says when more existed.
  * Derived metrics users ask about (share of the leader, gap to the leader)
    are computed here, in SQL - the LLM never does arithmetic.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable

logger = logging.getLogger(__name__)

VIDEO_ID_PATTERN = r"^[A-Za-z0-9_-]{5,32}$"
CHANNEL_ID_PATTERN = r"^UC[A-Za-z0-9_-]{22}$"
FORMATS = ["normal", "unknown", "live", "shorts"]
COMPARE_METRICS = ["median_views_per_hour", "total_delta_views", "rankable_videos"]
SUBMIT_ANSWER = "submit_answer"

# Timestamp columns used to compute a result's as_of (freshest observation it saw).
_AS_OF_COLUMNS = ("end_observed_at", "latest_observed_at", "last_observed_at",
                  "observed_at", "as_of")


# =============================================================================
# Result type
# =============================================================================

@dataclass(frozen=True)
class ToolResult:
    tool: str
    rows: tuple[dict, ...] = ()
    row_count: int = 0
    truncated: bool = False
    source: str = ""
    as_of: str | None = None
    video_ids: tuple[str, ...] = ()
    extra: dict = field(default_factory=dict)      # tool-level facts (e.g. brief status)
    error: dict | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_llm(self) -> dict:
        """JSON-safe view sent to the LLM (and stored in eval transcripts)."""
        if self.error:
            return {"tool": self.tool, **self.error}
        return to_jsonable({
            "tool": self.tool, "source": self.source, "row_count": self.row_count,
            "truncated": self.truncated, "as_of": self.as_of, "rows": list(self.rows),
            **({"extra": self.extra} if self.extra else {}),
        })


def error_result(tool: str, error: str, detail: Any) -> ToolResult:
    return ToolResult(tool=tool, error={"error": error, "detail": detail})


def to_jsonable(value):
    """Decimal -> float, datetime/date -> ISO string, recursively."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    return value


def finish(tool: str, rows: list[dict], *, max_rows: int, source: str,
           extra: dict | None = None) -> ToolResult:
    """Build a ToolResult from up to max_rows+1 fetched rows."""
    truncated = len(rows) > max_rows
    rows = [dict(r) for r in rows[:max_rows]]
    stamps = [r[c] for r in rows for c in _AS_OF_COLUMNS
              if isinstance(r.get(c), datetime)]
    if extra:
        stamps += [v for k, v in extra.items() if k in _AS_OF_COLUMNS and isinstance(v, datetime)]
    vids = tuple(dict.fromkeys(r["video_id"] for r in rows if r.get("video_id")))
    return ToolResult(tool=tool, rows=tuple(rows), row_count=len(rows), truncated=truncated,
                      source=source, as_of=max(stamps).isoformat() if stamps else None,
                      video_ids=vids, extra=extra or {})


# =============================================================================
# Minimal JSON Schema check (the subset our tool schemas use)
# =============================================================================

_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool,
          "array": list, "object": dict}


def check_args(schema: dict, args: Any, path: str = "") -> list[str]:
    """Return a list of problems; empty means valid. Supports type, enum,
    minimum/maximum, minLength/maxLength, pattern, items, maxItems, required,
    properties and additionalProperties: false."""
    errors: list[str] = []
    where = path or "arguments"
    expected = schema.get("type")
    if expected:
        py = _TYPES[expected]
        # bool is a subclass of int in Python - reject it for integer/number.
        if not isinstance(args, py) or (expected in ("integer", "number") and isinstance(args, bool)):
            return [f"{where}: expected {expected}"]
    if "enum" in schema and args not in schema["enum"]:
        errors.append(f"{where}: must be one of {schema['enum']}")
    if isinstance(args, (int, float)) and not isinstance(args, bool):
        if "minimum" in schema and args < schema["minimum"]:
            errors.append(f"{where}: must be >= {schema['minimum']}")
        if "maximum" in schema and args > schema["maximum"]:
            errors.append(f"{where}: must be <= {schema['maximum']}")
    if isinstance(args, str):
        if len(args) < schema.get("minLength", 0):
            errors.append(f"{where}: too short")
        if "maxLength" in schema and len(args) > schema["maxLength"]:
            errors.append(f"{where}: longer than {schema['maxLength']} characters")
        if "pattern" in schema and not re.match(schema["pattern"], args):
            errors.append(f"{where}: invalid format")
    if isinstance(args, list):
        if "maxItems" in schema and len(args) > schema["maxItems"]:
            errors.append(f"{where}: more than {schema['maxItems']} items")
        for i, item in enumerate(args):
            errors += check_args(schema.get("items", {}), item, f"{where}[{i}]")
    if isinstance(args, dict):
        props = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in args:
                errors.append(f"{where}.{name}: required")
        if schema.get("additionalProperties") is False:
            for name in args:
                if name not in props:
                    errors.append(f"{where}.{name}: unknown argument")
        for name, value in args.items():
            if name in props:
                errors += check_args(props[name], value, f"{where}.{name}" if path else name)
    return errors


# =============================================================================
# Tool definition and registry
# =============================================================================

Handler = Callable[..., ToolResult]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict                  # JSON Schema, additionalProperties: false
    handler: Handler | None           # handler(cur, *, max_rows, **args); None for submit_answer
    source: str = ""
    # Optional step run BEFORE the database transaction opens (e.g. embedding the
    # query for search_videos), so no network call happens inside a transaction.
    prepare: Callable[[dict], dict] | None = None

    def spec(self) -> dict:
        """OpenAI `tools` entry."""
        return {"type": "function",
                "function": {"name": self.name, "description": self.description,
                             "parameters": self.parameters}}


class ToolRegistry:
    def __init__(self, tools: list[Tool]):
        self._tools = {t.name: t for t in tools}
        if SUBMIT_ANSWER not in self._tools:
            raise ValueError("A registry must include the submit_answer tool.")

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def specs(self) -> list[dict]:
        return [t.spec() for t in self._tools.values()]

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def parse_args(self, name: str, raw: str | dict | None) -> tuple[dict | None, ToolResult | None]:
        """(args, None) when valid, else (None, error ToolResult for the LLM)."""
        tool = self._tools.get(name)
        if tool is None:
            return None, error_result(name, "unknown_tool", f"Available tools: {list(self._tools)}")
        if isinstance(raw, dict):
            args = raw
        else:
            try:
                args = json.loads(raw or "{}")
            except json.JSONDecodeError as e:
                return None, error_result(name, "invalid_args", f"arguments are not valid JSON: {e}")
        problems = check_args(tool.parameters, args)
        if problems:
            return None, error_result(name, "invalid_args", problems)
        return args, None

    def run(self, name: str, raw_args, executor) -> ToolResult:
        """Validate then execute one data tool. Never raises."""
        args, err = self.parse_args(name, raw_args)
        if err:
            return err
        tool = self._tools[name]
        if tool.handler is None:
            return error_result(name, "not_a_data_tool", "submit_answer ends the turn.")
        try:
            if tool.prepare is not None:
                args = tool.prepare(dict(args))
            return executor.execute(tool, args)
        except Exception as e:  # DB errors, timeouts: report to the LLM, keep the turn alive
            kind = "timeout" if "timeout" in str(e).lower() or "cancel" in str(e).lower() else "tool_failed"
            logger.warning("Tool %s failed: %s", name, e)
            return error_result(name, kind, type(e).__name__)


class DbToolExecutor:
    """Runs a tool handler on a read-only, time-limited connection."""

    def __init__(self, db, *, max_rows: int = 20, statement_timeout_ms: int = 5000):
        self.db = db
        self.max_rows = max_rows
        self.statement_timeout_ms = statement_timeout_ms

    def execute(self, tool: Tool, args: dict) -> ToolResult:
        from youtube_intel import repository as repo
        with self.db.connect() as conn:
            with conn:
                with repo.dict_cursor(conn) as cur:
                    cur.execute("SET TRANSACTION READ ONLY;")
                    cur.execute("SET LOCAL statement_timeout = %s;",
                                (f"{int(self.statement_timeout_ms)}ms",))
                    return tool.handler(cur, max_rows=self.max_rows, **args)


@dataclass(frozen=True)
class TimedResult:
    """A tool result with its trace info (for AssistantAnswer.tool_trace)."""
    result: ToolResult
    latency_ms: int


def run_timed(registry: ToolRegistry, name: str, raw_args, executor) -> TimedResult:
    t0 = time.perf_counter()
    res = registry.run(name, raw_args, executor)
    return TimedResult(res, int((time.perf_counter() - t0) * 1000))


# =============================================================================
# Handlers (SQL). Each fetches max_rows + 1 rows so truncation is detectable.
# =============================================================================

def get_data_coverage(cur, *, max_rows: int, group: str | None = None) -> ToolResult:
    cur.execute(
        """
        SELECT g.group_code,
               count(*)                                           AS total_videos,
               count(*) FILTER (WHERE g.is_rankable)              AS rankable_videos,
               count(*) FILTER (WHERE g.validity_reason = 'insufficient_history')
                                                                  AS insufficient_history,
               max(g.end_observed_at)                             AS latest_observed_at,
               (max(g.end_observed_at) < now() - interval '30 hours')
                                                                  AS is_stale,
               (SELECT jsonb_object_agg(reason, n) FROM (
                    SELECT COALESCE(v2.validity_reason, 'no_observation') AS reason, count(*) AS n
                      FROM yti.v_video_growth v2
                     WHERE v2.group_code = g.group_code
                     GROUP BY 1) x)                               AS reason_counts,
               (SELECT max(r.started_at) FROM yti.collection_runs r
                 WHERE r.status IN ('succeeded', 'partial'))      AS last_collection_at
          FROM yti.v_video_growth g
         WHERE (%(group)s IS NULL OR g.group_code = %(group)s)
         GROUP BY g.group_code
         ORDER BY g.group_code
         LIMIT %(lim)s;
        """,
        {"group": group, "lim": max_rows + 1},
    )
    return finish("get_data_coverage", cur.fetchall(), max_rows=max_rows,
                  source="yti.v_video_growth, yti.collection_runs")


def list_channels(cur, *, max_rows: int, group: str | None = None) -> ToolResult:
    cur.execute(
        """
        SELECT c.channel_id, c.title AS channel_title, c.group_code,
               count(DISTINCT v.video_id)    AS videos,
               max(o.observed_at)            AS last_observed_at
          FROM yti.tracked_channels c
          LEFT JOIN yti.videos_current v USING (channel_id)
          LEFT JOIN yti.video_observations o ON o.video_id = v.video_id
         WHERE c.enabled AND (%(group)s IS NULL OR c.group_code = %(group)s)
         GROUP BY c.channel_id, c.title, c.group_code
         ORDER BY c.group_code, c.title
         LIMIT %(lim)s;
        """,
        {"group": group, "lim": max_rows + 1},
    )
    return finish("list_channels", cur.fetchall(), max_rows=max_rows,
                  source="yti.tracked_channels, yti.videos_current, yti.video_observations")


def top_growing_videos(cur, *, max_rows: int, group: str | None = None,
                       channel_id: str | None = None, format: str | None = None,
                       limit: int = 5) -> ToolResult:
    n = min(limit, max_rows)
    cur.execute(
        """
        SELECT row_number() OVER w                                  AS rank,
               video_id, video_title, channel_title, channel_id, group_code,
               format_label, published_at,
               views_per_hour, delta_views, elapsed_hours,
               end_view_count AS total_views, end_like_count AS likes,
               end_comment_count AS comments, end_observed_at,
               -- share of the group's leader, in percent (computed here, not by the LLM)
               round((100 * views_per_hour / NULLIF(max(views_per_hour) OVER (), 0))::numeric, 1)
                                                                    AS pct_of_leader
          FROM yti.v_video_growth
         WHERE is_rankable
           AND (%(group)s   IS NULL OR group_code   = %(group)s)
           AND (%(channel)s IS NULL OR channel_id   = %(channel)s)
           AND (%(format)s  IS NULL OR format_label = %(format)s)
        WINDOW w AS (ORDER BY views_per_hour DESC, published_at DESC, video_id)
         ORDER BY views_per_hour DESC, published_at DESC, video_id
         LIMIT %(lim)s;
        """,
        {"group": group, "channel": channel_id, "format": format, "lim": n + 1},
    )
    return finish("top_growing_videos", cur.fetchall(), max_rows=n, source="yti.v_video_growth")


def get_video_stats(cur, *, max_rows: int, video_id: str) -> ToolResult:
    cur.execute(
        """
        SELECT video_id, video_title, channel_title, channel_id, group_code, published_at,
               duration_seconds, format_label, live_state, made_for_kids,
               start_observed_at, end_observed_at, elapsed_hours,
               start_view_count, end_view_count AS total_views, delta_views, views_per_hour,
               end_like_count AS likes, end_comment_count AS comments,
               validity_reason, is_rankable, is_stale
          FROM yti.v_video_growth
         WHERE video_id = %(vid)s
         LIMIT 2;
        """,
        {"vid": video_id},
    )
    return finish("get_video_stats", cur.fetchall(), max_rows=1, source="yti.v_video_growth")


def get_video_history(cur, *, max_rows: int, video_id: str, limit: int = 10) -> ToolResult:
    n = min(limit, max_rows)
    cur.execute(
        """
        SELECT o.video_id, v.title AS video_title, o.observation_id, o.observed_at,
               o.view_count, o.like_count, o.comment_count
          FROM yti.video_observations o
          JOIN yti.videos_current v USING (video_id)
         WHERE o.video_id = %(vid)s
         ORDER BY o.observed_at DESC
         LIMIT %(lim)s;
        """,
        {"vid": video_id, "lim": n + 1},
    )
    return finish("get_video_history", cur.fetchall(), max_rows=n, source="yti.video_observations")


def compare_channels(cur, *, max_rows: int, metric: str,
                     group: str | None = None) -> ToolResult:
    cur.execute(
        """
        WITH per AS (
            SELECT channel_id, channel_title, group_code,
                   count(*)                                         AS videos,
                   count(*) FILTER (WHERE is_rankable)              AS rankable_videos,
                   round((percentile_cont(0.5) WITHIN GROUP (ORDER BY views_per_hour)
                          FILTER (WHERE is_rankable))::numeric, 2)  AS median_views_per_hour,
                   sum(delta_views) FILTER (WHERE is_rankable)      AS total_delta_views
              FROM yti.v_video_growth
             WHERE (%(group)s IS NULL OR group_code = %(group)s)
             GROUP BY channel_id, channel_title, group_code
        ), scored AS (
            SELECT per.*,
                   CASE %(metric)s
                        WHEN 'median_views_per_hour' THEN median_views_per_hour::numeric
                        WHEN 'total_delta_views'     THEN total_delta_views::numeric
                        WHEN 'rankable_videos'       THEN rankable_videos::numeric
                   END AS metric_value
              FROM per
        )
        SELECT row_number() OVER (ORDER BY metric_value DESC NULLS LAST, channel_title) AS rank,
               channel_id, channel_title, group_code, videos, rankable_videos,
               median_views_per_hour, total_delta_views,
               %(metric)s AS metric, metric_value,
               round(100 * metric_value / NULLIF(max(metric_value) OVER (), 0), 1) AS pct_of_leader,
               max(metric_value) OVER () - metric_value                           AS gap_to_leader
          FROM scored
         ORDER BY metric_value DESC NULLS LAST, channel_title
         LIMIT %(lim)s;
        """,
        {"group": group, "metric": metric, "lim": max_rows + 1},
    )
    return finish("compare_channels", cur.fetchall(), max_rows=max_rows, source="yti.v_video_growth")


def get_latest_brief(cur, *, max_rows: int, group: str) -> ToolResult:
    cur.execute(
        """
        SELECT report_id, group_code, as_of, status, model_name, prompt_version,
               validated_payload, evidence_bundle
          FROM yti.reports
         WHERE group_code = %(group)s
         ORDER BY as_of DESC
         LIMIT 1;
        """,
        {"group": group},
    )
    row = cur.fetchone()
    if row is None:
        return finish("get_latest_brief", [], max_rows=max_rows, source="yti.reports")
    payload = row["validated_payload"] or {}
    by_id = {c["video_id"]: c for c in (row["evidence_bundle"] or {}).get("candidates", [])}
    items = []
    for it in payload.get("items", [])[:max_rows]:
        cand = by_id.get(it.get("video_id"), {})
        m = cand.get("metrics", {})
        items.append({
            "video_id": it.get("video_id"), "video_title": cand.get("title"),
            "channel_title": cand.get("channel_title"),
            "views_per_hour": m.get("views_per_hour"), "delta_views": m.get("delta_views"),
            "elapsed_hours": m.get("elapsed_hours"),
            "topic_inference": it.get("topic_inference"), "why_selected": it.get("why_selected"),
            "original_angle": it.get("original_angle"),
        })
    extra = {"as_of": row["as_of"], "status": row["status"],
             "written_by": row["model_name"] or "template",
             "prompt_version": row["prompt_version"],
             "scope_statement": payload.get("scope_statement")}
    return finish("get_latest_brief", items, max_rows=max_rows, source="yti.reports", extra=extra)


# =============================================================================
# Registry
# =============================================================================

def _group_prop(groups) -> dict:
    return {"type": "string", "enum": list(groups),
            "description": "Content group. Omit for all groups."}


def build_tools(groups) -> list[Tool]:
    """All data tools plus submit_answer. `groups` = valid group codes from config."""
    G = _group_prop(groups)
    vid = {"type": "string", "pattern": VIDEO_ID_PATTERN,
           "description": "YouTube video id, exactly as returned by another tool."}
    obj = lambda props, required=(): {  # noqa: E731
        "type": "object", "properties": props, "required": list(required),
        "additionalProperties": False}
    return [
        Tool("get_data_coverage",
             "How much data exists per group: total videos, how many can be ranked, how "
             "many lack history, why the rest cannot be ranked, the latest observation, "
             "and whether data is stale. Call this when the user asks about coverage or "
             "freshness, or when another tool returns nothing.",
             obj({"group": G}), get_data_coverage, "yti.v_video_growth"),
        Tool("list_channels",
             "The tracked channels: id, title, group, number of videos, last observation.",
             obj({"group": G}), list_channels, "yti.tracked_channels"),
        Tool("top_growing_videos",
             "Videos ranked by views per hour between two observations about 24 hours "
             "apart. Only rankable videos. Includes each video's share of the leader "
             "(pct_of_leader, percent).",
             obj({"group": G,
                  "channel_id": {"type": "string", "pattern": CHANNEL_ID_PATTERN},
                  "format": {"type": "string", "enum": FORMATS,
                             "description": "normal = longer than 180 s; unknown may be Shorts"},
                  "limit": {"type": "integer", "minimum": 1, "maximum": 10}}),
             top_growing_videos, "yti.v_video_growth"),
        Tool("get_video_stats",
             "Latest numbers and growth of one video, with validity_reason explaining "
             "why growth can or cannot be computed.",
             obj({"video_id": vid}, ["video_id"]), get_video_stats, "yti.v_video_growth"),
        Tool("get_video_history",
             "Past observations (view/like/comment counts over time) of one video, newest first.",
             obj({"video_id": vid, "limit": {"type": "integer", "minimum": 1, "maximum": 30}},
                 ["video_id"]),
             get_video_history, "yti.video_observations"),
        Tool("compare_channels",
             "Compare channels on one metric: median views per hour of rankable videos, "
             "total views gained, or number of rankable videos. Includes each channel's "
             "share of the leader (percent) and its gap to the leader.",
             obj({"group": G, "metric": {"type": "string", "enum": COMPARE_METRICS}},
                 ["metric"]),
             compare_channels, "yti.v_video_growth"),
        Tool("get_latest_brief",
             "The latest daily brief of a group: which videos it picked, its notes, and "
             "whether it was validated (LLM) or a template.",
             obj({"group": {**G, "description": "Content group."}}, ["group"]),
             get_latest_brief, "yti.reports"),
        submit_answer_tool(),
    ]


def submit_answer_tool() -> Tool:
    return Tool(
        SUBMIT_ANSWER,
        "Finish the turn with the final answer. Every number in `answer` must come "
        "from a tool result of this turn. Do not include URLs. Put each missing piece "
        "of data in data_gaps. Use status=declined for out-of-scope questions.",
        {"type": "object",
         "properties": {
             "answer": {"type": "string", "minLength": 1, "maxLength": 4000},
             "cited_video_ids": {"type": "array", "maxItems": 10,
                                 "items": {"type": "string", "pattern": VIDEO_ID_PATTERN}},
             "data_gaps": {"type": "array", "maxItems": 10,
                           "items": {"type": "string", "maxLength": 300}},
             "status": {"type": "string", "enum": ["answered", "declined"]},
         },
         "required": ["answer", "cited_video_ids", "data_gaps", "status"],
         "additionalProperties": False},
        None,
    )


def build_registry(groups, extra_tools: list[Tool] | None = None) -> ToolRegistry:
    tools = build_tools(groups)
    if extra_tools:                          # e.g. search_videos (RAG)
        tools = tools[:-1] + list(extra_tools) + tools[-1:]
    return ToolRegistry(tools)
