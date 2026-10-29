"""
The eval seed must produce exactly the hand-computed numbers the cases expect.
Loads the seed inside a transaction on the configured database and rolls back.
"""
from pathlib import Path

import pytest

from youtube_intel.assistant import tools as T

pytestmark = pytest.mark.integration

SEED = Path(__file__).resolve().parents[2] / "evals" / "fixtures" / "assistant_seed.sql"


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
            c.execute(SEED.read_text(encoding="utf-8"))
            yield c
            conn.rollback()


def vph(cur):
    cur.execute("""SELECT video_id, views_per_hour, validity_reason, is_rankable
                     FROM yti.v_video_growth WHERE video_id LIKE 'syn%'""")
    return {r["video_id"]: r for r in cur.fetchall()}


def test_growth_numbers_match_the_seed_header(cur):
    g = vph(cur)
    expected = {"synBook0001": 1000, "synBook0002": 500, "synBook0003": 100, "synBook0006": 20,
                "synKids0001": 2000, "synKids0002": 500, "synKids0003": 100,
                "synKids0004": 10, "synKids0005": 0}
    for vid, value in expected.items():
        assert float(g[vid]["views_per_hour"]) == value and g[vid]["is_rankable"], vid
    assert g["synBook0004"]["validity_reason"] == "view_count_decreased"
    assert g["synBook0005"]["validity_reason"] == "insufficient_history"
    assert not g["synKids0006"]["is_rankable"]                     # livestream


def test_tool_results_match_case_expectations(cur):
    books = T.compare_channels(cur, max_rows=20, group="books_learning",
                               metric="median_views_per_hour")
    by_ch = {r["channel_id"]: r for r in books.rows if r["channel_id"].startswith("UCsyn")}
    assert float(by_ch["UCsynBooksAAAAAAAAAAAAAA"]["median_views_per_hour"]) == 550
    assert float(by_ch["UCsynBooksBBBBBBBBBBBBBB"]["median_views_per_hour"]) == 260
    kids = T.top_growing_videos(cur, max_rows=20, channel_id="UCsynKidsBBBBBBBBBBBBBBB")
    assert kids.rows[0]["video_id"] == "synKids0002"
    brief = T.get_latest_brief(cur, max_rows=20, group="kids_animation")
    assert brief.rows[0]["video_id"] == "synKids0001"
