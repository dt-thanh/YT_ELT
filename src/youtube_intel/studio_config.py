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
class SignalSettings:
    """Spec §8.1 thresholds (initial guesses, calibrated in A-03)."""
    window_days: int = 60
    short_max_seconds: int = 180
    s1_ages_days: tuple[int, ...] = (2, 7, 28)
    age_tolerance_hours: float = 12
    peers_max: int = 20
    peers_min: int = 5
    approx_age_days: tuple[int, int] = (7, 60)
    s1_outstanding: float = 2.0
    s1_very_outstanding: float = 5.0
    s2_window_hours: tuple[float, float] = (24, 72)
    s2_gap_hours: tuple[float, float] = (18, 30)


@dataclass(frozen=True)
class TopicSettings:
    """Spec §8.2: weekly clusters and S4-S6 (initial guesses, calibrated in A-03)."""
    embedding_model: str = "BAAI/bge-m3"
    text_chars: int = 300
    window_days: int = 90
    min_cluster_size: int = 3
    rising_days: int = 14
    rising_min_channels: int = 3
    rising_min_s1: float = 2.0
    gap_max_vi_videos: int = 2
    saturation_min_vi_videos: int = 4


@dataclass(frozen=True)
class CommentSettings:
    """Spec §8.3."""
    comment_rate_min: float = 0.005
    max_videos_per_run: int = 30
    per_video: int = 100
    refresh_days: int = 7
    classify_batch: int = 25
    min_question_cluster: int = 2


@dataclass(frozen=True)
class StudioConfig:
    channel_search: ChannelSearch
    signals: SignalSettings = SignalSettings()
    topics: TopicSettings = TopicSettings()
    comments: CommentSettings = CommentSettings()


def load_studio_config(path: Path | str | None = None) -> StudioConfig:
    path = Path(path) if path else DEFAULT_STUDIO_PATH
    if not path.exists():
        raise ConfigError(f"Không tìm thấy {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return StudioConfig(channel_search=_channel_search(raw.get("channel_search"), path),
                        signals=_signals(raw.get("signals"), path),
                        topics=_topics(raw.get("topics"), path),
                        comments=_comments(raw.get("comments"), path))


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


def _signals(block, path: Path) -> SignalSettings:
    if block is None:
        return SignalSettings()
    if not isinstance(block, dict):
        raise ConfigError(f"{path}: signals phải là mapping")
    known = {f for f in SignalSettings.__dataclass_fields__}
    unknown = set(block) - known
    if unknown:
        raise ConfigError(f"{path}: khóa lạ trong signals: {sorted(unknown)}")
    try:
        vals = {k: (tuple(v) if isinstance(v, list) else v) for k, v in block.items()}
        cfg = SignalSettings(**vals)
    except TypeError as e:
        raise ConfigError(f"{path}: signals sai: {e}") from e
    if not 1 <= cfg.peers_min <= cfg.peers_max:
        raise ConfigError(f"{path}: cần 1 <= peers_min <= peers_max")
    if not 1 < cfg.s1_outstanding < cfg.s1_very_outstanding:
        raise ConfigError(f"{path}: cần 1 < s1_outstanding < s1_very_outstanding")
    for name in ("approx_age_days", "s2_window_hours", "s2_gap_hours"):
        lo, hi = getattr(cfg, name)
        if not 0 <= lo < hi:
            raise ConfigError(f"{path}: signals.{name} phải là [thấp, cao]")
    if not cfg.s1_ages_days or sorted(cfg.s1_ages_days) != list(cfg.s1_ages_days):
        raise ConfigError(f"{path}: signals.s1_ages_days phải tăng dần, không rỗng")
    return cfg


def _topics(block, path: Path) -> TopicSettings:
    if block is None:
        return TopicSettings()
    if not isinstance(block, dict) or set(block) - set(TopicSettings.__dataclass_fields__):
        raise ConfigError(f"{path}: topics phải là mapping với khóa của TopicSettings")
    try:
        cfg = TopicSettings(**block)
    except TypeError as e:
        raise ConfigError(f"{path}: topics sai: {e}") from e
    if cfg.min_cluster_size < 2 or cfg.rising_min_channels < 1 or cfg.rising_days < 1:
        raise ConfigError(f"{path}: topics: min_cluster_size >= 2, rising_* >= 1")
    if cfg.rising_days > cfg.window_days:
        raise ConfigError(f"{path}: topics.rising_days phải <= window_days")
    return cfg


def _comments(block, path: Path) -> CommentSettings:
    if block is None:
        return CommentSettings()
    if not isinstance(block, dict) or set(block) - set(CommentSettings.__dataclass_fields__):
        raise ConfigError(f"{path}: comments phải là mapping với khóa của CommentSettings")
    cfg = CommentSettings(**block)
    if not 1 <= cfg.per_video <= 100 or not 1 <= cfg.classify_batch <= 50:
        raise ConfigError(f"{path}: comments.per_video trong 1..100, classify_batch trong 1..50")
    return cfg
