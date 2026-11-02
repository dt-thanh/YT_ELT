"""
Shared pieces of the video signals (spec §8.1). Pure functions over plain data:
signals are computed by deterministic code, never by an LLM, and only when
derived_metrics_enabled is true (the caller checks; see signals/run.py).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class Obs:
    observed_at: datetime
    views: int | None
    likes: int | None = None
    comments: int | None = None


@dataclass(frozen=True)
class Video:
    video_id: str
    channel_id: str
    published_at: datetime
    duration_seconds: int | None
    observations: tuple[Obs, ...]          # oldest first

    def age_hours(self, at: datetime) -> float:
        return (at - self.published_at).total_seconds() / 3600

    @property
    def latest(self) -> Obs | None:
        return self.observations[-1] if self.observations else None


def format_guess(duration_seconds: int | None, short_max_seconds: int) -> str:
    """Spec §8.1: up to 180 s is a Short. A heuristic, stored as format_guess."""
    if duration_seconds is None:
        return "unknown"
    return "short" if duration_seconds <= short_max_seconds else "long"


def views_at_age(video: Video, days: float, tolerance_hours: float) -> int | None:
    """Views from the snapshot nearest to `days` after publishing, if one lies within
    the tolerance. None when no snapshot was taken at that age."""
    target = video.published_at + timedelta(days=days)
    best = None
    for o in video.observations:
        gap = abs((o.observed_at - target).total_seconds()) / 3600
        if o.views is not None and gap <= tolerance_hours and (best is None or gap < best[0]):
            best = (gap, o.views)
    return best[1] if best else None


def median(values) -> float | None:
    values = [v for v in values if v is not None]
    return float(statistics.median(values)) if values else None


def same_format_peers(target: Video, channel_videos, short_max_seconds: int) -> list[Video]:
    """Other videos of the target's channel with the same format, most recent first.
    Shorts are never compared with long videos (spec §8.1)."""
    fmt = format_guess(target.duration_seconds, short_max_seconds)
    return sorted((v for v in channel_videos
                   if v.video_id != target.video_id and v.channel_id == target.channel_id
                   and format_guess(v.duration_seconds, short_max_seconds) == fmt),
                  key=lambda v: v.published_at, reverse=True)
