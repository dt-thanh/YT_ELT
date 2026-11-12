"""The three gates against a real Postgres (migration 014): a list of proposals, the owner
picks, a script is pinned by its hash, nothing renders unapproved. Rolls back."""
import psycopg2
import pytest

from studio import jobs
from youtube_intel import repository as repo

pytestmark = pytest.mark.integration


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


def proposal(ep):
    return jobs.Proposal(pillar="ai_film", format="film_episode", title=f"Tập thử {ep}",
                         idea="Một tập thử cho bài kiểm tra.", why="Chỉ để kiểm tra điểm duyệt.",
                         series_id="zztest_series", episode=ep)


def test_the_owner_picks_from_the_list_and_nothing_renders_unapproved(cur, tmp_path):
    keep, drop = jobs.propose(cur, [proposal(1), proposal(2)], "research")
    waiting = [j["job_id"] for j in jobs.list_jobs(cur, ["awaiting_topic_approval"])]
    assert {keep, drop} <= set(waiting)

    with pytest.raises(jobs.JobError, match="say why"):
        jobs.record_decision(cur, drop, "rejected", reviewer="owner")
    assert jobs.record_decision(cur, drop, "rejected", reviewer="owner", reason="trùng ý") == "rejected"
    assert jobs.record_decision(cur, keep, "approved", reviewer="owner", minutes=2) == "writing"

    script = tmp_path / "ep01.yaml"
    script.write_text("v1", encoding="utf-8")
    jobs.move(cur, keep, "checking", "writer", script_path=str(script),
              script_sha256=jobs.sha256_file(script))
    jobs.move(cur, keep, "awaiting_script_approval", "checker")
    with pytest.raises(jobs.JobError, match="waiting for your approval"):
        jobs.require_renderable(jobs.get(cur, keep))

    # the owner asks for changes; the writer sees why, fixes, resubmits
    jobs.record_decision(cur, keep, "changes_requested", reviewer="owner", reason="hook yếu quá")
    _, approvals = jobs.history(cur, keep)
    assert jobs.open_feedback(approvals) == "hook yếu quá"
    script.write_text("v2", encoding="utf-8")
    jobs.move(cur, keep, "checking", "writer", script_path=str(script),
              script_sha256=jobs.sha256_file(script))
    jobs.move(cur, keep, "awaiting_script_approval", "checker")

    # an edit after submitting cannot be approved, an edit after approving cannot render
    script.write_text("v2 sneaky", encoding="utf-8")
    with pytest.raises(jobs.JobError, match="changed after it was submitted"):
        jobs.record_decision(cur, keep, "approved", reviewer="owner")
    script.write_text("v2", encoding="utf-8")
    assert jobs.record_decision(cur, keep, "approved", reviewer="owner") == "producing"
    jobs.require_renderable(jobs.get(cur, keep))
    script.write_text("v3", encoding="utf-8")
    with pytest.raises(jobs.JobError, match="changed after you approved"):
        jobs.require_renderable(jobs.get(cur, keep))
    script.write_text("v2", encoding="utf-8")

    jobs.move(cur, keep, "qc", "production", outputs={"short": {"path": "x.mp4", "seconds": 40.0,
                                                               "qc_ok": True}})
    jobs.move(cur, keep, "awaiting_final_approval", "production")
    with pytest.raises(jobs.JobError):
        jobs.move(cur, keep, "published", "production")          # only the owner moves it on
    assert jobs.record_decision(cur, keep, "approved", reviewer="owner") == "ready_to_publish"

    events, approvals = jobs.history(cur, keep)
    assert [a["gate"] for a in approvals] == ["topic", "script", "script", "final"]
    assert events[-1]["to_state"] == "ready_to_publish"
    assert jobs.get(cur, keep)["outputs"]["short"]["path"] == "x.mp4"


def test_the_database_itself_refuses_a_reasonless_no_and_a_second_live_job(cur):
    (job,) = jobs.propose(cur, [proposal(7)], "research")
    with pytest.raises(psycopg2.errors.UniqueViolation):
        jobs.propose(cur, [proposal(7)], "research")
    cur.connection.rollback()
    (job,) = jobs.propose(cur, [proposal(7)], "research")
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute("INSERT INTO yti.approvals (job_id, gate, decision, reason, reviewer) "
                    "VALUES (%s, 'topic', 'rejected', '  ', 'owner')", (job,))
