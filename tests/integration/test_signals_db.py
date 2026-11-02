"""R-02 against a real Postgres (migration 010). Seeds inside a transaction, rolls back."""
import uuid
from datetime import datetime, timedelta, timezone

import psycopg2
import pytest

from youtube_intel import repository as repo
from youtube_intel.signals.run import compute_rows, load_videos, write_rows
from youtube_intel.studio_config import load_studio_config

pytestmark = pytest.mark.integration

CH = "UC" + "G" * 22
CFG = load_studio_config().signals


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


def seed(cur, now):
    """6 peers at 100 views/day and one video at 300 views/day, daily snapshots."""
    cur.execute("INSERT INTO yti.tracked_channels (channel_id, group_code, enabled) "
                "VALUES (%s, 'stoic', TRUE)", (CH,))
    days = 12
    runs = []
    for d in range(days + 30):
        cid, bid = str(uuid.uuid4()), str(uuid.uuid4())
        t = now - timedelta(days=d)
        cur.execute("INSERT INTO yti.collection_runs (collection_id, scheduled_for, started_at, "
                    "status) VALUES (%s, %s, %s, 'succeeded')", (cid, t, t))
        cur.execute("INSERT INTO yti.channel_batches (batch_id, collection_id, channel_id, status, "
                    "quality_status) VALUES (%s, %s, %s, 'succeeded', 'passed')", (bid, cid, CH))
        runs.append((cid, bid, t))
    vids = [(f"sigPEER{i:04d}", 30 + i, 100) for i in range(6)] + [("sigTARGET01", 10, 300)]
    for vid, age, per_day in vids:
        pub = now - timedelta(days=age)
        cur.execute("INSERT INTO yti.videos_current (video_id, channel_id, title, published_at, "
                    "duration_seconds) VALUES (%s, %s, 't', %s, 600)", (vid, CH, pub))
        for cid, bid, t in runs:
            if t > pub:
                d = (t - pub).total_seconds() / 86400
                cur.execute("INSERT INTO yti.video_observations (video_id, collection_id, batch_id, "
                            "observed_at, view_count, like_count, comment_count) "
                            "VALUES (%s, %s, %s, %s, %s, 10, 2)", (vid, cid, bid, t, int(per_day * d)))


def test_signals_are_written_with_their_raw_numbers(cur):
    cur.execute("SELECT now() AS now")
    now = cur.fetchone()["now"]
    seed(cur, now)
    videos = [v for v in load_videos(cur) if v.channel_id == CH]
    rows = compute_rows(videos, now, CFG)
    write_rows(cur, [r for r in rows if r.video.video_id == "sigTARGET01"], now.date())
    cur.execute("SELECT * FROM yti.video_signals WHERE video_id = 'sigTARGET01'")
    row = cur.fetchone()
    assert (row["s1_age_days"], float(row["s1"]), row["s1_label"], row["baseline_approx"]) == \
        (7, 3.0, "nổi trội", False)
    assert row["views"] and row["s1_views_at_age"] == 2100 and float(row["s1_baseline"]) == 700
    assert row["data_class"] == "derived" and row["fetched_at"] is not None


def test_database_refuses_an_outstanding_label_on_an_approximate_baseline(cur):
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute("""INSERT INTO yti.video_signals (signal_date, video_id, channel_id,
                           format_guess, baseline_approx, s1_label, fetched_at)
                       VALUES (current_date, 'x', %s, 'long', TRUE, 'nổi trội', now())""", (CH,))
