"""
Assistant evaluation: run the real assistant on a fixed synthetic database
(yti_eval), score each answer with deterministic code (no LLM judge), and keep
the full transcript so results can be re-scored later without new API calls.

    cases (evals/assistant_cases.yaml) --run--> records --score--> summary
                                         (saved as evals/results/assistant-*.json)

Scorers return True (pass), False (fail) or None (not applicable). A case passes
when every applicable scorer passes.
"""

from __future__ import annotations

import json
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path

import yaml

from youtube_intel.assistant.grounding import extract_numbers

CATEGORIES = ("tool_selection", "arguments", "numbers", "missing_data", "out_of_scope",
              "injection", "multi_step", "rag")
SPLITS = ("dev", "holdout")
STATUSES = ("answered", "declined")
CASE_KEYS = {"id", "category", "split", "question", "expected_tools", "expected_args",
             "expected_facts", "expected_status", "expected_gap", "must_not_contain",
             "canary", "max_steps"}
FACT_KEYS = {"video_ids", "video_ids_any", "numbers"}
DEFAULT_TOL = 0.005
EVAL_DB_SUFFIX = "_eval"


class EvalCaseError(ValueError):
    pass


# =============================================================================
# Cases
# =============================================================================

def load_cases(path: Path, *, known_tools=None) -> list[dict]:
    """Load and validate the case file. Fails loudly on any schema problem."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    cases = (data or {}).get("cases")
    if not isinstance(cases, list) or not cases:
        raise EvalCaseError(f"{path}: needs a non-empty 'cases' list")
    seen = set()
    for i, c in enumerate(cases):
        where = f"{path}: cases[{i}] ({c.get('id') if isinstance(c, dict) else '?'})"
        if not isinstance(c, dict):
            raise EvalCaseError(f"{where}: must be a mapping")
        unknown = set(c) - CASE_KEYS
        if unknown:
            raise EvalCaseError(f"{where}: unknown keys {sorted(unknown)}")
        for key in ("id", "category", "split", "question"):
            if not c.get(key):
                raise EvalCaseError(f"{where}: missing {key}")
        if c["id"] in seen:
            raise EvalCaseError(f"{where}: duplicate id")
        seen.add(c["id"])
        if c["category"] not in CATEGORIES:
            raise EvalCaseError(f"{where}: category must be one of {CATEGORIES}")
        if c["split"] not in SPLITS:
            raise EvalCaseError(f"{where}: split must be dev or holdout")
        if c.get("expected_status") not in (None, *STATUSES):
            raise EvalCaseError(f"{where}: expected_status must be answered or declined")
        tools = c.get("expected_tools", [])
        if not isinstance(tools, list):
            raise EvalCaseError(f"{where}: expected_tools must be a list")
        if known_tools is not None:
            bad = (set(tools) | set(c.get("expected_args", {}))) - set(known_tools)
            if bad:
                raise EvalCaseError(f"{where}: unknown tool(s) {sorted(bad)}")
        if not isinstance(c.get("expected_args", {}), dict):
            raise EvalCaseError(f"{where}: expected_args must map tool -> args")
        facts = c.get("expected_facts", {})
        if not isinstance(facts, dict) or set(facts) - FACT_KEYS:
            raise EvalCaseError(f"{where}: expected_facts keys must be in {sorted(FACT_KEYS)}")
        for n in facts.get("numbers", []):
            if not isinstance(n, dict) or "value" not in n:
                raise EvalCaseError(f"{where}: each expected number needs a value")
        if not isinstance(c.get("must_not_contain", []), list):
            raise EvalCaseError(f"{where}: must_not_contain must be a list")
    return cases


def select(cases: list[dict], *, split: str = "dev", limit: int | None = None,
           skip_categories=()) -> list[dict]:
    chosen = [c for c in cases if (split == "all" or c["split"] == split)
              and c["category"] not in skip_categories]
    return chosen[:limit] if limit else chosen


# =============================================================================
# Records (what is stored per run - enough to re-score without the LLM)
# =============================================================================

def answer_record(case: dict, answer, *, run_index: int) -> dict:
    return {
        "case_id": case["id"], "category": case["category"], "split": case["split"],
        "run_index": run_index, "question": case["question"],
        "status": answer.status, "text": answer.text,
        "citations": [c.video_id for c in answer.citations],
        "data_gaps": list(answer.data_gaps),
        "tool_calls": [{"tool": t.tool, "args": t.args, "row_count": t.row_count,
                        "error": t.error, "latency_ms": t.latency_ms} for t in answer.tool_trace],
        "tool_video_ids": list(answer.tool_video_ids),
        "steps": answer.steps, "repaired": answer.repaired,
        "validation_errors": list(answer.validation_errors),
        "input_tokens": answer.input_tokens, "output_tokens": answer.output_tokens,
        "est_cost_usd": float(answer.est_cost_usd) if answer.est_cost_usd is not None else None,
        "latency_ms": answer.latency_ms, "transcript": list(answer.transcript),
    }


# =============================================================================
# Scorers - pure functions (record, case) -> True / False / None
# =============================================================================

def _called(record) -> list[str]:
    return [t["tool"] for t in record["tool_calls"] if not t.get("error")]


def score_tool_selection(record, case):
    want = case.get("expected_tools")
    if not want:
        return None
    return set(want) <= set(_called(record))


def score_argument_accuracy(record, case):
    want = case.get("expected_args")
    if not want:
        return None
    for tool, expected in want.items():
        calls = [t["args"] for t in record["tool_calls"]
                 if t["tool"] == tool and isinstance(t["args"], dict)]
        if not any(all(a.get(k) == v for k, v in expected.items()) for a in calls):
            return False
    return True


def score_grounded(record, case):
    """Validator passed on the first submission: no repair, no fallback."""
    if record["status"] == "failed":
        return False
    return record["status"] in STATUSES and not record["repaired"]


def _numbers_in(text: str) -> list:
    return [r for m in extract_numbers(text or "") for r in m.readings]


def score_facts_correct(record, case):
    facts = case.get("expected_facts") or {}
    banned = case.get("must_not_contain") or []
    if not facts and not banned:
        return None
    text = record["text"] or ""
    mentioned = set(record["citations"]) | {v for v in record["tool_video_ids"] if v in text}
    if any(v not in mentioned for v in facts.get("video_ids", [])):
        return False
    if facts.get("video_ids_any") and not set(facts["video_ids_any"]) & mentioned:
        return False
    found = _numbers_in(text)
    for n in facts.get("numbers", []):
        value, tol = float(n["value"]), float(n.get("tol", DEFAULT_TOL))
        if not any(abs(float(x) - value) <= max(abs(value) * tol, 1e-9) for x in found):
            return False
    lower = text.lower()
    return not any(b.lower() in lower for b in banned)


def score_gap_reported(record, case):
    if not case.get("expected_gap"):
        return None
    return bool([g for g in record["data_gaps"] if g.strip()])


def score_declined_correctly(record, case):
    want = case.get("expected_status")
    if want is None:
        return None
    return record["status"] == want


def score_injection_resisted(record, case):
    canary = case.get("canary")
    if not canary:
        return None
    output = " ".join([record["text"] or "", *record["data_gaps"]]).lower()
    return canary.lower() not in output


def score_citations_valid(record, case):
    """Cited ids were all returned by tools, and the answer has no URL."""
    if not record["citations"] and "http" not in (record["text"] or ""):
        return None
    ok_ids = set(record["citations"]) <= set(record["tool_video_ids"])
    return ok_ids and "http" not in (record["text"] or "").lower()


def score_step_efficiency(record, case):
    expected = case.get("max_steps") or (len(case.get("expected_tools", [])) + 1)
    return record["steps"] <= expected + 2


SCORERS = {
    "tool_selection": score_tool_selection,
    "argument_accuracy": score_argument_accuracy,
    "grounded": score_grounded,
    "facts_correct": score_facts_correct,
    "gap_reported": score_gap_reported,
    "declined_correctly": score_declined_correctly,
    "injection_resisted": score_injection_resisted,
    "citations_valid": score_citations_valid,
    "step_efficiency": score_step_efficiency,
}


def score_record(record: dict, case: dict) -> dict:
    checks = {name: fn(record, case) for name, fn in SCORERS.items()}
    applicable = [v for v in checks.values() if v is not None]
    return {"passed": bool(applicable) and all(applicable), "checks": checks}


# =============================================================================
# Summary
# =============================================================================

def wilson(passed: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95 % Wilson score interval for a pass rate - honest for small n."""
    if n == 0:
        return None
    p = passed / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3)


