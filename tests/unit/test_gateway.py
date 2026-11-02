"""Gateway (spec §12): routing, cost ceiling checked BEFORE the call, job/agent logging."""
import uuid
from decimal import Decimal

import pytest
from pydantic import BaseModel

from studio.gateway import BudgetExceeded, Gateway, GatewayError, load_gateway_config
from youtube_intel.config import ConfigError, ModelPrice
from youtube_intel.llm import LLMClient, MemorySink

from unit.fakes import PRICING, FakeOpenAI, chat_reply

CFG = load_gateway_config()
JOB = str(uuid.uuid4())


class Answer(BaseModel):
    label: str
    score: int


def gateway(*replies, spent=Decimal("0"), pricing=PRICING):
    fake, sink = FakeOpenAI(*replies), MemorySink()
    client = LLMClient(model="gpt-4o-mini", pricing=pricing, sink=sink, openai_client=fake)
    gw = Gateway(CFG, client_for=lambda m: client, pricing=pricing, job_spent=lambda j: spent)
    return gw, fake, sink


MSG = [{"role": "user", "content": "phân loại bình luận này"}]


def test_structured_call_is_routed_parsed_and_logged_with_job_and_agent():
    gw, fake, sink = gateway(chat_reply('{"label": "question", "score": 3}'))
    obj, call_id = gw.structured("classify_comments", MSG, Answer, job_id=JOB, agent="research")
    assert obj == Answer(label="question", score=3)
    [rec] = sink.records
    assert (rec.call_id, rec.feature, rec.job_id, rec.agent, rec.validation_status) == \
        (call_id, "studio", JOB, "research", "passed")
    req = fake.requests[0]
    assert req["model"] == "gpt-4o-mini" and req["max_tokens"] == 1500
    assert req["response_format"]["json_schema"]["name"] == "Answer"


def test_a_call_that_could_pass_the_ceiling_is_refused_before_sending():
    gw, fake, sink = gateway(chat_reply("{}"), spent=Decimal("0.4999"))
    with pytest.raises(BudgetExceeded, match="ceiling"):
        gw.structured("classify_comments", MSG, Answer, job_id=JOB, agent="research")
    assert fake.requests == [] and sink.records == []


def test_worst_case_counts_full_output_and_generous_input():
    gw, _, _ = gateway()
    route = CFG.routes["classify_comments"]
    worst = gw.worst_case_usd(route, [{"role": "user", "content": "x" * 3000}])
    # >= 1000 input tokens at $0.15/M + 1500 output tokens at $0.60/M
    assert worst >= Decimal("0.00015") + Decimal("0.0009")


def test_a_model_without_a_price_is_refused():
    gw, fake, _ = gateway(chat_reply("{}"), pricing=(ModelPrice("other", 1, 1),))
    with pytest.raises(BudgetExceeded, match="no price"):
        gw.structured("classify_comments", MSG, Answer, job_id=JOB, agent="research")
    assert fake.requests == []


def test_output_that_breaks_the_schema_is_an_error_and_marked_failed():
    gw, _, sink = gateway(chat_reply('{"label": "question"}'))
    with pytest.raises(GatewayError) as e:
        gw.structured("classify_comments", MSG, Answer, job_id=JOB, agent="research")
    assert e.value.call_id == sink.records[0].call_id
    assert sink.records[0].validation_status == "failed"


@pytest.mark.parametrize("kwargs, message", [
    ({"job_id": "not-a-uuid", "agent": "a"}, "badly formed"),
    ({"job_id": JOB, "agent": "a", "budget": "huge"}, "unknown budget"),
])
def test_bad_calls_are_rejected(kwargs, message):
    gw, _, _ = gateway()
    with pytest.raises((GatewayError, ValueError), match=message):
        gw.structured("classify_comments", MSG, Answer, **kwargs)


def test_unknown_task_has_no_route():
    gw, _, _ = gateway()
    with pytest.raises(GatewayError, match="no route"):
        gw.structured("write_novel", MSG, Answer, job_id=JOB, agent="writer")


def test_a_route_to_an_unchosen_model_tier_fails_at_load(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text("gateway:\n  models: {cheap: gpt-4o-mini}\n  routes:\n"
                 "    write_script: {tier: good}\n"
                 "  budget_usd: {short: 3, long: 10, standalone: 0.5}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="good"):
        load_gateway_config(p)
