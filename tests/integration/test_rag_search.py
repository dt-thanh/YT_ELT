"""
Retrieval SQL against real Postgres + pgvector, with deterministic fake
embeddings (derived from a hash of the text) - no OpenAI call.

Needs migrations 006/007 (pgvector image). Seeds inside a transaction, rolls back.
"""
import hashlib
import math

import pytest

from youtube_intel.config import RagSettings
from youtube_intel.rag.indexer import vector_literal
from youtube_intel.rag.retriever import search
from youtube_intel.rag.text import normalize

pytestmark = pytest.mark.integration

CH = "UC" + "R" * 22
DIMS = 1536
RAG = RagSettings(min_similarity=0.5)
DOCS = {
    "ragHABIT001": "Thói quen tốt mỗi ngày: dậy sớm, đọc sách và tập thể dục.",
    "ragMONEY001": "Tiền được tạo ra như thế nào và vì sao lạm phát xảy ra.",
    "ragBEACH001": "Gia đình đi chơi biển, xây lâu đài cát.",
}


def fake_embedding(text: str) -> list[float]:
    """Deterministic unit vector from the text's hash: identical text -> identical
    vector (similarity 1), different text -> nearly orthogonal."""
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    vals = [((seed[i % 32] * (i + 1)) % 251) - 125 for i in range(DIMS)]
    norm = math.sqrt(sum(v * v for v in vals))
    return [v / norm for v in vals]


@pytest.fixture
def cur():
    from youtube_intel import repository as repo
    from youtube_intel.config import load_config
    try:
        s = load_config(require=("db",)).secrets
    except Exception as e:
        pytest.skip(f"database not configured: {e}")
    db = repo.Database(host=s.db_host, port=s.db_port, name=s.db_name,
                       user=s.db_user, password=s.db_password)
    with db.connect() as conn:
        with repo.dict_cursor(conn) as c:
            c.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
            if c.fetchone() is None:
                pytest.skip("pgvector not installed (migration 006)")
            c.execute("""INSERT INTO yti.tracked_channels (channel_id, group_code, title)
                         VALUES (%s, 'books_learning', 'RAG seed')""", (CH,))
            for vid, text in DOCS.items():
                c.execute("""INSERT INTO yti.videos_current (video_id, channel_id, title, published_at)
                             VALUES (%s, %s, %s, now())""", (vid, CH, text[:30]))
                c.execute("""INSERT INTO yti.video_chunks (video_id, chunk_index, content,
                                 search_text, content_hash, embedding, embedding_model)
                             VALUES (%s, 0, %s, %s, 'h', %s::vector, %s)""",
                          (vid, text, normalize(text), vector_literal(fake_embedding(text)),
                           RAG.embedding_model))
            yield c
            conn.rollback()


def test_vector_search_finds_identical_text(cur):
    text = DOCS["ragMONEY001"]
    out = search(cur, query="zzz", query_vec=fake_embedding(text), rag=RAG,
                 group="books_learning", modes=("vector",))
    assert out.rows[0]["video_id"] == "ragMONEY001" and out.rows[0]["similarity"] == 1.0


def test_fulltext_matches_without_accents(cur):
    out = search(cur, query="thoi quen doc sach", query_vec=None, rag=RAG,
                 group="books_learning", modes=("fulltext",))
    assert out.rows and out.rows[0]["video_id"] == "ragHABIT001"
    assert out.rows[0]["matched_by"] == "fulltext"


def test_hybrid_ranks_double_hit_first(cur):
    out = search(cur, query="lạm phát", query_vec=fake_embedding(DOCS["ragMONEY001"]), rag=RAG,
                 group="books_learning")
    assert out.rows[0]["video_id"] == "ragMONEY001" and out.rows[0]["matched_by"] == "both"


def test_unrelated_query_returns_nothing(cur):
    out = search(cur, query="qwertyuiop", query_vec=fake_embedding("unrelated text"),
                 rag=RAG, group="books_learning")
    seeded = {r["video_id"] for r in out.rows} & set(DOCS)
    assert out.no_relevant_results or not seeded
