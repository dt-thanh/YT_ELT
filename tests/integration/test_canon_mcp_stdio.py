"""C-04 acceptance: an EXTERNAL MCP client launches `python -m studio.mcp.canon_server`
as its own process over stdio and calls both tools against the loaded canon (needs the
database, the read-only login and the local bge-m3 model; no API cost)."""
import asyncio
import importlib.util
import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration
pytest.importorskip("mcp")
ROOT = Path(__file__).resolve().parents[2]


def test_external_stdio_client_searches_and_fetches_a_passage():
    from mcp import Client, StdioServerParameters
    from youtube_intel.config import load_config

    from youtube_intel import repository as repo
    try:
        s = load_config(require=("db", "assistant")).secrets
    except Exception as e:
        pytest.skip(f"database / read-only login not configured: {e}")
    if importlib.util.find_spec("sentence_transformers") is None:
        pytest.skip("bge-m3 needs the 'embeddings' extra")
    db = repo.Database(host=s.db_host, port=s.db_port, name=s.db_name,
                       user=s.db_user, password=s.db_password)
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM yti.canon_passages WHERE passage_id = "
                    "'daodejing.wangbi.1' AND embedding IS NOT NULL")
        if cur.fetchone()[0] == 0:
            pytest.skip("canon not loaded/embedded here (youtube-intel canon-load, embed-canon)")
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "TOKENIZERS_PARALLELISM": "false"}
    params = StdioServerParameters(command=sys.executable, cwd=str(ROOT), env=env,
                                   args=["-m", "studio.mcp.canon_server"])

    async def go():
        async with Client(params, read_timeout_seconds=300) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            hits = await client.call_tool("search_canon", {"query": "Đạo khả đạo phi thường đạo",
                                                           "k": 3, "pillar": "east"})
            got = await client.call_tool("get_passage", {"passage_id": "daodejing.wangbi.1"})
            return names, hits, got

    names, hits, got = asyncio.run(go())
    assert names == {"search_canon", "get_passage"}
    assert not hits.is_error and len(hits.structured_content["result"]) == 3
    p = got.structured_content
    assert p["text"].startswith("道可道") and p["license"] == "public_domain"
    assert "daodejing.legge1891.1" in [a["passage_id"] for a in p["aligned"]]
