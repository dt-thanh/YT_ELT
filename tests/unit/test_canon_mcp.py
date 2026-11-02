"""C-04: the canon MCP server, called through a real MCP client (in-process)."""
import asyncio
import json

import pytest

pytest.importorskip("mcp")          # extra "mcp"; the Airflow image does not need it


HIT = {"passage_id": "m.4.3", "work_vi": "Suy tưởng", "author": "Marcus Aurelius",
       "translator": "George Long", "language": "en", "license": "public_domain",
       "ref_label": "Suy tưởng, quyển 4, đoạn 3", "snippet": "Men seek retreats...",
       "rank": 1, "score": 0.03, "similarity": 0.61, "matched_by": "both", "work_id": "m"}
PASSAGE = {"passage_id": "m.4.3", "work": "Meditations", "work_vi": "Suy tưởng",
           "author": "Marcus Aurelius", "translator": "George Long", "pub_year": 1862,
           "language": "en", "license": "public_domain", "source_url": "https://x",
           "ref": "4.3", "ref_label": "Suy tưởng, quyển 4, đoạn 3",
           "text": "Men seek retreats for themselves.", "aligned": [], "align_group": None}


def test_mcp_client_can_search_and_fetch_a_passage():
    from mcp import Client
    from studio.mcp.canon_server import build_server

    seen = {}

    def search_fn(**kw):
        seen.update(kw)
        return [HIT]

    server = build_server(search_fn, lambda pid: PASSAGE if pid == "m.4.3" else None)

    async def go():
        async with Client(server) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            hits = await client.call_tool("search_canon", {"query": "nơi ẩn náu", "k": 50})
            got = await client.call_tool("get_passage", {"passage_id": "m.4.3"})
            missing = await client.call_tool("get_passage", {"passage_id": "nope"})
            return names, hits, got, missing

    names, hits, got, missing = asyncio.run(go())
    assert names == {"search_canon", "get_passage"}
    assert seen["k"] == 10                                         # capped
    assert json.dumps(hits.structured_content, ensure_ascii=False).count("m.4.3") == 1
    assert got.structured_content["text"] == "Men seek retreats for themselves."
    assert missing.is_error
