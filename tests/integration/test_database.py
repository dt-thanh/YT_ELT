"""
Integration: cần Postgres THẬT. Bỏ qua bằng:  pytest -m "not integration"
"""
import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def cur():
    from youtube_intel.config import load_config
    from youtube_intel import repository as repo
    try:
        s = load_config(require=("db",)).secrets
    except Exception as e:
        pytest.skip(f"chưa cấu hình database: {e}")
    db = repo.Database(host=s.db_host, port=s.db_port, name=s.db_name,
                       user=s.db_user, password=s.db_password)
    with db.connect() as conn:
        with repo.dict_cursor(conn) as c:
            yield c


def test_all_migrations_applied(cur):
    cur.execute("SELECT version FROM schema_migrations ORDER BY version")
    got = [r["version"] for r in cur.fetchall()]
    assert got[:3] == ["001_init", "002_staging_full_columns", "003_growth_view"]


def test_grain_constraint_exists(cur):
    # ⭐ Ràng buộc định nghĩa grain + đảm bảo idempotency. Mất nó = mất tất cả.
    cur.execute("""SELECT 1 FROM pg_constraint
                   WHERE conname = 'video_observations_grain'""")
    assert cur.fetchone() is not None


def test_no_duplicate_grain(cur):
    cur.execute("""SELECT count(*) AS n FROM (
                     SELECT video_id, collection_id FROM yti.video_observations
                     GROUP BY 1,2 HAVING count(*) > 1) x""")
    assert cur.fetchone()["n"] == 0


def test_growth_view_never_hides_videos(cur):
    # VIEW dùng LEFT JOIN: mọi video đều phải có mặt, kể cả chưa đủ dữ liệu
    cur.execute("""SELECT (SELECT count(*) FROM yti.videos_current) AS a,
                          (SELECT count(*) FROM yti.v_video_growth) AS b""")
    r = cur.fetchone()
    assert r["a"] == r["b"]


def test_run_status_is_derived_from_batches(cur):
    # Mô phỏng chế độ Airflow: 2 tiến trình, CÙNG một lần thu; kênh A lỗi, kênh B
    # thành công và đóng sổ SAU CÙNG. Trước đây "ai ghi sau cùng thắng" -> lần thu
    # báo 'succeeded' (nói dối). Giờ trạng thái tính từ channel_batches -> 'partial'.
    # Chạy trong transaction của fixture rồi ROLLBACK -> không để lại dữ liệu.
    import uuid
    from datetime import datetime, timezone
    from youtube_intel import repository as repo

    cid, now = str(uuid.uuid4()), datetime.now(timezone.utc)
    try:
        repo.insert_collection_run(cur, collection_id=cid, scheduled_for=now, started_at=now)
        for ch, status in (("UC" + "A" * 22, "failed"), ("UC" + "B" * 22, "succeeded")):
            repo.ensure_channel(cur, channel_id=ch, group_code="books_learning", source_url=None)
            bid = repo.insert_channel_batch(cur, batch_id=str(uuid.uuid4()),
                                            collection_id=cid, channel_id=ch)
            repo.finish_channel_batch(cur, batch_id=bid, status=status, quality_status="pending",
                                      expected_count=None, received_count=None,
                                      request_count=None, discovery_truncated=False,
                                      manifest_key=None)
            repo.finish_collection_run(cur, collection_id=cid, status=status, ended_at=now)
        cur.execute("SELECT status FROM yti.collection_runs WHERE collection_id = %s", (cid,))
        assert cur.fetchone()["status"] == "partial"
    finally:
        cur.connection.rollback()
