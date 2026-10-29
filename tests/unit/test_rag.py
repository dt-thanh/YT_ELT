"""RAG pure parts: normalisation, chunking, planning, fusion, metrics - no DB, no network."""
import pytest

from youtube_intel.config import RagSettings
from youtube_intel.rag.evaluation import RagEvalError, load_queries, mrr_at_k, recall_at_k
from youtube_intel.rag.indexer import plan_chunks, vector_literal
from youtube_intel.rag.retriever import rrf_fuse, search, search_videos_tool
from youtube_intel.rag.text import (
    chunk_video, clean_description, content_hash, normalize, query_terms,
)

RAG = RagSettings(chunk_chars=120, chunk_overlap=20, max_chunks=3, top_k=3, candidates=10,
                  min_similarity=0.3, embedding_dims=3)


# ---- normalisation ----------------------------------------------------------
@pytest.mark.parametrize("text, expected", [
    ("Đọc SÁCH mỗi ngày", "doc sach moi ngay"),     # đ is not decomposed by NFD
    ("Quản lý thời gian", "quan ly thoi gian"),
    ("  Bé   học đếm ", "be hoc dem"),
    ("Habits", "habits"),
])
def test_normalize_strips_vietnamese_accents(text, expected):
    assert normalize(text) == expected


def test_query_terms_are_normalised_and_deduplicated():
    assert query_terms("Đọc sách, đọc SÁCH nhanh!") == ["doc", "sach", "nhanh"]


# ---- chunking -----------------------------------------------------------------
def test_url_only_lines_are_dropped():
    desc = "Bài học hôm nay.\nhttps://patreon.com/x\n- www.facebook.com/y\nXem thêm: https://a.b ở đây"
    assert clean_description(desc) == "Bài học hôm nay.\nXem thêm: https://a.b ở đây"


def test_first_chunk_starts_with_title():
    chunks = chunk_video("Tiêu đề video", "Đoạn một.\n\nĐoạn hai.", chunk_chars=120, overlap=20)
    assert chunks[0].startswith("Tiêu đề video") and len(chunks) == 1


def test_chunks_respect_size_overlap_and_cap():
    paras = "\n\n".join(f"Đoạn văn số {i} " + "x" * 60 for i in range(10))
    chunks = chunk_video("T", paras, chunk_chars=120, overlap=20, max_chunks=3)
    assert len(chunks) == 3 and all(len(c) <= 120 + 2 + 20 for c in chunks)
    assert chunks[1].startswith(chunks[0][-20:])                   # overlap carried over


def test_long_paragraph_is_cut():
    chunks = chunk_video("", "y" * 500, chunk_chars=120, overlap=20, max_chunks=10)
    assert len(chunks) >= 4 and all(c for c in chunks)


def test_empty_description_gives_title_only():
    assert chunk_video("Chỉ có tiêu đề", None) == ["Chỉ có tiêu đề"]


def test_overlap_must_be_smaller_than_chunk():
    with pytest.raises(ValueError):
        chunk_video("t", "d", chunk_chars=100, overlap=100)


# ---- indexer planning -----------------------------------------------------------
def test_unchanged_chunks_are_not_re_embedded_and_stale_ones_removed():
    videos = [{"video_id": "vidA0000001", "title": "A", "description": "mô tả A"},
              {"video_id": "vidB0000001", "title": "B", "description": None}]
    first, unchanged, stale = plan_chunks(videos, {}, RAG)
    assert len(first) == 2 and unchanged == 0 and stale == []
    existing = {(c.video_id, c.chunk_index): c.content_hash for c in first}
    existing[("vidA0000001", 4)] = "old"                            # chunk the video no longer has
    again, unchanged, stale = plan_chunks(videos, existing, RAG)
    assert again == [] and unchanged == 2 and stale == [("vidA0000001", 4)]
    changed = [{**videos[0], "description": "mô tả mới"}, videos[1]]
    redo, _, _ = plan_chunks(changed, existing, RAG)
    assert [c.video_id for c in redo] == ["vidA0000001"]
    assert redo[0].search_text == normalize(redo[0].content)


def test_content_hash_and_vector_literal():
    assert content_hash("a") == content_hash("a") != content_hash("b")
    assert vector_literal([0.1, 2, -0.5]) == "[0.1,2,-0.5]"


# ---- fusion --------------------------------------------------------------------
def test_rrf_rewards_agreement_between_modes():
    fused = rrf_fuse({"vector": ["a", "b", "c"], "fulltext": ["b", "d"]}, k=60)
    assert fused[0][0] == "b" and fused[0][2] == ("vector", "fulltext")
    assert fused[0][1] == pytest.approx(1 / 62 + 1 / 61)


