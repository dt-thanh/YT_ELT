"""
R3 against a real Postgres (migration 008): for every data class, rows older than
the limit are deleted or cleared and newer rows stay; a channel's data can be
deleted on request. Each test seeds its own rows inside the fixture's transaction
and rolls back - nothing is left behind, including real rows the rules touched.

Run:  pytest tests/integration -m integration
"""
import json
import uuid

import pytest

from youtube_intel import retention as R

pytestmark = pytest.mark.integration

CH = "UC" + "R" * 22
V_OLD, V_NEW = "r3OLDvideo1", "r3NEWvideo1"
LIMITS = R.R3Limits(api_days=28, stats_days=28)
VEC = "[" + ",".join(["0.01"] * 1536) + "]"


@pytest.fixture
def cur():
    from youtube_intel import repository as repo
    from youtube_intel.config import load_config
    try:
        cfg = load_config(require=("db",))
    except Exception as e:
        pytest.skip(f"database not configured: {e}")
    s = cfg.secrets
    db = repo.Database(host=s.db_host, port=s.db_port, name=s.db_name,
                       user=s.db_user, password=s.db_password)
    with db.connect() as conn:
        with repo.dict_cursor(conn) as c:
            seed(c)
            yield c
            conn.rollback()


def seed(cur):
    ago = lambda days: f"now() - interval '{days} days'"   # noqa: E731
    cur.execute(f"""INSERT INTO yti.tracked_channels (channel_id, group_code, title, description,
                        fetched_at) VALUES (%s, 'kids_animation', 'R3 Channel', 'desc', {ago(40)})""",
                (CH,))
    cid, bid = str(uuid.uuid4()), str(uuid.uuid4())
    cur.execute("INSERT INTO yti.collection_runs (collection_id, scheduled_for, started_at, status) "
                "VALUES (%s, now(), now(), 'succeeded')", (cid,))
    cur.execute("INSERT INTO yti.channel_batches (batch_id, collection_id, channel_id, status, "
                "quality_status) VALUES (%s, %s, %s, 'succeeded', 'passed')", (bid, cid, CH))
    for vid, days in ((V_OLD, 40), (V_NEW, 1)):
        cur.execute(f"""INSERT INTO yti.videos_current (video_id, channel_id, title, published_at,
                            fetched_at) VALUES (%s, %s, %s, now(), {ago(days)})""", (vid, CH, vid))
        cur.execute(f"""INSERT INTO yti.video_observations (video_id, collection_id, batch_id,
                            observed_at, view_count, fetched_at)
                        VALUES (%s, %s, %s, {ago(days)}, 100, {ago(days)})""", (vid, cid, bid))
        cur.execute(f"""INSERT INTO yti.video_chunks (video_id, chunk_index, content, search_text,
                            content_hash, embedding, embedding_model)
                        VALUES (%s, 0, 't', 't', 'h', %s::vector, 'r3-test')""", (vid, VEC))
        cur.execute(f"""INSERT INTO yti_staging.video_observations_stg (batch_id, collection_id,
                            channel_id, video_id, title, observed_at, fetched_at)
                        VALUES (%s, %s, %s, %s, 't', {ago(days)}, {ago(days)})""",
                    (str(uuid.uuid4()), str(uuid.uuid4()), CH, vid))
        cur.execute(f"""INSERT INTO yti.reports (report_id, group_code, as_of, evidence_bundle,
                            fetched_at)
                        VALUES (%s, 'kids_animation', now() + interval '{100 + days} years',
                                %s::jsonb, {ago(days)})""",
                    (str(uuid.uuid4()), json.dumps({"candidates": [{"video_id": vid}]})))


def count(cur, sql, *params):
    cur.execute(sql, params)
    return cur.fetchone()["n"]


@pytest.mark.parametrize("table, key", [
    ("yti.video_observations", "video_id"),
    ("yti.videos_current", "video_id"),
    ("yti.video_chunks", "video_id"),
    ("yti_staging.video_observations_stg", "video_id"),
])
def test_old_rows_are_deleted_and_new_rows_stay(cur, table, key):
    R.delete_r3(cur, LIMITS)
    n = lambda vid: count(cur, f"SELECT count(*) AS n FROM {table} WHERE {key} = %s", vid)  # noqa: E731
    assert n(V_OLD) == 0 and n(V_NEW) == 1


def test_old_briefs_are_deleted(cur):
    R.delete_r3(cur, LIMITS)
    left = count(cur, "SELECT count(*) AS n FROM yti.reports WHERE evidence_bundle->'candidates' "
                      "@> %s::jsonb", json.dumps([{"video_id": V_NEW}]))
    gone = count(cur, "SELECT count(*) AS n FROM yti.reports WHERE evidence_bundle->'candidates' "
                      "@> %s::jsonb", json.dumps([{"video_id": V_OLD}]))
    assert (left, gone) == (1, 0)


def test_old_channel_metadata_is_cleared_but_the_registry_row_stays(cur):
    R.delete_r3(cur, LIMITS)
    cur.execute("SELECT title, description, fetched_at FROM yti.tracked_channels "
                "WHERE channel_id = %s", (CH,))
    assert cur.fetchone() == {"title": None, "description": None, "fetched_at": None}


def test_plan_counts_match_what_delete_removes(cur):
    planned = {p.what: p.count for p in R.plan_r3(cur, LIMITS)}
    deleted = R.delete_r3(cur, LIMITS)
    assert [planned[f"R3 {r.what} [{r.data_class}, {r.days(LIMITS)} ngày]"] for r in R.R3_RULES] \
        == [deleted[r.what] for r in R.R3_RULES]


def test_channel_data_is_deleted_on_request(cur):
    R.delete_channel_data(cur, CH)
    for table in ("yti.videos_current", "yti_staging.video_observations_stg"):
        assert count(cur, f"SELECT count(*) AS n FROM {table} WHERE channel_id = %s", CH) == 0
    for table in ("yti.video_observations", "yti.video_chunks"):
        assert count(cur, f"SELECT count(*) AS n FROM {table} WHERE video_id IN (%s, %s)",
                     V_OLD, V_NEW) == 0
    assert count(cur, "SELECT count(*) AS n FROM yti.reports WHERE evidence_bundle->'candidates' "
                      "@> %s::jsonb", json.dumps([{"video_id": V_NEW}])) == 0
    cur.execute("SELECT title, enabled FROM yti.tracked_channels WHERE channel_id = %s", (CH,))
    assert cur.fetchone() == {"title": None, "enabled": False}
