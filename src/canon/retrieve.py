"""
Canon retrieval (C-03): hybrid vector (bge-m3) + full-text, fused with Reciprocal
Rank Fusion like the video search. A Vietnamese question ("kiểm soát cơn giận")
finds English, Latin or Chinese passages through the multilingual embedding; the
full-text side uses canon/text.py search_tokens() - the tokeniser that built the
index (spec §9: never a different configuration at query time).
"""

from __future__ import annotations

from canon.text import tsquery
from youtube_intel.rag.indexer import vector_literal
from youtube_intel.rag.retriever import rrf_fuse

MODES = ("vector", "fulltext")
SNIPPET = 400

_FILTERS = """
   AND (%(language)s IS NULL OR w.language = %(language)s)
   AND (%(pillar)s   IS NULL OR w.pillar   = %(pillar)s)
   AND (%(work_id)s  IS NULL OR w.work_id  = %(work_id)s)"""


def search(cur, *, query: str, query_vec, k: int = 5, candidates: int = 30, rrf_k: int = 60,
           language: str | None = None, pillar: str | None = None, work_id: str | None = None,
           modes=MODES) -> list[dict]:
    params = {"language": language, "pillar": pillar, "work_id": work_id, "n": candidates}
    rankings, sim = {}, {}
    if "vector" in modes and query_vec is not None:
        # EXACT nearest neighbours: the HNSW index applies the language/pillar filter
        # after its ~40 approximate candidates, so a filtered search could silently
        # miss passages. The canon is a few thousand rows - a full scan takes ms.
        cur.execute(
            """WITH d AS MATERIALIZED (
                   SELECT p.passage_id, p.embedding <=> %(vec)s::vector AS distance
                     FROM yti.canon_passages p JOIN yti.canon_works w USING (work_id)
                    WHERE p.embedding IS NOT NULL""" + _FILTERS + """)
               SELECT passage_id, 1 - distance AS similarity FROM d
                ORDER BY distance, passage_id LIMIT %(n)s""",
            {**params, "vec": vector_literal(query_vec)})
        rows = cur.fetchall()
        rankings["vector"] = [r["passage_id"] for r in rows]
        sim = {r["passage_id"]: float(r["similarity"]) for r in rows}
    tsq = tsquery(query)
    if "fulltext" in modes and tsq:
        cur.execute(
            """SELECT p.passage_id FROM yti.canon_passages p JOIN yti.canon_works w USING (work_id),
                      to_tsquery('simple', %(tsq)s) q
                WHERE p.tsv @@ q""" + _FILTERS + """
                ORDER BY ts_rank(p.tsv, q) DESC, p.passage_id LIMIT %(n)s""",
            {**params, "tsq": tsq})
        rankings["fulltext"] = [r["passage_id"] for r in cur.fetchall()]
    fused = rrf_fuse(rankings, rrf_k)[:k]
    if not fused:
        return []
    cur.execute(
        """SELECT p.passage_id, p.work_id, w.work_vi, w.author, w.translator, w.language,
                  w.license, p.ref, p.ref_label, left(p.text, %(snip)s) AS snippet
             FROM yti.canon_passages p JOIN yti.canon_works w USING (work_id)
            WHERE p.passage_id = ANY(%(ids)s)""",
        {"ids": [d for d, _, _ in fused], "snip": SNIPPET})
    meta = {r["passage_id"]: dict(r) for r in cur.fetchall()}
    return [{**meta[d], "rank": i, "score": round(s, 4), "similarity":
             round(sim[d], 3) if d in sim else None,
             "matched_by": "both" if len(found) == 2 else found[0]}
            for i, (d, s, found) in enumerate(fused, start=1) if d in meta]


def get_passage(cur, passage_id: str) -> dict | None:
    """The exact text of one passage, its reference, license, and the passages aligned
    with it (an original and its translation share an align_group)."""
    cur.execute(
        """SELECT p.passage_id, p.work_id, w.work, w.work_vi, w.author, w.translator,
                  w.pub_year, w.language, w.license, w.source_url, p.ref, p.ref_label,
                  p.text, p.align_group
             FROM yti.canon_passages p JOIN yti.canon_works w USING (work_id)
            WHERE p.passage_id = %s""", (passage_id,))
    row = cur.fetchone()
    if row is None:
        return None
    out = dict(row)
    out["aligned"] = []
    if row["align_group"]:
        cur.execute(
            """SELECT p.passage_id, w.language, w.translator, p.ref_label, p.text
                 FROM yti.canon_passages p JOIN yti.canon_works w USING (work_id)
                WHERE p.align_group = %s AND p.passage_id <> %s ORDER BY w.language""",
            (row["align_group"], passage_id))
        out["aligned"] = [dict(r) for r in cur.fetchall()]
    return out