def test_rrf_is_deterministic_on_ties():
    assert [d for d, _, _ in rrf_fuse({"vector": ["z"], "fulltext": ["a"]})] == ["a", "z"]


def test_rrf_ignores_duplicates_within_a_list():
    fused = dict((d, s) for d, s, _ in rrf_fuse({"vector": ["a", "a", "b"]}, k=60))
    assert fused["b"] == pytest.approx(1 / 62)


# ---- search() with a scripted cursor ----------------------------------------------
class ScriptedCursor:
    """Returns the next scripted row list for each SELECT, in order."""

    def __init__(self, *results):
        self.results, self.sql = list(results), []

    def execute(self, sql, params=None):
        self.sql.append(sql)
        self._next = self.results.pop(0)

    def fetchall(self):
        return self._next


META = [{"video_id": "v1", "video_title": "Thói quen", "channel_title": "K", "group_code": "g"},
        {"video_id": "v2", "video_title": "Thời gian", "channel_title": "K", "group_code": "g"}]


def test_search_fuses_and_groups_per_video():
    vec = [{"video_id": "v1", "content": "a" * 400, "similarity": 0.8},
           {"video_id": "v1", "content": "second chunk", "similarity": 0.7},
           {"video_id": "v2", "content": "b", "similarity": 0.6}]
    txt = [{"video_id": "v2", "content": "b", "text_rank": 0.5}]
    out = search(ScriptedCursor(vec, txt, META), query="thói quen", query_vec=[0, 0, 1], rag=RAG)
    assert [r["video_id"] for r in out.rows] == ["v2", "v1"]      # v2 found by both modes
    assert out.rows[0]["matched_by"] == "both" and len(out.rows[1]["snippet"]) == 300


def test_search_reports_no_relevant_results():
    weak = [{"video_id": "v1", "content": "a", "similarity": 0.1}]
    out = search(ScriptedCursor(weak, []), query="xyz", query_vec=[0, 0, 1], rag=RAG)
    assert out.no_relevant_results and out.rows == []


def test_text_match_alone_is_relevant_in_hybrid():
    weak = [{"video_id": "v1", "content": "a", "similarity": 0.1}]
    txt = [{"video_id": "v1", "content": "a", "text_rank": 0.2}]
    out = search(ScriptedCursor(weak, txt, META), query="quen", query_vec=[0, 0, 1], rag=RAG)
    assert not out.no_relevant_results and out.rows[0]["video_id"] == "v1"


def test_search_videos_embeds_before_the_transaction():
    from types import SimpleNamespace as NS
    from youtube_intel.assistant.tools import build_registry
    order = []

    class Client:
        def embed(self, texts, feature):
            order.append("embed")
            return [[0.0, 0.0, 1.0]]

    class Executor:
        def execute(self, tool, args):
            order.append("db")
            assert args["_query_vec"] == [0.0, 0.0, 1.0]
            return tool.handler(ScriptedCursor([], []), max_rows=20, **args)

    cfg = NS(settings=NS(rag=RAG), channels=[NS(group="g")])
    reg = build_registry(["g"], [search_videos_tool(cfg, Client())])
    res = reg.run("search_videos", {"query": "thói quen"}, Executor())
    assert order == ["embed", "db"]
    assert res.row_count == 0 and res.extra == {"no_relevant_results": True}


# ---- metrics and query file ----------------------------------------------------------
def test_recall_and_mrr():
    assert recall_at_k(["a", "x", "b"], ["a", "b"]) == 1.0
    assert recall_at_k(["x", "y"], ["a", "b"]) == 0.0
    assert mrr_at_k(["x", "a"], ["a"]) == 0.5 and mrr_at_k(["x"], ["a"]) == 0.0
    assert recall_at_k(["a", "b", "c", "d", "e", "f"], ["f"]) == 0.0   # outside top 5


def test_shipped_query_file_is_valid():
    from pathlib import Path
    qs = load_queries(Path(__file__).resolve().parents[2] / "evals" / "rag_queries.yaml")
    assert len(qs) >= 25 and {q["lang"] for q in qs} >= {"vi", "vi_plain", "en"}


def test_bad_query_file_is_rejected(tmp_path):
    p = tmp_path / "q.yaml"
    p.write_text("queries:\n  - {id: a, query: x}\n  - {id: a, query: y}\n", encoding="utf-8")
    with pytest.raises(RagEvalError):
        load_queries(p)
