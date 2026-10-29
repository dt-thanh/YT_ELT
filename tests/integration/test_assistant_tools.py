"""
Assistant tools against a real Postgres. Each test seeds its own rows inside the
fixture's transaction and rolls back - nothing is left behind.

Run:  pytest tests/integration -m integration
"""
import os
import uuid
from datetime import timedelta

import pytest

from youtube_intel.assistant import tools as T

pytestmark = pytest.mark.integration

CH = "UC" + "S" * 22                 # seeded channel (not a real one)
V_FAST, V_SLOW, V_NEW = "seedFAST001", "seedSLOW001", "seedNEW0001"


@pytest.fixture
def db_settings():
    from youtube_intel.config import load_config
    try:
        return load_config(require=("db",))
    except Exception as e:
        pytest.skip(f"database not configured: {e}")


@pytest.fixture
def cur(db_settings):
    from youtube_intel import repository as repo
    s = db_settings.secrets
    db = repo.Database(host=s.db_host, port=s.db_port, name=s.db_name,
                       user=s.db_user, password=s.db_password)
    with db.connect() as conn:
        with repo.dict_cursor(conn) as c:
            seed(c)
            yield c
            conn.rollback()


def seed(cur):
    cur.execute("SELECT now() AS now")
    now = cur.fetchone()["now"]
    cur.execute("""INSERT INTO yti.tracked_channels (channel_id, group_code, title)
                   VALUES (%s, 'kids_animation', 'Seed Channel')""", (CH,))
    for vid, title in ((V_FAST, "Seed fast video"), (V_SLOW, "Seed slow video"), (V_NEW, "Seed new video")):
        cur.execute("""INSERT INTO yti.videos_current (video_id, channel_id, title, published_at,
                           duration_seconds, live_state, format_label)
                       VALUES (%s, %s, %s, %s, 600, 'none', 'normal')""",
                    (vid, CH, title, now - timedelta(days=3)))
    runs = []
    for hours_ago in (24, 0):
        cid, bid = str(uuid.uuid4()), str(uuid.uuid4())
        t = now - timedelta(hours=hours_ago, minutes=1)
        cur.execute("""INSERT INTO yti.collection_runs (collection_id, scheduled_for, started_at, status)
                       VALUES (%s, %s, %s, 'succeeded')""", (cid, t, t))
        cur.execute("""INSERT INTO yti.channel_batches (batch_id, collection_id, channel_id, status, quality_status)
                       VALUES (%s, %s, %s, 'succeeded', 'passed')""", (bid, cid, CH))
        runs.append((cid, bid, t))
    obs = {V_FAST: (1000, 3400), V_SLOW: (500, 740), V_NEW: (None, 90)}   # V_NEW: one observation
    for (cid, bid, t), idx in zip(runs, (0, 1)):
        for vid, views in obs.items():
            if views[idx] is None:
                continue
            cur.execute("""INSERT INTO yti.video_observations
                               (video_id, collection_id, batch_id, observed_at, view_count, like_count)
                           VALUES (%s, %s, %s, %s, %s, NULL)""", (vid, cid, bid, t, views[idx]))


def test_top_growing_videos_ranks_and_computes_share(cur):
    r = T.top_growing_videos(cur, max_rows=20, channel_id=CH)
    assert [row["video_id"] for row in r.rows] == [V_FAST, V_SLOW]          # V_NEW not rankable
    assert [row["rank"] for row in r.rows] == [1, 2]
    assert float(r.rows[0]["views_per_hour"]) == 100.0                       # 2400 views / 24 h
    assert float(r.rows[1]["pct_of_leader"]) == 10.0                         # 10 / 100
    assert r.rows[0]["likes"] is None                                        # NULL stays NULL
    assert r.as_of and r.video_ids == (V_FAST, V_SLOW)


def test_top_growing_videos_respects_limit(cur):
    r = T.top_growing_videos(cur, max_rows=20, channel_id=CH, limit=1)
    assert r.row_count == 1 and r.truncated


