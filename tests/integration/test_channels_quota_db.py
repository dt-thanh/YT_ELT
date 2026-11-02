"""R-01 against a real Postgres (migration 009): registry sync and quota per bucket.
Seeds inside the fixture's transaction and rolls back."""
import dataclasses

import pytest

from youtube_intel import repository as repo

pytestmark = pytest.mark.integration

CH = "UC" + "Q" * 22


@pytest.fixture
def cfg():
    from youtube_intel.config import load_config
    try:
        return load_config(require=("db",))
    except Exception as e:
        pytest.skip(f"database not configured: {e}")


@pytest.fixture
def cur(cfg):
    s = cfg.secrets
    db = repo.Database(host=s.db_host, port=s.db_port, name=s.db_name,
                       user=s.db_user, password=s.db_password)
    with db.connect() as conn:
        with repo.dict_cursor(conn) as c:
            yield c
            conn.rollback()


def test_registry_sync_writes_our_decisions_not_api_metadata(cur, cfg):
    ch = dataclasses.replace(cfg.channels[0], channel_id=CH, group="stoic", pillar="stoic",
                             language="zh", enabled=True, added_by="owner")
    repo.sync_registry(cur, [ch])
    cur.execute("UPDATE yti.tracked_channels SET title = 'from API' WHERE channel_id = %s", (CH,))
    repo.sync_registry(cur, [dataclasses.replace(ch, enabled=False)])
    cur.execute("SELECT group_code, pillar, language, enabled, added_by, title "
                "FROM yti.tracked_channels WHERE channel_id = %s", (CH,))
    assert cur.fetchone() == {"group_code": "stoic", "pillar": "stoic", "language": "zh",
                              "enabled": False, "added_by": "owner", "title": "from API"}


def test_quota_today_sums_per_bucket(cur):
    before = repo.quota_today(cur)
    repo.insert_quota_usage(cur, [("channels", "default", 1), ("search", "search", 1),
                                  ("videos", "default", 1)], purpose="test", channel_id=CH)
    after = repo.quota_today(cur)
    grew = {b: after[b]["units"] - before.get(b, {"units": 0})["units"] for b in after}
    assert grew == {"default": 2, "search": 1}


def test_pillar_groups_are_allowed_and_unknown_ones_are_not(cur):
    import psycopg2
    cur.execute("INSERT INTO yti.tracked_channels (channel_id, group_code) VALUES (%s, 'east')",
                (CH,))
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute("UPDATE yti.tracked_channels SET pillar = 'history' WHERE channel_id = %s",
                    (CH,))
    cur.execute("ROLLBACK TO SAVEPOINT s")
