"""R-06: saving the market report (one row per pillar and day). Rolls back."""
from datetime import date

import pytest

from studio.agents.report import MarketReport, save_market_report
from youtube_intel import repository as repo
from youtube_intel.assistant.tools import ToolResult

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


def test_rerunning_a_day_overwrites_and_status_follows_grounding(cur):
    day = date(2999, 1, 1)
    res = [ToolResult(tool="list_channels", row_count=0)]
    ok = MarketReport("east", "# ok", "2999-01-01", False, [], results=res)
    bad = MarketReport("east", "# bad", "2999-01-01", False, ["number '9' ..."], results=res)
    first = save_market_report(cur, ok, day=day)
    second = save_market_report(cur, bad, day=day)
    cur.execute("SELECT status, validated_payload->>'markdown' AS md, data_class "
                "FROM yti.reports WHERE report_id = %s", (first,))
    assert first == second and cur.fetchone() == {"status": "draft", "md": "# bad",
                                                   "data_class": "metadata_text"}
