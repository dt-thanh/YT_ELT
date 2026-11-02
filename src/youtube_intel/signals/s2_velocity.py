"""
S2 - early velocity (spec §8.1, derived metric): views per hour during hours 24-72
after publishing, divided by the median of the same measure over the channel's
recent same-format videos. Detects a breakout earlier than S1.

A pair of snapshots counts under the rules v_video_growth uses for validity_reason
= 'ok': 18-30 hours apart, both view counts present, views not decreasing.
"""

from __future__ import annotations

from dataclasses import dataclass

from youtube_intel.signals.common import Video, median, same_format_peers


@dataclass(frozen=True)
class S2:
    views_per_hour: float
    baseline: float
    peers: int
    value: float


def early_views_per_hour(video: Video, cfg) -> float | None:
    """The first valid pair of snapshots inside the 24-72 h window, else None."""
    lo_age, hi_age = cfg.s2_window_hours
    lo_gap, hi_gap = cfg.s2_gap_hours
    obs = [o for o in video.observations if o.views is not None
           and lo_age <= (o.observed_at - video.published_at).total_seconds() / 3600 <= hi_age]
    for i, a in enumerate(obs):
        for b in obs[i + 1:]:
            hours = (b.observed_at - a.observed_at).total_seconds() / 3600
            if lo_gap <= hours <= hi_gap and b.views >= a.views:
                return (b.views - a.views) / hours
    return None


def compute_s2(target: Video, channel_videos, cfg) -> S2 | None:
    mine = early_views_per_hour(target, cfg)
    if mine is None:
        return None
    peers = same_format_peers(target, channel_videos, cfg.short_max_seconds)
    rates = [r for r in (early_views_per_hour(u, cfg) for u in peers) if r is not None]
    rates = rates[:cfg.peers_max]
    if len(rates) < cfg.peers_min:
        return None
    baseline = median(rates)
    if not baseline:
        return None
    return S2(views_per_hour=round(mine, 2), baseline=round(baseline, 2), peers=len(rates),
              value=round(mine / baseline, 4))
