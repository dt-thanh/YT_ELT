"""
config/studio.yaml - studio settings that are choices, not secrets (spec §11).
Read once into frozen dataclasses; a wrong key or value fails at start-up.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from youtube_intel.config import LANGUAGES, PILLARS, ConfigError

DEFAULT_STUDIO_PATH = Path(__file__).resolve().parents[2] / "config" / "studio.yaml"


@dataclass(frozen=True)
class SearchQuery:
    pillar: str
    language: str
    query: str


@dataclass(frozen=True)
class ChannelSearch:
    active_within_days: int
    results_per_query: int
    queries: tuple[SearchQuery, ...]


@dataclass(frozen=True)
class StudioConfig:
    channel_search: ChannelSearch


def load_studio_config(path: Path | str | None = None) -> StudioConfig:
    path = Path(path) if path else DEFAULT_STUDIO_PATH
    if not path.exists():
        raise ConfigError(f"Không tìm thấy {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return StudioConfig(channel_search=_channel_search(raw.get("channel_search"), path))


def _channel_search(block, path: Path) -> ChannelSearch:
    if not isinstance(block, dict):
        raise ConfigError(f"{path}: thiếu khối channel_search")
    queries = []
    for i, q in enumerate(block.get("queries") or []):
        where = f"{path}: channel_search.queries[{i}]"
        if not isinstance(q, dict) or set(q) != {"pillar", "language", "query"}:
            raise ConfigError(f"{where}: cần đúng ba khóa pillar, language, query")
        if q["pillar"] not in PILLARS or q["language"] not in LANGUAGES:
            raise ConfigError(f"{where}: pillar trong {PILLARS}, language trong {LANGUAGES}")
        if not str(q["query"]).strip():
            raise ConfigError(f"{where}: query rỗng")
        queries.append(SearchQuery(q["pillar"], q["language"], str(q["query"]).strip()))
    try:
        cs = ChannelSearch(active_within_days=int(block.get("active_within_days", 60)),
                           results_per_query=int(block.get("results_per_query", 25)),
                           queries=tuple(queries))
    except (TypeError, ValueError) as e:
        raise ConfigError(f"{path}: channel_search sai kiểu: {e}") from e
    if not 1 <= cs.results_per_query <= 50:
        raise ConfigError(f"{path}: channel_search.results_per_query phải trong 1..50")
    if cs.active_within_days < 1:
        raise ConfigError(f"{path}: channel_search.active_within_days phải >= 1")
    return cs