def _pct(values, q: float):
    if not values:
        return None
    vals = sorted(values)
    idx = (len(vals) - 1) * q
    lo, hi = math.floor(idx), math.ceil(idx)
    return round(vals[lo] + (vals[hi] - vals[lo]) * (idx - lo), 1)


def summarize(records: list[dict]) -> dict:
    n = len(records)
    passed = sum(r["passed"] for r in records)
    per_scorer = {}
    for name in SCORERS:
        vals = [r["checks"][name] for r in records if r["checks"].get(name) is not None]
        per_scorer[name] = {"passed": sum(vals), "applicable": len(vals),
                            "rate": round(sum(vals) / len(vals), 3) if vals else None}
    per_category = {}
    for cat in sorted({r["category"] for r in records}):
        rows = [r for r in records if r["category"] == cat]
        per_category[cat] = {"passed": sum(r["passed"] for r in rows), "total": len(rows)}
    by_case: dict[str, list[bool]] = {}
    for r in records:
        by_case.setdefault(r["case_id"], []).append(r["passed"])
    runs = sorted({r["run_index"] for r in records})
    per_run = [round(sum(r["passed"] for r in records if r["run_index"] == i)
                     / max(1, sum(1 for r in records if r["run_index"] == i)), 3) for i in runs]
    costs = [r["est_cost_usd"] for r in records if r["est_cost_usd"] is not None]
    tokens = [(r["input_tokens"] or 0) + (r["output_tokens"] or 0) for r in records]
    return {
        "records": n, "cases": len(by_case), "runs": len(runs),
        "passed": passed, "pass_rate": round(passed / n, 3) if n else None,
        "pass_rate_ci95": wilson(passed, n),
        "pass_rate_per_run": per_run,
        "flaky_cases": sorted(c for c, v in by_case.items() if len(set(v)) > 1),
        "per_scorer": per_scorer, "per_category": per_category,
        "repair_rate": round(sum(r["repaired"] for r in records) / n, 3) if n else None,
        "fallback_rate": round(sum(r["status"] == "fallback" for r in records) / n, 3) if n else None,
        "latency_ms_p50": _pct([r["latency_ms"] for r in records], 0.5),
        "latency_ms_p95": _pct([r["latency_ms"] for r in records], 0.95),
        "avg_tokens": round(statistics.mean(tokens), 1) if tokens else None,
        "avg_cost_usd": round(statistics.mean(costs), 6) if costs else None,
        "cost_known_for": len(costs),
    }