def test_video_stats_reports_why_growth_is_missing(cur):
    r = T.get_video_stats(cur, max_rows=20, video_id=V_NEW)
    assert r.rows[0]["validity_reason"] == "insufficient_history"
    assert r.rows[0]["views_per_hour"] is None


def test_video_history_newest_first(cur):
    r = T.get_video_history(cur, max_rows=20, video_id=V_FAST)
    assert [row["view_count"] for row in r.rows] == [3400, 1000]


def test_unknown_video_returns_no_rows(cur):
    assert T.get_video_stats(cur, max_rows=20, video_id="doesNotExist").row_count == 0


def test_list_channels_and_coverage(cur):
    chans = T.list_channels(cur, max_rows=50, group="kids_animation")
    seeded = next(r for r in chans.rows if r["channel_id"] == CH)
    assert seeded["videos"] == 3
    cov = T.get_data_coverage(cur, max_rows=20, group="kids_animation")
    assert cov.row_count == 1 and cov.rows[0]["rankable_videos"] >= 2
    assert "insufficient_history" in cov.rows[0]["reason_counts"]


def test_compare_channels_metric_and_gap(cur):
    r = T.compare_channels(cur, max_rows=50, group="kids_animation", metric="rankable_videos")
    seeded = next(row for row in r.rows if row["channel_id"] == CH)
    assert seeded["rankable_videos"] == 2 and seeded["metric"] == "rankable_videos"
    assert float(seeded["gap_to_leader"]) >= 0


def test_latest_brief(cur):
    cur.execute("""INSERT INTO yti.reports (report_id, group_code, as_of, status, model_name,
                       prompt_version, validated_payload, evidence_bundle)
                   VALUES (%s, 'kids_animation', now() + interval '10 years', 'validated',
                           'gpt-test', 'v3', %s::jsonb, %s::jsonb)""",
                (str(uuid.uuid4()),
                 '{"scope_statement": "s", "items": [{"video_id": "seedFAST001", "why_selected": "w"}]}',
                 '{"candidates": [{"video_id": "seedFAST001", "title": "Seed fast video", '
                 '"metrics": {"views_per_hour": 100.0}}]}'))
    r = T.get_latest_brief(cur, max_rows=20, group="kids_animation")
    assert r.extra["written_by"] == "gpt-test" and r.rows[0]["views_per_hour"] == 100.0


def test_registry_and_executor_end_to_end(db_settings):
    """DbToolExecutor: read-only transaction + timeout, as the ELT user here."""
    from youtube_intel import repository as repo
    s = db_settings.secrets
    db = repo.Database(host=s.db_host, port=s.db_port, name=s.db_name,
                       user=s.db_user, password=s.db_password)
    reg = T.build_registry(["books_learning", "kids_animation"])
    r = reg.run("get_data_coverage", {}, T.DbToolExecutor(db, statement_timeout_ms=5000))
    assert r.ok


def test_assistant_login_cannot_write(db_settings):
    """Migration 005: yti_assistant may SELECT but never write."""
    import psycopg2
    from youtube_intel import repository as repo
    pw = os.environ.get("ASSISTANT_DB_PASSWORD")
    if not pw:
        pytest.skip("ASSISTANT_DB_PASSWORD not set")
    s = db_settings.secrets
    ro = repo.Database(host=s.db_host, port=s.db_port, name=s.db_name,
                       user=db_settings.settings.assistant.db_user, password=pw)
    with ro.connect() as conn:
        with conn.cursor() as c:
            c.execute("SELECT count(*) FROM yti.v_video_growth")
            conn.rollback()
            with pytest.raises(psycopg2.Error):
                c.execute("DELETE FROM yti.reports")
            conn.rollback()
            with pytest.raises(psycopg2.Error):
                c.execute("SELECT count(*) FROM yti.llm_calls")     # not granted
