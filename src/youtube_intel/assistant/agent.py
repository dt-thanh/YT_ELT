"""
The assistant's agent loop.

    question -> LLM (tools offered, tool_choice=auto)
             -> run the tools it calls (read-only), feed results back wrapped in
                <tool_result>
             -> ... at most max_steps rounds; the last round must call submit_answer
             -> grounding check of the submitted answer
                  ok        -> answered / declined
                  not ok    -> one repair round (errors sent back, submit forced)
                  still bad -> fallback: a table built from the tool results

Every LLM round is logged by LLMClient (feature="assistant", one session_id per
chat, one turn_id per question) and then marked: passed / repaired / failed,
and fallback_used on the last call when the fallback answer is shown.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from youtube_intel.assistant import grounding
from youtube_intel.assistant.prompts import system_prompt
from youtube_intel.assistant.tools import (
    SUBMIT_ANSWER, DbToolExecutor, ToolRegistry, ToolResult, build_registry,
    run_timed, to_jsonable,
)
from youtube_intel.llm import LLMError, estimate_cost

logger = logging.getLogger(__name__)

VIDEO_URL = "https://www.youtube.com/watch?v={}"


@dataclass(frozen=True)
class Citation:
    video_id: str
    title: str | None
    channel_title: str | None
    url: str                         # built by code, never by the LLM


@dataclass(frozen=True)
class TraceEntry:
    step: int
    tool: str
    args: dict | str
    row_count: int
    latency_ms: int
    error: str | None = None


@dataclass(frozen=True)
class AssistantAnswer:
    question: str
    text: str
    status: str                      # answered | declined | fallback | failed
    citations: tuple[Citation, ...] = ()
    data_gaps: tuple[str, ...] = ()
    tool_trace: tuple[TraceEntry, ...] = ()
    validation_errors: tuple[str, ...] = ()
    repaired: bool = False
    steps: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    est_cost_usd: Decimal | None = None
    latency_ms: int = 0
    data_as_of: str | None = None
    session_id: str = ""
    turn_id: str = ""
    prompt_version: str = ""
    tool_video_ids: tuple[str, ...] = ()       # every video id any tool returned this turn
    transcript: tuple[dict, ...] = field(default=(), repr=False)   # for eval / debugging


# =============================================================================
# Message helpers
# =============================================================================

def wrap_tool_result(result: ToolResult) -> str:
    """Tool output as DATA. `</` is escaped so a title cannot close the tag."""
    body = json.dumps(result.to_llm(), ensure_ascii=False).replace("</", "<\\/")
    return f"<tool_result>{body}</tool_result>"


def _assistant_msg(resp) -> dict:
    return {"role": "assistant", "content": resp.content,
            "tool_calls": [{"id": tc["id"], "type": "function",
                            "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                           for tc in resp.tool_calls]}


def history_messages(history, max_turns: int) -> list[dict]:
    """history: [(question, answer_text), ...] from the same chat session."""
    msgs: list[dict] = []
    for q, a in list(history)[-max_turns:] if max_turns else []:
        msgs += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
    return msgs


# =============================================================================
# Fallback answer: the facts, straight from the tools
# =============================================================================

_PREFERRED_COLUMNS = ("rank", "video_title", "channel_title", "group_code", "views_per_hour",
                      "delta_views", "elapsed_hours", "total_views", "pct_of_leader",
                      "metric_value", "videos", "rankable_videos", "validity_reason",
                      "observed_at", "view_count", "latest_observed_at", "is_stale")


def _cell(v) -> str:
    v = to_jsonable(v)
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:,.2f}".rstrip("0").rstrip(".")
    if isinstance(v, int) and not isinstance(v, bool):
        return f"{v:,}"
    return str(v).replace("|", "/")[:60]


def fallback_text(results: list[ToolResult], reason: str) -> str:
    lines = [f"_{reason} Dưới đây là số liệu lấy thẳng từ các công cụ, chưa qua diễn giải._", ""]
    shown = False
    for r in results:
        if not r.ok or not r.rows:
            continue
        cols = [c for c in _PREFERRED_COLUMNS if any(c in row for row in r.rows)][:7]
        if not cols:
            cols = [k for k in r.rows[0] if not isinstance(r.rows[0][k], (dict, list))][:7]
        lines += [f"**{r.tool}** ({r.row_count} dòng, nguồn `{r.source}`)", "",
                  "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
        lines += ["| " + " | ".join(_cell(row.get(c)) for c in cols) + " |" for row in r.rows[:10]]
        lines.append("")
        shown = True
    if not shown:
        lines.append("Không có công cụ nào trả về dữ liệu cho câu hỏi này.")
    return "\n".join(lines)


def _citations(video_ids, results: list[ToolResult]) -> tuple[Citation, ...]:
    info: dict[str, dict] = {}
    for r in results:
        for row in r.rows:
            vid = row.get("video_id")
            if vid and vid not in info:
                info[vid] = row
    out = []
    for vid in dict.fromkeys(video_ids):
        row = info.get(vid, {})
        out.append(Citation(vid, row.get("video_title") or row.get("title"),
                            row.get("channel_title"), VIDEO_URL.format(vid)))
    return tuple(out)


# =============================================================================
# The loop
# =============================================================================

@dataclass
class _Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost: Decimal | None = None

    def add(self, resp, pricing) -> None:
        if resp.input_tokens is not None:
            self.input_tokens = (self.input_tokens or 0) + resp.input_tokens
        if resp.output_tokens is not None:
            self.output_tokens = (self.output_tokens or 0) + resp.output_tokens
        c = estimate_cost(resp.model, resp.input_tokens, resp.output_tokens, pricing)
        if c is not None:
            self.cost = (self.cost or Decimal(0)) + c


def run_turn(question: str, *, client, registry: ToolRegistry, executor, groups,
             prompt_version: str = "a1", history=(), session_id: str | None = None,
             max_steps: int = 5, history_turns: int = 6,
             feature: str = "assistant") -> AssistantAnswer:
    """Answer one question. Never raises; failures become status='failed'/'fallback'."""
    t0 = time.perf_counter()
    session_id = session_id or str(uuid.uuid4())
    turn_id = str(uuid.uuid4())
    messages = ([{"role": "system", "content": system_prompt(prompt_version, groups=groups)}]
                + history_messages(history, history_turns)
                + [{"role": "user", "content": question}])
    results: list[ToolResult] = []
    trace: list[TraceEntry] = []
    usage = _Usage()
    submitted = None                  # (args, llm_call_id, tool_call_id)
    last_call_id = None
    steps = 0

    def finish(text, status, *, cited=(), gaps=(), errors=(), repaired=False):
        as_of = max((r.as_of for r in results if r.as_of), default=None)
        return AssistantAnswer(
            question=question, text=text, status=status,
            citations=_citations(cited, results), data_gaps=tuple(gaps),
            tool_trace=tuple(trace), validation_errors=tuple(errors), repaired=repaired,
            steps=steps, input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
            est_cost_usd=usage.cost, latency_ms=int((time.perf_counter() - t0) * 1000),
            data_as_of=as_of, session_id=session_id, turn_id=turn_id,
            prompt_version=prompt_version,
            tool_video_ids=tuple(dict.fromkeys(v for r in results for v in r.video_ids)),
            transcript=tuple(to_jsonable(messages)))

    def call(step: int, *, force_submit: bool):
        nonlocal last_call_id
        choice = ({"type": "function", "function": {"name": SUBMIT_ANSWER}}
                  if force_submit else "auto")
        resp = client.chat(messages, feature=feature, prompt_version=prompt_version,
                           tools=registry.specs(), tool_choice=choice, session_id=session_id,
                           turn_id=turn_id, attempt=step, temperature=0)
        last_call_id = resp.call_id
        usage.add(resp, client.pricing)
        return resp

    # ---- tool rounds -----------------------------------------------------------
    try:
        for step in range(1, max_steps + 1):
            steps = step
            resp = call(step, force_submit=(step == max_steps))
            if not resp.tool_calls:
                # Plain text instead of a tool call: remind, keep the turn going.
                messages.append({"role": "assistant", "content": resp.content or ""})
                messages.append({"role": "user",
                                 "content": "Give your final answer by calling submit_answer."})
                continue
            messages.append(_assistant_msg(resp))
            for tc in resp.tool_calls:
                if tc["name"] == SUBMIT_ANSWER:
                    args, err = registry.parse_args(SUBMIT_ANSWER, tc["arguments"])
                    if err:
                        messages.append({"role": "tool", "tool_call_id": tc["id"],
                                         "content": wrap_tool_result(err)})
                    else:
                        submitted = (args, resp.call_id, tc["id"])
                    continue
                timed = run_timed(registry, tc["name"], tc["arguments"], executor)
                results.append(timed.result)
                trace.append(TraceEntry(step, tc["name"], _safe_args(tc["arguments"]),
                                        timed.result.row_count, timed.latency_ms,
                                        (timed.result.error or {}).get("error")))
                messages.append({"role": "tool", "tool_call_id": tc["id"],
                                 "content": wrap_tool_result(timed.result)})
            if submitted:
                break
    except LLMError as e:
        logger.error("Assistant LLM call failed: %s", e)
        if last_call_id:
            client.mark(last_call_id, fallback_used=bool(results))
        if results:
            return finish(fallback_text(results, "Mô hình ngôn ngữ đang lỗi."), "fallback")
        return finish("Xin lỗi, hiện chưa gọi được mô hình ngôn ngữ. Vui lòng thử lại sau.", "failed")

    if submitted is None:
        if last_call_id:
            client.mark(last_call_id, validation_status="failed", fallback_used=True)
        return finish(fallback_text(results, "Chưa đủ bước để trả lời."), "fallback",
                      errors=("no submit_answer within max_steps",))

    # ---- grounding, with one repair ---------------------------------------------
    args, call_id, tool_call_id = submitted
    report = grounding.ground(args["answer"], args["cited_video_ids"], args["data_gaps"],
                              results, status=args["status"])
    if report.ok:
        client.mark(call_id, validation_status="passed")
        return finish(args["answer"], args["status"], cited=args["cited_video_ids"],
                      gaps=args["data_gaps"])

    client.mark(call_id, validation_status="failed")
    first_errors = report.errors
    messages.append({"role": "tool", "tool_call_id": tool_call_id, "content":
                     "<grounding_errors>" + json.dumps(list(first_errors), ensure_ascii=False)
                     + "</grounding_errors> Fix every error using only the tool results "
                       "above, then call submit_answer again."})
    try:
        steps += 1
        resp = call(steps, force_submit=True)
        messages.append(_assistant_msg(resp) if resp.tool_calls
                        else {"role": "assistant", "content": resp.content or ""})
        fixed = next((tc for tc in resp.tool_calls if tc["name"] == SUBMIT_ANSWER), None)
        args2, err = (registry.parse_args(SUBMIT_ANSWER, fixed["arguments"]) if fixed
                      else (None, None))
        if args2 is not None:
            report2 = grounding.ground(args2["answer"], args2["cited_video_ids"],
                                       args2["data_gaps"], results, status=args2["status"])
            if report2.ok:
                client.mark(resp.call_id, validation_status="repaired")
                return finish(args2["answer"], args2["status"], cited=args2["cited_video_ids"],
                              gaps=args2["data_gaps"], errors=first_errors, repaired=True)
            first_errors = first_errors + tuple(f"after repair: {x}" for x in report2.errors)
        client.mark(resp.call_id, validation_status="failed", fallback_used=True)
    except LLMError as e:
        logger.error("Assistant repair call failed: %s", e)
        if last_call_id:
            client.mark(last_call_id, fallback_used=True)

    return finish(fallback_text(results, "Câu trả lời của mô hình không qua được bước kiểm chứng."),
                  "fallback", errors=first_errors)


def _safe_args(raw) -> dict | str:
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return str(raw)[:200]


# =============================================================================
# Wiring: one core for CLI and Streamlit
# =============================================================================

class Assistant:
    """Everything needed to answer questions, built once from config."""

    def __init__(self, *, client, registry: ToolRegistry, executor, groups, settings):
        self.client = client
        self.registry = registry
        self.executor = executor
        self.groups = tuple(groups)
        self.settings = settings          # AssistantSettings

    def ask(self, question: str, *, history=(), session_id: str | None = None,
            prompt_version: str | None = None, feature: str = "assistant") -> AssistantAnswer:
        a = self.settings
        return run_turn(question, client=self.client, registry=self.registry,
                        executor=self.executor, groups=self.groups,
                        prompt_version=prompt_version or a.prompt_version, history=history,
                        session_id=session_id, max_steps=a.max_steps,
                        history_turns=a.history_turns, feature=feature)


def build_assistant(cfg, *, extra_tools=None, data_db_name: str | None = None) -> Assistant:
    """Production wiring: LLM calls logged as the ELT user, tools run as the
    read-only login yti_assistant. Needs secret groups db, llm and assistant.

    data_db_name: database the TOOLS read (default: the main one). The eval
    harness points it at yti_eval; LLM calls are still logged in the main DB.
    """
    from youtube_intel import repository as repo
    from youtube_intel.llm import LLMClient, PostgresSink

    s, st = cfg.secrets, cfg.settings
    log_db = repo.Database(host=s.db_host, port=s.db_port, name=s.db_name,
                           user=s.db_user, password=s.db_password)
    ro_db = repo.Database(host=s.db_host, port=s.db_port, name=data_db_name or s.db_name,
                          user=st.assistant.db_user, password=s.assistant_db_password)
    groups = sorted({c.group for c in cfg.channels})
    client = LLMClient(api_key=s.openai_api_key, model=st.llm_model, pricing=st.llm_pricing,
                       sink=PostgresSink(log_db), embedding_model=st.rag.embedding_model)
    if extra_tools is None:
        extra_tools = _default_extra_tools(cfg, client)
    # search_videos must read the same database as the other tools.
    return Assistant(client=client, registry=build_registry(groups, extra_tools),
                     executor=DbToolExecutor(ro_db, max_rows=st.assistant.max_rows,
                                             statement_timeout_ms=st.assistant.statement_timeout_ms),
                     groups=groups, settings=st.assistant)


def _default_extra_tools(cfg, client) -> list:
    """Optional tools (RAG search) - added when that module is available."""
    try:
        from youtube_intel.rag.retriever import search_videos_tool
    except ImportError:
        return []
    return [search_videos_tool(cfg, client)]