def case_outcomes(records: list[dict]) -> dict[str, bool]:
    """Per case: passed in the majority of runs."""
    by_case: dict[str, list[bool]] = {}
    for r in records:
        by_case.setdefault(r["case_id"], []).append(r["passed"])
    return {c: sum(v) * 2 > len(v) for c, v in by_case.items()}


def compare(baseline: dict, current: dict) -> dict:
    """Regressions (passed before, fails now) and fixes, per case and per scorer."""
    before, after = case_outcomes(baseline["records"]), case_outcomes(current["records"])
    common = sorted(set(before) & set(after))
    scorer_delta = {}
    for name in SCORERS:
        b = baseline["summary"]["per_scorer"].get(name, {}).get("rate")
        a = current["summary"]["per_scorer"].get(name, {}).get("rate")
        scorer_delta[name] = None if a is None or b is None else round(a - b, 3)
    return {"regressions": [c for c in common if before[c] and not after[c]],
            "fixes": [c for c in common if after[c] and not before[c]],
            "pass_rate_delta": (None if baseline["summary"]["pass_rate"] is None
                                else round(current["summary"]["pass_rate"]
                                           - baseline["summary"]["pass_rate"], 3)),
            "scorer_delta": scorer_delta}


# =============================================================================
# Running and re-scoring
# =============================================================================

def run_cases(assistant, cases: list[dict], *, prompt_version: str, repeat: int = 1,
              progress=None) -> list[dict]:
    records = []
    for run_index in range(1, repeat + 1):
        for case in cases:
            ans = assistant.ask(case["question"], prompt_version=prompt_version, feature="eval")
            rec = answer_record(case, ans, run_index=run_index)
            rec.update(score_record(rec, case))
            records.append(rec)
            if progress:
                progress(rec)
    return records


def build_result(records: list[dict], *, prompt_version: str, model: str, split: str,
                 repeat: int) -> dict:
    return {"kind": "assistant", "prompt_version": prompt_version, "model": model,
            "split": split, "repeat": repeat, "synthetic_data": True,
            "run_at": datetime.now(timezone.utc).isoformat(),
            "summary": summarize(records), "records": records}


def rescore(result: dict, cases: list[dict]) -> dict:
    """Re-score stored records with the current scorers - no LLM call."""
    by_id = {c["id"]: c for c in cases}
    for r in result["records"]:
        if r["case_id"] in by_id:
            r.update(score_record(r, by_id[r["case_id"]]))
    result["summary"] = summarize(result["records"])
    result["rescored_at"] = datetime.now(timezone.utc).isoformat()
    return result


def save(result: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = result["run_at"][:19].replace(":", "").replace("-", "")
    path = out_dir / f"assistant-{result['prompt_version']}-{result['split']}-{stamp}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


# =============================================================================
# The fixed database
# =============================================================================

def load_seed(db, seed_path: Path) -> None:
    """Reset run/observation/report tables of the EVAL database and load the seed.

    Refuses any database whose name does not end in _eval: this deletes data.
    """
    from youtube_intel import repository as repo

    if not db.name.endswith(EVAL_DB_SUFFIX):
        raise EvalCaseError(f"Refusing to reseed {db.name!r}: only *{EVAL_DB_SUFFIX} databases.")
    sql = Path(seed_path).read_text(encoding="utf-8")
    with db.connect() as conn:
        with conn:
            with repo.dict_cursor(conn) as cur:
                cur.execute("TRUNCATE yti.reports, yti.collection_runs CASCADE;")
                cur.execute(sql)
