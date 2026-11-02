"""R-04 against a real Postgres (migration 011). Seeds inside a transaction, rolls back."""
import pytest

from youtube_intel import repository as repo
from youtube_intel import retention as R
from youtube_intel.comments.collect import save

pytestmark = pytest.mark.integration

CH = "UC" + "C" * 22


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


def row(cid, text):
    return (cid, "vidC", CH, "hash", "vi", text, 1, "2026-11-01T00:00:00Z")


def label_of(cur, cid):
    cur.execute("SELECT label FROM yti.comment_labels WHERE comment_id = %s", (cid,))
    return cur.fetchone()["label"]


def test_an_edited_comment_loses_its_label(cur):
    save(cur, [row("cmA", "câu hỏi"), row("cmB", "khen")])
    cur.execute("UPDATE yti.comment_labels SET label = 'question' WHERE comment_id IN ('cmA','cmB')")
    save(cur, [row("cmA", "câu hỏi"), row("cmB", "khen, đã sửa")])
    assert (label_of(cur, "cmA"), label_of(cur, "cmB")) == ("question", None)


def test_comments_expire_after_the_api_limit(cur):
    save(cur, [row("cmOld", "cũ"), row("cmNew", "mới")])
    cur.execute("UPDATE yti.comment_labels SET fetched_at = now() - interval '40 days' "
                "WHERE comment_id = 'cmOld'")
    cur.execute("""INSERT INTO yti.viewer_questions (week_start, cluster_key, kind, n_comments,
                       n_videos, comment_ids, fetched_at)
                   VALUES ('2000-01-03', 0, 'question', 1, 1, '{cmOld}', now() - interval '40 days')""")
    R.delete_r3(cur, R.R3Limits(api_days=28, stats_days=28))
    cur.execute("SELECT comment_id FROM yti.comment_labels WHERE comment_id LIKE 'cm%%'")
    assert [r["comment_id"] for r in cur.fetchall()] == ["cmNew"]
    cur.execute("SELECT count(*) AS n FROM yti.viewer_questions WHERE week_start = '2000-01-03'")
    assert cur.fetchone()["n"] == 0


def test_channel_deletion_removes_its_comments_and_the_questions_built_on_them(cur):
    save(cur, [row("cmX", "hỏi")])
    cur.execute("""INSERT INTO yti.viewer_questions (week_start, cluster_key, kind, n_comments,
                       n_videos, comment_ids, fetched_at)
                   VALUES ('2000-01-10', 0, 'question', 1, 1, '{cmX}', now())""")
    R.delete_channel_data(cur, CH)
    cur.execute("SELECT count(*) AS n FROM yti.comment_labels WHERE channel_id = %s", (CH,))
    assert cur.fetchone()["n"] == 0
    cur.execute("SELECT count(*) AS n FROM yti.viewer_questions WHERE week_start = '2000-01-10'")
    assert cur.fetchone()["n"] == 0
