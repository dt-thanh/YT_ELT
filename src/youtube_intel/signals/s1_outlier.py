"""
S1 - above the channel's usual level (spec §8.1, derived metric).

    S1(v) = views_v(a) / median over peers(c, f) of views_u(a),   a in {2, 7, 28} days

peers(c, f): up to 20 most recent videos of the same channel and format that have a
snapshot at age a. Fewer than 5 -> the median CURRENT views of same-format videos
7-60 days old, flagged baseline_approx; such a video is at most "cần xem", never
"nổi trội" (and never counted in S4).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from youtube_intel.signals.common import Video, median, same_format_peers, views_at_age

VERY = "rất nổi trội"
OUTSTANDING = "nổi trội"
WATCH = "cần xem"


@dataclass(frozen=True)
class S1:
    age_days: int
    views_at_age: int
    baseline: float
    peers: int
    value: float
    baseline_approx: bool
    label: str | None


def label_for(value: float, baseline_approx: bool, cfg) -> str | None:
    if value < cfg.s1_outstanding:
        return None
    if baseline_approx:
        return WATCH
    return VERY if value >= cfg.s1_very_outstanding else OUTSTANDING


def compute_s1(target: Video, channel_videos, now: datetime, cfg) -> S1 | None:
    """None when the video has no snapshot at any configured age, or no baseline."""
    age = target.age_hours(now) / 24
    for a in sorted(cfg.s1_ages_days, reverse=True):          # most mature age first
        if age < a:
            continue
        mine = views_at_age(target, a, cfg.age_tolerance_hours)
        if mine is None:
            continue
        peers = same_format_peers(target, channel_videos, cfg.short_max_seconds)
        at_a = [(u, views_at_age(u, a, cfg.age_tolerance_hours)) for u in peers]
        full = [n for _, n in at_a if n is not None][:cfg.peers_max]
        if len(full) >= cfg.peers_min:
            baseline, n_peers, approx = median(full), len(full), False
        else:
            lo, hi = cfg.approx_age_days
            pool = [u.latest.views for u in peers
                    if u.latest and u.latest.views is not None
                    and lo <= u.age_hours(now) / 24 <= hi]
            baseline, n_peers, approx = median(pool), len(pool), True
        if not baseline:
            return None
        value = mine / baseline
        return S1(age_days=a, views_at_age=mine, baseline=baseline, peers=n_peers,
                  value=round(value, 4), baseline_approx=approx,
                  label=label_for(value, approx, cfg))
    return None
