"""LLMClient logging and cost estimation - no network (scripted fake OpenAI)."""
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

from unit.fakes import PRICING, APITimeoutError, FakeOpenAI, chat_reply
from youtube_intel.llm import (
    LLMClient, LLMError, MemorySink, estimate_cost, find_price, request_hash,
)

def client(*script, sink=None):
    return LLMClient(model="gpt-4o-mini", pricing=PRICING, sink=sink or MemorySink(),
                     openai_client=FakeOpenAI(*script))


# ---- cost ------------------------------------------------------------------
def test_cost_is_input_plus_output():
    # 1000 * 0.15/1M + 500 * 0.60/1M = 0.00015 + 0.0003
    assert estimate_cost("gpt-4o-mini", 1000, 500, PRICING) == Decimal("0.000450")


def test_dated_snapshot_uses_base_price():
    assert find_price("gpt-4o-mini-2024-07-18", PRICING).model == "gpt-4o-mini"


@pytest.mark.parametrize("model, inp, out", [
    ("gpt-unknown", 1000, 500),       # no price -> unknown, not 0
    ("gpt-4o-mini", None, 500),       # usage not reported
    ("gpt-4o-mini", 1000, None),      # chat model bills output, count unknown
])
def test_cost_is_null_when_unknown(model, inp, out):
    assert estimate_cost(model, inp, out, PRICING) is None


def test_embedding_cost_needs_input_only():
    assert estimate_cost("text-embedding-3-small", 1_000_000, None, PRICING) == Decimal("0.020000")


def test_request_hash_is_stable_and_sensitive():
    a = request_hash({"model": "m", "messages": [{"role": "user", "content": "x"}]})
    assert a == request_hash({"messages": [{"role": "user", "content": "x"}], "model": "m"})
    assert a != request_hash({"model": "m", "messages": [{"role": "user", "content": "y"}]})


# ---- logging ---------------------------------------------------------------
def test_successful_call_is_logged_with_tokens_cost_latency():
    sink = MemorySink()
    c = client(chat_reply('{"a": 1}'), sink=sink)
    resp = c.chat([{"role": "user", "content": "hi"}], feature="report",
                  prompt_version="v3", session_id="00000000-0000-0000-0000-000000000001")
    assert resp.content == '{"a": 1}' and resp.total_tokens == 120
    [rec] = sink.records
    assert rec.call_id == resp.call_id
    assert (rec.status, rec.feature, rec.prompt_version) == ("ok", "report", "v3")
    assert (rec.input_tokens, rec.output_tokens) == (100, 20)
    assert rec.model == "gpt-4o-mini-2024-07-18" and rec.est_cost_usd == Decimal("0.000027")
    assert rec.latency_ms >= 0 and rec.input_hash and rec.validation_status == "not_applicable"


def test_failed_call_is_logged_and_raises():
    sink = MemorySink()
    c = client(RuntimeError("boom"), sink=sink)
    with pytest.raises(LLMError) as e:
        c.chat([{"role": "user", "content": "hi"}], feature="assistant")
    [rec] = sink.records
    assert e.value.call_id == rec.call_id
    assert (rec.status, rec.error_type) == ("error", "RuntimeError")
    assert rec.input_tokens is None and rec.est_cost_usd is None    # NULL, never 0


def test_timeout_is_its_own_status():
    sink = MemorySink()
    with pytest.raises(LLMError):
        client(APITimeoutError("slow"), sink=sink).chat([], feature="report")
    assert sink.records[0].status == "timeout"


def test_missing_usage_stays_null():
    sink = MemorySink()
    client(chat_reply(usage=False), sink=sink).chat([], feature="report")
    rec = sink.records[0]
    assert rec.input_tokens is None and rec.output_tokens is None and rec.est_cost_usd is None


def test_refusal_and_tool_calls_are_recorded():
    tc = NS(id="t1", function=NS(name="list_channels", arguments="{}"))
    sink = MemorySink()
    c = client(chat_reply(None, tool_calls=[tc]), chat_reply(None, refusal="no"), sink=sink)
    resp = c.chat([], feature="assistant")
    assert resp.tool_calls == ({"id": "t1", "name": "list_channels", "arguments": "{}"},)
    c.chat([], feature="assistant")
    assert [r.tool_call_count for r in sink.records] == [1, 0]
    assert [r.status for r in sink.records] == ["ok", "refused"]


def test_mark_updates_validation_and_fallback():
    sink = MemorySink()
    c = client(chat_reply(), sink=sink)
    resp = c.chat([], feature="report")
    c.mark(resp.call_id, validation_status="failed", fallback_used=True)
    assert (sink.records[0].validation_status, sink.records[0].fallback_used) == ("failed", True)
    with pytest.raises(ValueError):
        c.mark(resp.call_id, validation_status="bogus")


def test_broken_sink_never_breaks_the_answer():
    class BrokenSink:
        def record(self, rec):
            raise RuntimeError("database down")

        def mark(self, *a, **k):
            raise RuntimeError("database down")

    c = client(chat_reply("ok"), sink=BrokenSink())
    assert c.chat([], feature="report").content == "ok"
    c.mark("any", validation_status="passed")          # swallowed, logged


def test_broken_sink_does_not_hide_the_llm_error():
    class BrokenSink:
        def record(self, rec):
            raise RuntimeError("database down")

    with pytest.raises(LLMError, match="boom"):
        client(RuntimeError("boom"), sink=BrokenSink()).chat([], feature="report")


def test_unknown_feature_is_rejected():
    with pytest.raises(ValueError):
        client(chat_reply()).chat([], feature="chitchat")


def test_embed_logs_one_call_and_keeps_order():
    sink = MemorySink()
    reply = NS(data=[NS(index=1, embedding=[0.2]), NS(index=0, embedding=[0.1])],
               model="text-embedding-3-small", usage=NS(prompt_tokens=50))
    vectors = client(reply, sink=sink).embed(["a", "b"])
    assert vectors == [[0.1], [0.2]]
    [rec] = sink.records
    assert (rec.feature, rec.input_tokens, rec.est_cost_usd) == ("embedding", 50, Decimal("0.000001"))


def test_real_client_requires_a_key():
    with pytest.raises(ValueError):
        LLMClient(model="gpt-4o-mini")
