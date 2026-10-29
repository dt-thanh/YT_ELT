"""
The single entry point for LLM and embedding calls (OpenAI).

Every call - successful or not - leaves one row in yti.llm_calls: model, tokens,
estimated cost, latency, status, and (set later by the caller) validation status
and whether a fallback was used. No other module may call OpenAI directly.

Design:
  * Logging goes through a CallSink: PostgresSink in production, MemorySink in
    tests, NullSink when logging is off.
  * The row is written in a `finally` block, so failed calls are logged too.
  * Logging is best-effort: a sink error is logged and swallowed - it must never
    break or replace the LLM answer, nor hide the original LLM error.
  * The prompt text is not stored; only a truncated SHA-256 (input_hash).
  * The openai client is injectable, so tests run against a scripted fake and
    never touch the network.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Protocol

from youtube_intel.config import ModelPrice

logger = logging.getLogger(__name__)

FEATURES = ("report", "assistant", "embedding", "eval")
VALIDATION_STATUSES = ("passed", "repaired", "failed", "not_applicable")
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"

# The API answers with dated snapshots ("gpt-4o-mini-2024-07-18"); prices are
# published per base name. Strip a trailing -YYYY-MM-DD to find the price.
_SNAPSHOT_SUFFIX = re.compile(r"-\d{4}-\d{2}-\d{2}$")


class LLMError(Exception):
    """An LLM/embedding call failed. `call_id` points at its yti.llm_calls row."""

    def __init__(self, message: str, *, call_id: str, status: str, error_type: str):
        super().__init__(message)
        self.call_id = call_id
        self.status = status
        self.error_type = error_type


# =============================================================================
# Cost
# =============================================================================

def find_price(model: str, pricing) -> ModelPrice | None:
    """Exact model name first, then the base name of a dated snapshot."""
    by_name = {p.model: p for p in pricing}
    if model in by_name:
        return by_name[model]
    return by_name.get(_SNAPSHOT_SUFFIX.sub("", model))


def estimate_cost(model: str, input_tokens: int | None, output_tokens: int | None,
                  pricing) -> Decimal | None:
    """Estimated USD cost, or None when it cannot be known.

    None (not 0) when: the model has no price, input tokens are unknown, or the
    model bills output tokens but the output count is unknown.
    """
    price = find_price(model, pricing)
    if price is None or input_tokens is None:
        return None
    cost = Decimal(str(price.input_per_1m)) * input_tokens
    if price.output_per_1m is not None:
        if output_tokens is None:
            return None
        cost += Decimal(str(price.output_per_1m)) * output_tokens
    return (cost / Decimal(1_000_000)).quantize(Decimal("0.000001"))


def request_hash(payload: dict) -> str:
    """Truncated SHA-256 of a request - stable for identical requests."""
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# =============================================================================
# Call records and sinks
# =============================================================================

@dataclass(frozen=True)
class CallRecord:
    """One row of yti.llm_calls."""
    call_id: str
    called_at: datetime
    feature: str
    model: str
    latency_ms: int
    status: str                              # ok | error | timeout | refused
    session_id: str | None = None
    turn_id: str | None = None
    prompt_version: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    est_cost_usd: Decimal | None = None
    error_type: str | None = None
    validation_status: str = "not_applicable"
    fallback_used: bool = False
    tool_call_count: int = 0
    attempt: int = 1
    input_hash: str | None = None


class CallSink(Protocol):
    def record(self, rec: CallRecord) -> None: ...
    def mark(self, call_id: str, *, validation_status: str | None = None,
             fallback_used: bool | None = None) -> None: ...


class NullSink:
    """Logging switched off."""

    def record(self, rec: CallRecord) -> None:
        pass

    def mark(self, call_id, *, validation_status=None, fallback_used=None) -> None:
        pass


class MemorySink:
    """Keeps records in a list. For tests."""

    def __init__(self) -> None:
        self.records: list[CallRecord] = []

    def record(self, rec: CallRecord) -> None:
        self.records.append(rec)

    def mark(self, call_id, *, validation_status=None, fallback_used=None) -> None:
        for i, rec in enumerate(self.records):
            if rec.call_id == call_id:
                changes = {}
                if validation_status is not None:
                    changes["validation_status"] = validation_status
                if fallback_used is not None:
                    changes["fallback_used"] = fallback_used
                self.records[i] = dataclasses.replace(rec, **changes)


class PostgresSink:
    """Writes to yti.llm_calls, each write in its own short transaction.

    Its own transaction on purpose: a call made while the caller's transaction
    later rolls back must still be logged (it was made, and it may have cost money).
    """

    def __init__(self, db) -> None:
        self._db = db

    def record(self, rec: CallRecord) -> None:
        from youtube_intel import repository as repo
        with self._db.connect() as conn:
            with conn:
                with repo.dict_cursor(conn) as cur:
                    cur.execute(
                        """
                        INSERT INTO yti.llm_calls
                            (call_id, called_at, feature, session_id, turn_id, model,
                             prompt_version, input_tokens, output_tokens, est_cost_usd,
                             latency_ms, status, error_type, validation_status,
                             fallback_used, tool_call_count, attempt, input_hash)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (call_id) DO NOTHING;
                        """,
                        (rec.call_id, rec.called_at, rec.feature, rec.session_id,
                         rec.turn_id, rec.model, rec.prompt_version, rec.input_tokens,
                         rec.output_tokens, rec.est_cost_usd, rec.latency_ms, rec.status,
                         rec.error_type, rec.validation_status, rec.fallback_used,
                         rec.tool_call_count, rec.attempt, rec.input_hash),
                    )

    def mark(self, call_id, *, validation_status=None, fallback_used=None) -> None:
        from youtube_intel import repository as repo
        with self._db.connect() as conn:
            with conn:
                with repo.dict_cursor(conn) as cur:
                    cur.execute(
                        """
                        UPDATE yti.llm_calls
                           SET validation_status = COALESCE(%s, validation_status),
                               fallback_used     = COALESCE(%s, fallback_used)
                         WHERE call_id = %s;
                        """,
                        (validation_status, fallback_used, call_id),
                    )


# =============================================================================
# Client
# =============================================================================

@dataclass(frozen=True)
class LLMResponse:
    call_id: str
    content: str | None
    tool_calls: tuple[dict, ...]             # [{"id", "name", "arguments"}]
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int
    model: str
    refusal: str | None = None

    @property
    def total_tokens(self) -> int | None:
        if self.input_tokens is None and self.output_tokens is None:
            return None
        return (self.input_tokens or 0) + (self.output_tokens or 0)


class LLMClient:
    """Chat and embedding calls with logging. One instance can serve many calls."""

    def __init__(self, *, api_key: str | None = None, model: str,
                 pricing=(), sink: CallSink | None = None, timeout: int = 60,
                 embedding_model: str = DEFAULT_EMBEDDING_MODEL,
                 openai_client: Any = None) -> None:
        if openai_client is None:
            if not api_key:
                raise ValueError("LLMClient needs an api_key (or an injected openai_client).")
            from openai import OpenAI       # only imported when a real client is needed
            openai_client = OpenAI(api_key=api_key, timeout=timeout)
        self._openai = openai_client
        self.model = model
        self.embedding_model = embedding_model
        self.pricing = tuple(pricing)
        self.sink: CallSink = sink or NullSink()

    # ------------------------------------------------------------------ chat --

    def chat(self, messages: list[dict], *, feature: str, prompt_version: str | None = None,
             tools: list[dict] | None = None, response_format: dict | None = None,
             tool_choice: str | dict | None = None,
             session_id: str | None = None, turn_id: str | None = None,
             attempt: int = 1, max_tokens: int | None = None,
             temperature: float | None = None) -> LLMResponse:
        """One chat completion. Raises LLMError on failure (the failure is logged)."""
        _check_feature(feature)
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
        if response_format:
            kwargs["response_format"] = response_format
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if temperature is not None:
            kwargs["temperature"] = temperature

        rec = _Pending(feature=feature, model=self.model, prompt_version=prompt_version,
                       session_id=session_id, turn_id=turn_id, attempt=attempt,
                       input_hash=request_hash({"model": self.model, "messages": messages,
                                                "tools": tools}))
        t0 = time.perf_counter()
        try:
            resp = self._openai.chat.completions.create(**kwargs)
            rec.latency_ms = _elapsed_ms(t0)
            msg = resp.choices[0].message
            rec.model = getattr(resp, "model", None) or self.model
            rec.input_tokens, rec.output_tokens = _usage(resp)
            tool_calls = tuple(
                {"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments}
                for tc in (getattr(msg, "tool_calls", None) or ())
            )
            rec.tool_call_count = len(tool_calls)
            refusal = getattr(msg, "refusal", None)
            rec.status = "refused" if refusal else "ok"
            return LLMResponse(call_id=rec.call_id, content=msg.content, tool_calls=tool_calls,
                               input_tokens=rec.input_tokens, output_tokens=rec.output_tokens,
                               latency_ms=rec.latency_ms, model=rec.model, refusal=refusal)
        except Exception as e:
            rec.latency_ms = _elapsed_ms(t0)
            rec.status = "timeout" if _is_timeout(e) else "error"
            rec.error_type = type(e).__name__
            raise LLMError(f"LLM call failed ({rec.error_type}): {e}", call_id=rec.call_id,
                           status=rec.status, error_type=rec.error_type) from e
        finally:
            self._log(rec)

    # ------------------------------------------------------------- embedding --

    def embed(self, texts: list[str], *, feature: str = "embedding",
              session_id: str | None = None) -> list[list[float]]:
        """Embeds texts in one request. Raises LLMError on failure (logged)."""
        _check_feature(feature)
        rec = _Pending(feature=feature, model=self.embedding_model, session_id=session_id,
                       input_hash=request_hash({"model": self.embedding_model, "input": texts}))
        t0 = time.perf_counter()
        try:
            resp = self._openai.embeddings.create(model=self.embedding_model, input=list(texts))
            rec.latency_ms = _elapsed_ms(t0)
            rec.model = getattr(resp, "model", None) or self.embedding_model
            rec.input_tokens, _ = _usage(resp)
            rec.status = "ok"
            return [list(d.embedding) for d in sorted(resp.data, key=lambda d: d.index)]
        except Exception as e:
            rec.latency_ms = _elapsed_ms(t0)
            rec.status = "timeout" if _is_timeout(e) else "error"
            rec.error_type = type(e).__name__
            raise LLMError(f"Embedding call failed ({rec.error_type}): {e}", call_id=rec.call_id,
                           status=rec.status, error_type=rec.error_type) from e
        finally:
            self._log(rec)

    # ---------------------------------------------------------------- marks --

    def mark(self, call_id: str, *, validation_status: str | None = None,
             fallback_used: bool | None = None) -> None:
        """Record the caller's verdict on a call. Best-effort, like logging."""
        if validation_status is not None and validation_status not in VALIDATION_STATUSES:
            raise ValueError(f"Unknown validation_status {validation_status!r}")
        try:
            self.sink.mark(call_id, validation_status=validation_status,
                           fallback_used=fallback_used)
        except Exception as e:
            logger.warning("Could not update LLM call log %s: %s", call_id, e)

    def _log(self, pending: "_Pending") -> None:
        try:
            self.sink.record(pending.to_record(self.pricing))
        except Exception as e:
            logger.warning("Could not write LLM call log (%s): %s", pending.feature, e)


