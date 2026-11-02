"""P2 against a real Postgres (migration 012): loading is idempotent, an edited passage
loses its embedding, hybrid search and aligned passages. Fake vectors; rolls back."""
from datetime import date

import pytest

from canon.chunk import Passage
from canon.ingest import Source, load_work, store_embeddings
from canon.retrieve import get_passage, search
from youtube_intel import repository as repo

pytestmark = pytest.mark.integration


def src(sid, language, **over):
    base = dict(id=sid, work="W", work_vi="Tác phẩm thử", author="A", translator=None,
                pub_year=1900, language=language, pillar="east", license="public_domain",
                status="verified", verified_on=date(2026, 11, 2), source_url="https://x",
                fetch=(), format="text", parser={}, ref_label="chương {ch}",
                align="test.{ch}", evidence=("test",))
    return Source(**{**base, **over})


def vec(i):
    v = [0.0] * 1024
    v[i] = 1.0
    return v


@pytest.fixture
def cur():
    from youtube_intel.config import load_config
    try:
        s = load_config(require=("db",)).secrets
    except Exception as e:
        pytest.skip(f"database not configured: {e}")
    db = repo.Database(host=s.db_host, port=s.db_port, name=s.db_name,
                       user=s.db_user, password=s.db_password)
    with db.connect() as conn:
        with repo.dict_cursor(conn) as c:
            yield c
            conn.rollback()


def load_pair(cur):
    zh = src("zztest.zh", "zh")
    en = src("zztest.en", "en", translator="T")
    load_work(cur, zh, [Passage({"ch": 1}, "上善若水。水善利萬物而不爭。"),
                        Passage({"ch": 2}, "知人者智，自知者明。")])
    load_work(cur, en, [Passage({"ch": 1}, "The highest excellence is like water."),
                        Passage({"ch": 2}, "He who knows zzquux men is discerning.")])
    store_embeddings(cur, [{"passage_id": "zztest.zh.1"}, {"passage_id": "zztest.en.1"},
                           {"passage_id": "zztest.zh.2"}, {"passage_id": "zztest.en.2"}],
                     [vec(0), vec(0), vec(1), vec(1)], "fake")


def test_reloading_is_idempotent_and_an_edit_drops_the_embedding(cur):
    load_pair(cur)
    out = load_work(cur, src("zztest.zh", "zh"), [Passage({"ch": 1}, "上善若水。水善利萬物而不爭。"),
                                                  Passage({"ch": 2}, "知人者智，自知者明。改")])
    cur.execute("SELECT passage_id, embedding IS NOT NULL AS has FROM yti.canon_passages "
                "WHERE work_id = 'zztest.zh' ORDER BY passage_id")
    assert out["passages"] == 2 and [r["has"] for r in cur.fetchall()] == [True, False]


def test_chinese_full_text_matches_with_the_same_tokens_as_the_index(cur):
    load_pair(cur)
    hits = search(cur, query="上善若水", query_vec=None, k=5, work_id="zztest.zh",
                  modes=("fulltext",))
    assert hits[0]["passage_id"] == "zztest.zh.1" and hits[0]["matched_by"] == "fulltext"


def test_hybrid_finds_both_languages_and_aligned_passages_come_back(cur):
    load_pair(cur)
    # 'zzquux' is in no real passage, so the loaded canon cannot tie with the test rows
    hits = search(cur, query="zzquux", query_vec=vec(1), k=3, pillar="east")
    ids = [h["passage_id"] for h in hits if h["passage_id"].startswith("zztest.")]
    assert ids[:2] == ["zztest.en.2", "zztest.zh.2"]            # en: both modes, zh: vector
    p = get_passage(cur, "zztest.zh.2")
    assert p["text"] == "知人者智，自知者明。" and p["license"] == "public_domain"
    assert [a["passage_id"] for a in p["aligned"]] == ["zztest.en.2"]
