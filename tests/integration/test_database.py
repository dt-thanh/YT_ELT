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
