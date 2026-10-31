"""
Retrieval evaluation: recall@5 and MRR@5 for vector-only, full-text-only and hybrid,
plus how often a query that should find nothing really returns no_relevant_results.

Labels live in evals/rag_queries.yaml (query -> relevant video ids, or
expect_no_results: true), reviewed by a human. `suggest` proposes candidates for
unlabeled queries to make labeling quicker - it never writes labels itself.

min_similarity only gates a whole result set (retriever.search): it never
reorders results. So each query is searched once with the gate off, and the
metrics for any threshold are computed from that run (`sweep`).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import yaml

from youtube_intel.rag.retriever import search

MODES = {"vector": ("vector",), "fulltext": ("fulltext",), "hybrid": ("vector", "fulltext")}
K = 5


class RagEvalError(ValueError):
    pass


def load_queries(path: Path) -> list[dict]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    queries = (data or {}).get("queries")
    if not isinstance(queries, list) or not queries:
        raise RagEvalError(f"{path}: needs a non-empty 'queries' list")
    seen = set()
    for i, q in enumerate(queries):
        where = f"{path}: queries[{i}]"
        if not isinstance(q, dict) or not q.get("id") or not q.get("query"):
            raise RagEvalError(f"{where}: needs id and query")
        if q["id"] in seen:
            raise RagEvalError(f"{where}: duplicate id {q['id']!r}")
        seen.add(q["id"])
        rel = q.get("relevant", [])
        if not isinstance(rel, list) or not all(isinstance(v, str) for v in rel):
            raise RagEvalError(f"{where}: relevant must be a list of video ids")
        if q.get("expect_no_results") and rel:
            raise RagEvalError(f"{where}: expect_no_results with relevant ids")
        if q.get("group") not in (None, "books_learning", "kids_animation"):
            raise RagEvalError(f"{where}: unknown group {q.get('group')!r}")
    return queries


def recall_at_k(ranked: list[str], relevant, k: int = K) -> float:
    """Share of what top k COULD hold: the denominator is min(|relevant|, k), so a
    query with 10 relevant videos can still reach 1.0."""
    relevant = set(relevant)
    return len(set(ranked[:k]) & relevant) / min(len(relevant), k) if relevant else 0.0


def mrr_at_k(ranked: list[str], relevant, k: int = K) -> float:
    relevant = set(relevant)
    for i, vid in enumerate(ranked[:k], start=1):
        if vid in relevant:
            return 1.0 / i
    return 0.0


def gated(mode_result: dict, mode: str, threshold: float) -> bool:
    """Would retriever.search return no_relevant_results at this threshold?"""
    best, text_hits = mode_result["best_similarity"], mode_result["text_hits"]
    below = best is None or best < threshold
    if mode == "hybrid":
        return not text_hits and below
    if mode == "vector":
        return below
    return not text_hits


def score_at(per_query: list[dict], mode: str, threshold: float) -> dict:
    """Metrics for one mode if min_similarity were `threshold`."""
    pos = [q for q in per_query if q["relevant"]]
    neg = [q for q in per_query if q.get("expect_no_results")]
    recall = mrr = 0.0
    for q in pos:
        r = q["modes"][mode]
        ranked = [] if gated(r, mode, threshold) else r["ranked"]
        recall += recall_at_k(ranked, q["relevant"])
        mrr += mrr_at_k(ranked, q["relevant"])
    empty = sum(gated(q["modes"][mode], mode, threshold) for q in neg)
    return {"recall@5": round(recall / len(pos), 3) if pos else None,
            "mrr@5": round(mrr / len(pos), 3) if pos else None,
            "positives_lost": sum(gated(q["modes"][mode], mode, threshold) for q in pos),
            "no_results_correct": f"{empty}/{len(neg)}",
            "queries": len(pos)}


def summarize(per_query: list[dict], threshold: float) -> dict:
    return {mode: score_at(per_query, mode, threshold) for mode in MODES}


SWEEP = tuple(round(0.05 * i, 2) for i in range(13))          # 0.00 .. 0.60


def sweep(per_query: list[dict]) -> list[dict]:
    """Hybrid metrics for each candidate min_similarity (the mode the assistant uses)."""
    return [{"min_similarity": t, **score_at(per_query, "hybrid", t)} for t in SWEEP]


def run_rag_eval(*, db, client, rag, queries: list[dict]) -> dict:
    """Run every labeled query through the three modes, gate off (one embedding per query)."""
    from dataclasses import replace
    from youtube_intel import repository as repo

    ungated = replace(rag, min_similarity=0.0)
    labeled = [q for q in queries if q.get("relevant") or q.get("expect_no_results")]
    per_query = []
    for q in labeled:
        vec = client.embed([q["query"]], feature="eval")[0]
        modes = {}
        with db.connect() as conn:
            with repo.dict_cursor(conn) as cur:
                for name, mode in MODES.items():
                    out = search(cur, query=q["query"], query_vec=vec, rag=ungated,
                                 group=q.get("group"), k=K, modes=mode)
                    modes[name] = {"ranked": [r["video_id"] for r in out.rows],
                                   "best_similarity": out.best_similarity,
                                   "text_hits": out.text_hits}
        rel = q.get("relevant", [])
        for name, r in modes.items():
            r.update({"recall": recall_at_k(r["ranked"], rel) if rel else None,
                      "mrr": mrr_at_k(r["ranked"], rel) if rel else None,
                      "no_relevant_results": gated(r, name, rag.min_similarity)})
        per_query.append({"id": q["id"], "query": q["query"], "lang": q.get("lang"),
                          "relevant": rel, "expect_no_results": bool(q.get("expect_no_results")),
                          "modes": modes})
    return {"run_at": datetime.now(timezone.utc).isoformat(),
            "embedding_model": rag.embedding_model, "min_similarity": rag.min_similarity,
            "k": K, "recall_denominator": "min(|relevant|, k)",
            "n_queries": len(queries), "n_labeled": len(labeled),
            "summary": summarize(per_query, rag.min_similarity),
            "sweep_hybrid": sweep(per_query), "queries": per_query}


def suggest(*, db, client, rag, queries: list[dict], k: int = 10) -> list[dict]:
    """Hybrid top-k (no relevance cut-off) for each UNLABELED query, for a human to label."""
    from youtube_intel import repository as repo
    from dataclasses import replace

    open_rag = replace(rag, min_similarity=0.0)
    out = []
    for q in (q for q in queries if not q.get("relevant")):
        vec = client.embed([q["query"]], feature="eval")[0]
        with db.connect() as conn:
            with repo.dict_cursor(conn) as cur:
                res = search(cur, query=q["query"], query_vec=vec,
                             rag=replace(open_rag, top_k=k), group=q.get("group"), k=k)
        out.append({"id": q["id"], "query": q["query"], "candidates": [
            {"video_id": r["video_id"], "title": r["video_title"], "channel": r["channel_title"],
             "similarity": r["similarity"], "matched_by": r["matched_by"]} for r in res.rows]})
    return out


def save(result, out_dir: Path, prefix: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"{prefix}-{stamp}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path
