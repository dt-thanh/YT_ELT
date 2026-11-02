"""R-05 research tools against a real Postgres (migrations 010-011). Rolls back."""
import json

import pytest

from youtube_intel import repository as repo
from youtube_intel.assistant import tools as T

pytestmark = pytest.mark.integration

CH = "UC" + "T" * 22


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
            seed(c)
            yield c
            conn.rollback()


def seed(cur):
    cur.execute("INSERT INTO yti.tracked_channels (channel_id, group_code, title, language, "
                "enabled) VALUES (%s, 'stoic', 'Stoic EN', 'en', TRUE)", (CH,))
    cur.execute("INSERT INTO yti.videos_current (video_id, channel_id, title, published_at) "
                "VALUES ('rtVideo0001', %s, 'On anger', now())", (CH,))
    cur.execute("""INSERT INTO yti.video_signals (signal_date, video_id, channel_id, format_guess,
                       views, s1_age_days, s1, baseline_approx, s1_label, fetched_at)
                   VALUES (current_date, 'rtVideo0001', %s, 'long', 9000, 7, 3.1, FALSE,
                           'nổi trội', now())""", (CH,))
    cur.execute("""INSERT INTO yti.topic_clusters (week_start, cluster_key, n_videos, n_channels,
                       languages, is_rising, is_gap, evidence, fetched_at)
                   VALUES ('2999-01-05', 1, 1, 1, '{en}', TRUE, TRUE, %s::jsonb, now())
                   RETURNING cluster_id""", (json.dumps({"strong_videos": ["rtVideo0001"]}),))
    cid = cur.fetchone()["cluster_id"]
    cur.execute("INSERT INTO yti.topic_cluster_members (cluster_id, video_id, channel_id, language) "
                "VALUES (%s, 'rtVideo0001', %s, 'en')", (cid, CH))
    cur.execute("""INSERT INTO yti.comment_labels (comment_id, video_id, channel_id, text, label,
                       fetched_at) VALUES ('rtCmt1', 'rtVideo0001', %s, 'how?', 'question', now())""",
                (CH,))
    cur.execute("""INSERT INTO yti.viewer_questions (week_start, cluster_key, kind, paraphrase_vi,
                       n_comments, n_videos, languages, comment_ids, fetched_at)
                   VALUES ('2999-01-05', 1, 'question', 'Làm sao bớt giận', 1, 1, '{en}',
                           '{rtCmt1}', now())""")


def test_get_signals_returns_raw_numbers_with_the_signal(cur):
    r = T.get_signals(cur, max_rows=50, group="stoic")
    row = next(x for x in r.rows if x["video_id"] == "rtVideo0001")
    assert (row["views"], float(row["s1"]), row["s1_label"], row["language"]) == \
        (9000, 3.1, "nổi trội", "en")


def test_get_topic_clusters_latest_week_gap_first(cur):
    r = T.get_topic_clusters(cur, max_rows=10, group="stoic", only="gap")
    [row] = r.rows
    assert row["is_gap"] and row["member_video_ids"] == ["rtVideo0001"]


def test_get_comment_insights_gives_restatements_never_text(cur):
    r = T.get_comment_insights(cur, max_rows=10, group="stoic")
    [row] = r.rows
    assert row["paraphrase_vi"] == "Làm sao bớt giận" and "text" not in row
    assert T.get_comment_insights(cur, max_rows=10, group="east").rows == ()
