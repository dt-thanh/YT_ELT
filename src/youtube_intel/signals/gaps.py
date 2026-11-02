"""
Topic signals S4-S6 (spec §8.2), from cluster membership and S1:

  S4 rising     : within 14 days, >= 3 different channels each with >= 1 video of
                  S1 >= 2 on a FULL baseline (baseline_approx never counts).
  S5 gap        : rising thanks to English/Chinese videos, yet fewer than 2 Vietnamese
                  videos in the cluster over the window. Priority opportunity no. 1.
  S6 saturated  : many new Vietnamese videos whose median S1 is falling (newer half
                  below older half): "late to the party".

⚠️ S4 counts distinct channels of different owners - Developer Policies III.E.2 limits
aggregating API data across content owners. Only behind derived_metrics_enabled,
and the owner decides (docs/policy_notes.md).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from youtube_intel.signals.clustering import TopicVideo
from youtube_intel.signals.common import median


@dataclass
class Cluster:
    key: int
    members: list[TopicVideo]
    is_rising: bool = False
    is_gap: bool = False
    is_saturated: bool = False
    evidence: dict = field(default_factory=dict)

    @property
    def n_channels(self) -> int:
        return len({m.channel_id for m in self.members})

    @property
    def languages(self) -> list[str]:
        return sorted({m.language for m in self.members if m.language})


def assess(labels: dict[str, int], videos: list[TopicVideo], now: datetime, cfg) -> list[Cluster]:
    by_key: dict[int, list[TopicVideo]] = defaultdict(list)
    for v in videos:
        k = labels.get(v.video_id, -1)
        if k != -1:
            by_key[k].append(v)
    out = []
    for key, members in sorted(by_key.items()):
        c = Cluster(key, members)
        recent_since = now - timedelta(days=cfg.rising_days)
        strong = [m for m in members if m.published_at >= recent_since and m.s1 is not None
                  and m.s1 >= cfg.rising_min_s1 and m.baseline_approx is False]
        channels = sorted({m.channel_id for m in strong})
        c.is_rising = len(channels) >= cfg.rising_min_channels

        vi = sorted((m for m in members if m.language == "vi"), key=lambda m: m.published_at)
        foreign_strong = [m for m in strong if m.language in ("en", "zh")]
        c.is_gap = c.is_rising and bool(foreign_strong) and len(vi) < cfg.gap_max_vi_videos

        half = len(vi) // 2
        older, newer = median(m.s1 for m in vi[:half]), median(m.s1 for m in vi[half:])
        c.is_saturated = (len(vi) >= cfg.saturation_min_vi_videos and older is not None
                          and newer is not None and newer < older)

        c.evidence = {"strong_videos": [m.video_id for m in strong],
                      "strong_channels": channels, "vi_videos": len(vi),
                      "vi_median_s1_older": older, "vi_median_s1_newer": newer}
        out.append(c)
    return out
