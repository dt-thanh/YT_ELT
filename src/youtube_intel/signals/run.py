"""
Daily S1-S3 run: read snapshots, compute in Python, write yti.video_signals.

Refuses to compute anything unless derived_metrics_enabled is true (R2): with the
flag off, nothing is calculated, stored or returned (spec P0-04 / §8).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from psycopg2.extras import execute_values

from youtube_intel.signals.common import Obs, Video, format_guess
from youtube_intel.signals.s1_outlier import compute_s1
from youtube_intel.signals.s2_velocity import compute_s2
from youtube_intel.signals.s3_engagement import compute_s3

logger = logging.getLogger(__name__)


class DerivedMetricsDisabled(RuntimeError):
    """derived_metrics_enabled is false: no signal may be computed (R2)."""


def require_derived_metrics(settings) -> None:
    if not settings.derived_metrics_enabled:
        raise DerivedMetricsDisabled(
            "derived_metrics_enabled = false: S1-S6 and topic clusters are not computed (R2).")


@dataclass(frozen=True)
class SignalRow:
    video: Video
    fmt: str
    s1: object
    s2: object
    s3: object


def compute_rows(videos: list[Video], now: datetime, cfg) -> list[SignalRow]:
    """Signals for videos published within cfg.window_days; peers may be older."""
    by_channel: dict[str, list[Video]] = defaultdict(list)
    for v in videos:
        by_channel[v.channel_id].append(v)
    since = now - timedelta(days=cfg.window_days)
    rows = []
    for v in videos:
        if v.published_at < since or v.latest is None:
            continue
        mates = by_channel[v.channel_id]
        rows.append(SignalRow(v, format_guess(v.duration_seconds, cfg.short_max_seconds),
                              compute_s1(v, mates, now, cfg), compute_s2(v, mates, cfg),
                              compute_s3(v)))
    return rows


def load_videos(cur) -> list[Video]:
    """Every video of an active channel, with all its snapshots (oldest first)."""
    cur.execute(
        """
        SELECT v.video_id, v.channel_id, v.published_at, v.duration_seconds,
               o.observed_at, o.view_count, o.like_count, o.comment_count
          FROM yti.videos_current v
          JOIN yti.tracked_channels t USING (channel_id)
          JOIN yti.video_observations o USING (video_id)
         WHERE t.enabled AND COALESCE(v.live_state, 'none') = 'none'
         ORDER BY v.video_id, o.observed_at
        """
    )
    meta, obs = {}, defaultdict(list)
    for r in cur.fetchall():
        meta[r["video_id"]] = (r["channel_id"], r["published_at"], r["duration_seconds"])
        obs[r["video_id"]].append(Obs(r["observed_at"], r["view_count"], r["like_count"],
                                      r["comment_count"]))
    return [Video(vid, ch, pub, dur, tuple(obs[vid])) for vid, (ch, pub, dur) in meta.items()]


def write_rows(cur, rows: list[SignalRow], signal_date: date) -> int:
    values = []
    for r in rows:
        o = r.video.latest
        s1, s2, s3 = r.s1, r.s2, r.s3
        values.append((
            signal_date, r.video.video_id, r.video.channel_id, r.fmt, r.video.duration_seconds,
            o.views, o.likes, o.comments,
            s1 and s1.age_days, s1 and s1.views_at_age, s1 and s1.baseline, s1 and s1.peers,
            s1 and s1.value, s1 and s1.baseline_approx, s1 and s1.label,
            s2 and s2.views_per_hour, s2 and s2.baseline, s2 and s2.peers, s2 and s2.value,
            s3 and s3.like_rate, s3 and s3.comment_rate, o.observed_at,
        ))
    if not values:
        return 0
    execute_values(
        cur,
        """
        INSERT INTO yti.video_signals
            (signal_date, video_id, channel_id, format_guess, duration_seconds,
             views, likes, comments,
             s1_age_days, s1_views_at_age, s1_baseline, s1_peers, s1, baseline_approx, s1_label,
             s2_views_per_hour, s2_baseline, s2_peers, s2,
             s3_like_rate, s3_comment_rate, fetched_at)
        VALUES %s
        ON CONFLICT (signal_date, video_id) DO UPDATE SET
            format_guess = EXCLUDED.format_guess, duration_seconds = EXCLUDED.duration_seconds,
            views = EXCLUDED.views, likes = EXCLUDED.likes, comments = EXCLUDED.comments,
            s1_age_days = EXCLUDED.s1_age_days, s1_views_at_age = EXCLUDED.s1_views_at_age,
            s1_baseline = EXCLUDED.s1_baseline, s1_peers = EXCLUDED.s1_peers, s1 = EXCLUDED.s1,
            baseline_approx = EXCLUDED.baseline_approx, s1_label = EXCLUDED.s1_label,
            s2_views_per_hour = EXCLUDED.s2_views_per_hour, s2_baseline = EXCLUDED.s2_baseline,
            s2_peers = EXCLUDED.s2_peers, s2 = EXCLUDED.s2,
            s3_like_rate = EXCLUDED.s3_like_rate, s3_comment_rate = EXCLUDED.s3_comment_rate,
            fetched_at = EXCLUDED.fetched_at, computed_at = now()
        """,
        values,
    )
    return len(values)


def run_signals(cfg, db, signal_cfg, *, now: datetime | None = None) -> dict:
    """Compute and store today's S1-S3. Raises DerivedMetricsDisabled with the flag off."""
    from youtube_intel import repository as repo

    require_derived_metrics(cfg.settings)
    now = now or datetime.now(timezone.utc)
    with db.connect() as conn:
        with conn:
            with repo.dict_cursor(conn) as cur:
                rows = compute_rows(load_videos(cur), now, signal_cfg)
                n = write_rows(cur, rows, now.date())
    labels: dict[str, int] = defaultdict(int)
    for r in rows:
        if r.s1 and r.s1.label:
            labels[r.s1.label] += 1
    logger.info("signals: %d videos written, labels %s", n, dict(labels))
    return {"videos": n, "with_s1": sum(1 for r in rows if r.s1),
            "with_s2": sum(1 for r in rows if r.s2), "labels": dict(labels)}
