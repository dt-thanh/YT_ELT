"""
Retrieval evaluation: recall@5 and MRR@5 for vector-only, full-text-only and hybrid.

Labels live in evals/rag_queries.yaml (query -> relevant video ids), reviewed by a
human. `suggest` proposes candidates for unlabeled queries to make labeling
quicker - it never writes labels itself.
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
        if q.get("group") not in (None, "books_learning", "kids_animation"):
            raise RagEvalError(f"{where}: unknown group {q.get('group')!r}")
    return queries


def recall_at_k(ranked: list[str], relevant, k: int = K) -> float:
    relevant = set(relevant)
    return len(set(ranked[:k]) & relevant) / len(relevant) if relevant else 0.0


def mrr_at_k(ranked: list[str], relevant, k: int = K) -> float:
    relevant = set(relevant)
    for i, vid in enumerate(ranked[:k], start=1):
        if vid in relevant:
            return 1.0 / i
    return 0.0


def summarize(per_query: list[dict]) -> dict:
    out = {}
    for mode in MODES:
        rows = [q["modes"][mode] for q in per_query]
        n = len(rows)
        out[mode] = {"recall@5": round(sum(r["recall"] for r in rows) / n, 3) if n else None,
                     "mrr@5": round(sum(r["mrr"] for r in rows) / n, 3) if n else None,
                     "queries": n}
    return out


def run_rag_eval(*, db, client, rag, queries: list[dict]) -> dict:
    """Run every labeled query through the three modes (one embedding per query)."""
    from youtube_intel import repository as repo

    labeled = [q for q in queries if q.get("relevant")]
    per_query = []
    for q in labeled:
        vec = client.embed([q["query"]], feature="eval")[0]
        modes = {}
        with db.connect() as conn:
            with repo.dict_cursor(conn) as cur:
                for name, mode in MODES.items():
                    out = search(cur, query=q["query"], query_vec=vec, rag=rag,
                                 group=q.get("group"), k=K, modes=mode)
                    ranked = [r["video_id"] for r in out.rows]
                    modes[name] = {"ranked": ranked, "recall": recall_at_k(ranked, q["relevant"]),
                                   "mrr": mrr_at_k(ranked, q["relevant"]),
                                   "best_similarity": out.best_similarity,
                                   "no_relevant_results": out.no_relevant_results}
        per_query.append({"id": q["id"], "query": q["query"], "lang": q.get("lang"),
                          "relevant": q["relevant"], "modes": modes})
    return {"run_at": datetime.now(timezone.utc).isoformat(),
            "embedding_model": rag.embedding_model, "min_similarity": rag.min_similarity,
            "k": K, "n_queries": len(queries), "n_labeled": len(labeled),
            "summary": summarize(per_query), "queries": per_query}


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
