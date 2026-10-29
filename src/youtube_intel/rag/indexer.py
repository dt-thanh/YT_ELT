"""
Builds yti.video_chunks from yti.videos_current: chunk -> embed -> upsert.

Idempotent and cheap to re-run: a chunk is embedded only when it is new or its
text changed (content_hash). Chunks a video no longer has are deleted.
Embedding calls go through LLMClient.embed (logged as feature="embedding") and
happen outside any database transaction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from youtube_intel.rag.text import chunk_video, content_hash, normalize

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChunkPlan:
    video_id: str
    chunk_index: int
    content: str
    search_text: str
    content_hash: str


@dataclass(frozen=True)
class IndexStats:
    videos: int
    chunks: int            # chunks the current text produces
    to_embed: int          # new or changed
    unchanged: int
    removed: int           # chunks a video no longer has
    embedded: int = 0
    dry_run: bool = False


def plan_chunks(videos, existing: dict, rag) -> tuple[list[ChunkPlan], int, list[tuple[str, int]]]:
    """Pure planning step.

    videos:   [{"video_id", "title", "description"}]
    existing: {(video_id, chunk_index): content_hash} already stored for this model
    Returns (chunks to embed, number unchanged, stale (video_id, chunk_index) keys).
    """
    to_embed: list[ChunkPlan] = []
    unchanged = 0
    wanted: set[tuple[str, int]] = set()
    for v in videos:
        chunks = chunk_video(v.get("title"), v.get("description"), chunk_chars=rag.chunk_chars,
                             overlap=rag.chunk_overlap, max_chunks=rag.max_chunks)
        for i, text in enumerate(chunks):
            key = (v["video_id"], i)
            wanted.add(key)
            h = content_hash(text)
            if existing.get(key) == h:
                unchanged += 1
                continue
            to_embed.append(ChunkPlan(v["video_id"], i, text, normalize(text), h))
    stale = sorted(k for k in existing if k not in wanted)
    return to_embed, unchanged, stale


def vector_literal(vec) -> str:
    """pgvector text input: '[0.1,0.2,...]' (no extra Python dependency needed)."""
    return "[" + ",".join(format(float(x), ".7g") for x in vec) + "]"


def index_videos(db, client, rag, *, dry_run: bool = False) -> IndexStats:
    from youtube_intel import repository as repo

    with db.connect() as conn:
        with repo.dict_cursor(conn) as cur:
            cur.execute("SELECT video_id, title, description FROM yti.videos_current ORDER BY video_id")
            videos = [dict(r) for r in cur.fetchall()]
            cur.execute("""SELECT video_id, chunk_index, content_hash FROM yti.video_chunks
                            WHERE embedding_model = %s""", (rag.embedding_model,))
            existing = {(r["video_id"], r["chunk_index"]): r["content_hash"] for r in cur.fetchall()}

    to_embed, unchanged, stale = plan_chunks(videos, existing, rag)
    n_chunks = len(to_embed) + unchanged
    stats = IndexStats(videos=len(videos), chunks=n_chunks, to_embed=len(to_embed),
                       unchanged=unchanged, removed=len(stale), dry_run=dry_run)
    if dry_run:
        return stats

    embedded = 0
    for start in range(0, len(to_embed), rag.batch_size):
        batch = to_embed[start:start + rag.batch_size]
        vectors = client.embed([c.content for c in batch], feature="embedding")
        if len(vectors) != len(batch) or any(len(v) != rag.embedding_dims for v in vectors):
            raise ValueError(f"Embedding size mismatch: expected {len(batch)} x {rag.embedding_dims}")
        with db.connect() as conn:
            with conn:
                with repo.dict_cursor(conn) as cur:
                    for c, vec in zip(batch, vectors):
                        cur.execute(
                            """
                            INSERT INTO yti.video_chunks
                                (video_id, chunk_index, content, search_text, content_hash,
                                 embedding, embedding_model)
                            VALUES (%s, %s, %s, %s, %s, %s::vector, %s)
                            ON CONFLICT (video_id, chunk_index, embedding_model) DO UPDATE SET
                                content      = EXCLUDED.content,
                                search_text  = EXCLUDED.search_text,
                                content_hash = EXCLUDED.content_hash,
                                embedding    = EXCLUDED.embedding,
                                embedded_at  = now();
                            """,
                            (c.video_id, c.chunk_index, c.content, c.search_text, c.content_hash,
                             vector_literal(vec), rag.embedding_model),
                        )
        embedded += len(batch)
        logger.info("Embedded %d/%d chunks", embedded, len(to_embed))

    if stale:
        with db.connect() as conn:
            with conn:
                with repo.dict_cursor(conn) as cur:
                    for vid, idx in stale:
                        cur.execute("""DELETE FROM yti.video_chunks
                                        WHERE video_id = %s AND chunk_index = %s
                                          AND embedding_model = %s""",
                                    (vid, idx, rag.embedding_model))
    return IndexStats(**{**stats.__dict__, "embedded": embedded})
