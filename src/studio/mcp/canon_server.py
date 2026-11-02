"""
MCP server `canon` (C-04): search_canon and get_passage over the canon.

Any MCP client can use it - the Writer agent, Claude, an IDE:

    python -m studio.mcp.canon_server            # stdio transport

It reads with the read-only database login and embeds queries with the same model
that embedded the passages (bge-m3). Inputs are validated from the function
signatures; outputs are the Pydantic models of studio/schemas.py.
"""

from __future__ import annotations

from typing import Callable, Literal

from mcp.server.mcpserver import MCPServer

from studio.schemas import CanonHit, CanonPassage

SearchFn = Callable[..., list[dict]]
GetFn = Callable[[str], "dict | None"]


def build_server(search_fn: SearchFn, get_fn: GetFn) -> MCPServer:
    server = MCPServer("canon", instructions=(
        "Public-domain originals (Marcus Aurelius, Seneca, Epictetus, Laozi, Zhuangzi, "
        "Confucius, Sun Tzu, Aesop). Quote ONLY the exact `text` returned by "
        "get_passage, with its ref_label; never rewrite a quote and attribute it."))

    @server.tool(description="Find canon passages about a topic, in any language (a "
                             "Vietnamese question finds English, Latin or Chinese passages).")
    def search_canon(query: str, k: int = 5,
                     language: Literal["en", "zh", "la", "fr", "vi"] | None = None,
                     pillar: Literal["stoic", "east", "fable"] | None = None) -> list[CanonHit]:
        if not query.strip():
            raise ValueError("query is empty")
        k = max(1, min(k, 10))
        return [CanonHit(**h) for h in search_fn(query=query, k=k, language=language,
                                                 pillar=pillar)]

    @server.tool(description="The exact text of one passage with its reference, license "
                             "and aligned translations/originals.")
    def get_passage(passage_id: str) -> CanonPassage:
        row = get_fn(passage_id)
        if row is None:
            raise ValueError(f"no passage {passage_id!r}")
        return CanonPassage(**row)

    return server


def production_server() -> MCPServer:
    """Read-only DB login + bge-m3 query embeddings."""
    from canon.retrieve import get_passage, search
    from youtube_intel import repository as repo
    from youtube_intel.config import load_config
    from youtube_intel.signals.clustering import BgeM3Embedder
    from youtube_intel.studio_config import load_studio_config

    cfg = load_config(require=("db", "assistant"))
    s = cfg.secrets
    ro = repo.Database(host=s.db_host, port=s.db_port, name=s.db_name,
                       user=cfg.settings.assistant.db_user, password=s.assistant_db_password)
    embedder: list = []

    def embed(q: str):
        if not embedder:                                    # load the model on first use
            embedder.append(BgeM3Embedder(load_studio_config().topics.embedding_model))
        return embedder[0].embed([q])[0]

    def search_fn(**kw):
        vec = embed(kw["query"])
        with ro.connect() as conn:
            with repo.dict_cursor(conn) as cur:
                return search(cur, query_vec=vec, **kw)

    def get_fn(pid):
        with ro.connect() as conn:
            with repo.dict_cursor(conn) as cur:
                return get_passage(cur, pid)

    return build_server(search_fn, get_fn)


if __name__ == "__main__":
    production_server().run("stdio")
