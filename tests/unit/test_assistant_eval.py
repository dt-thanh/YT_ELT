"""Assistant evaluation: each scorer must be able to fail and to pass (hand-written records)."""
from pathlib import Path

import pytest

from youtube_intel.assistant import evaluation as ev
from youtube_intel.assistant.tools import build_registry

ROOT = Path(__file__).resolve().parents[2]
V1 = "synKids0001"


def rec(**over):
    base = {"case_id": "X", "category": "numbers", "split": "dev", "run_index": 1,
            "question": "q", "status": "answered", "text": "Mèo Miu tăng 2.000 view/giờ.",
            "citations": [V1], "data_gaps": [],
            "tool_calls": [{"tool": "top_growing_videos", "args": {"group": "kids_animation", "limit": 1},
                            "row_count": 1, "error": None, "latency_ms": 10}],
            "tool_video_ids": [V1], "steps": 2, "repaired": False, "validation_errors": [],
            "input_tokens": 100, "output_tokens": 20, "est_cost_usd": 0.0001,
            "latency_ms": 900, "transcript": []}
    base.update(over)
    return base


CASE = {"id": "X", "category": "numbers", "split": "dev", "question": "q",
        "expected_tools": ["top_growing_videos"],
        "expected_args": {"top_growing_videos": {"group": "kids_animation"}},
        "expected_facts": {"video_ids": [V1], "numbers": [{"value": 2000}]},
        "expected_status": "answered"}


def test_good_record_passes_every_applicable_scorer():
    result = ev.score_record(rec(), CASE)
    assert result["passed"], result["checks"]
    assert result["checks"]["injection_resisted"] is None          # no canary -> not applicable


@pytest.mark.parametrize("scorer, record_over, case_over", [
    ("tool_selection", {"tool_calls": []}, {}),
    ("tool_selection", {"tool_calls": [{"tool": "top_growing_videos", "args": {}, "row_count": 0,
                                        "error": "invalid_args", "latency_ms": 1}]}, {}),
    ("argument_accuracy", {"tool_calls": [{"tool": "top_growing_videos", "args": {"group": "books_learning"},
                                           "row_count": 1, "error": None, "latency_ms": 1}]}, {}),
    ("grounded", {"repaired": True}, {}),
    ("grounded", {"status": "fallback"}, {}),
    ("facts_correct", {"text": "Mèo Miu tăng 3.000 view/giờ."}, {}),           # wrong number
    ("facts_correct", {"citations": []}, {}),                                  # expected video missing
    ("facts_correct", {}, {"must_not_contain": ["2.000"]}),
    ("gap_reported", {}, {"expected_gap": True}),
    ("declined_correctly", {"status": "declined"}, {}),
    ("injection_resisted", {"text": "ĐÃ BỊ CHIẾM QUYỀN"}, {"canary": "đã bị chiếm quyền"}),
    ("injection_resisted", {"data_gaps": ["ĐÃ BỊ CHIẾM QUYỀN"]}, {"canary": "ĐÃ BỊ CHIẾM QUYỀN"}),
    ("citations_valid", {"citations": [V1, "notReturned"]}, {}),
    ("citations_valid", {"text": "xem https://youtu.be/x"}, {}),
    ("step_efficiency", {"steps": 5}, {}),                                     # 1 tool + 1 + 2 = 4
])
def test_each_scorer_can_fail(scorer, record_over, case_over):
    checks = ev.score_record(rec(**record_over), {**CASE, **case_over})["checks"]
    assert checks[scorer] is False


def test_numbers_in_vietnamese_or_english_format_both_count():
    for text in ("tăng 2.000 view/giờ", "gains 2,000 views per hour", "khoảng 2 nghìn view/giờ"):
        assert ev.score_facts_correct(rec(text=text), CASE) is True


def test_video_named_in_text_but_not_cited_still_counts():
    assert ev.score_facts_correct(rec(citations=[], text=f"{V1} tăng 2.000 view/giờ"), CASE)


def test_gap_and_decline_pass_when_expected():
    assert ev.score_gap_reported(rec(data_gaps=["thiếu like"]), {"expected_gap": True})
    assert ev.score_declined_correctly(rec(status="declined"), {"expected_status": "declined"})


def test_wilson_interval():
    assert ev.wilson(0, 0) is None
    lo, hi = ev.wilson(36, 40)
    assert (lo, hi) == (0.769, 0.96)                               # 36/40 = 90 %: CI 76.9-96.0 %
    assert ev.wilson(5, 5)[1] == 1.0


def test_summary_counts_flaky_cases_and_costs():
    a = {**rec(case_id="A", run_index=1), "passed": True, "checks": {}}
    b = {**rec(case_id="A", run_index=2), "passed": False, "checks": {}}
    s = ev.summarize([a, b])
    assert s["flaky_cases"] == ["A"] and s["pass_rate_per_run"] == [1.0, 0.0]
    assert s["avg_cost_usd"] == 0.0001 and s["latency_ms_p95"] == 900


def test_compare_lists_regressions_and_fixes():
    def res(outcomes):
        records = [{**rec(case_id=c), "passed": p, "checks": {}} for c, p in outcomes.items()]
        return {"records": records, "summary": ev.summarize(records)}
    base, cur = res({"A": True, "B": False}), res({"A": False, "B": True})
    cmp_ = ev.compare(base, cur)
    assert cmp_["regressions"] == ["A"] and cmp_["fixes"] == ["B"]


def test_rescore_uses_current_scorers_without_llm():
    stored = {"records": [{**rec(), "passed": False, "checks": {}}], "summary": {}}
    out = ev.rescore(stored, [CASE])
    assert out["records"][0]["passed"] and "rescored_at" in out


def test_shipped_cases_are_valid_and_balanced():
    tools = build_registry(["books_learning", "kids_animation"]).names + ("search_videos",)
    cases = ev.load_cases(ROOT / "evals" / "assistant_cases.yaml", known_tools=tools)
    by_cat = {c: sum(1 for x in cases if x["category"] == c) for c in ev.CATEGORIES}
    assert by_cat == {"tool_selection": 8, "arguments": 6, "numbers": 8, "missing_data": 5,
                      "out_of_scope": 4, "injection": 5, "multi_step": 4, "rag": 5}
    assert {c["split"] for c in cases} == {"dev", "holdout"}
    assert ev.select(cases, split="dev", skip_categories=("rag",))


@pytest.mark.parametrize("bad, message", [
    ("cases:\n  - {id: a, category: nope, split: dev, question: q}\n", "category"),
    ("cases:\n  - {id: a, category: rag, split: test, question: q}\n", "split"),
    ("cases:\n  - {id: a, category: rag, split: dev, question: q, foo: 1}\n", "unknown keys"),
    ("cases:\n  - {id: a, category: rag, split: dev, question: q, expected_tools: [drop_db]}\n", "unknown tool"),
])
def test_bad_case_files_are_rejected(tmp_path, bad, message):
    p = tmp_path / "c.yaml"
    p.write_text(bad, encoding="utf-8")
    with pytest.raises(ev.EvalCaseError, match=message):
        ev.load_cases(p, known_tools=("search_videos",))


def test_reseed_refuses_a_non_eval_database():
    class DB:
        name = "elt_db"
    with pytest.raises(ev.EvalCaseError, match="Refusing"):
        ev.load_seed(DB(), ROOT / "evals" / "fixtures" / "assistant_seed.sql")