def client_from_settings(settings, api_key: str | None, *,
                         sink: CallSink | None = None) -> LLMClient | None:
    """LLMClient for the configured model, or None when LLM is off or the key is missing."""
    if not settings.llm_enabled or not api_key:
        return None
    return LLMClient(api_key=api_key, model=settings.llm_model,
                     pricing=settings.llm_pricing, sink=sink)


# =============================================================================
# Helpers
# =============================================================================

@dataclass
class _Pending:
    """Mutable draft of a CallRecord, filled in while the call runs."""
    feature: str
    model: str
    prompt_version: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    attempt: int = 1
    input_hash: str | None = None
    call_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    called_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    latency_ms: int = 0
    status: str = "error"
    error_type: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    tool_call_count: int = 0

    def to_record(self, pricing) -> CallRecord:
        return CallRecord(
            call_id=self.call_id, called_at=self.called_at, feature=self.feature,
            model=self.model, latency_ms=self.latency_ms, status=self.status,
            session_id=self.session_id, turn_id=self.turn_id,
            prompt_version=self.prompt_version, input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            est_cost_usd=estimate_cost(self.model, self.input_tokens, self.output_tokens, pricing),
            error_type=self.error_type, tool_call_count=self.tool_call_count,
            attempt=self.attempt, input_hash=self.input_hash,
        )


def _check_feature(feature: str) -> None:
    if feature not in FEATURES:
        raise ValueError(f"Unknown feature {feature!r}; expected one of {FEATURES}")


def _elapsed_ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


def _usage(resp) -> tuple[int | None, int | None]:
    """(input, output) tokens; None when the API did not report them."""
    usage = getattr(resp, "usage", None)
    if usage is None:
        return None, None
    return getattr(usage, "prompt_tokens", None), getattr(usage, "completion_tokens", None)


def _is_timeout(exc: Exception) -> bool:
    # Matches openai.APITimeoutError and the builtin TimeoutError without
    # importing openai at module level.
    return isinstance(exc, TimeoutError) or "Timeout" in type(exc).__name__
