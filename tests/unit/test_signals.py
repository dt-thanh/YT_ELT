"""R-02: S1-S3 on sample data (spec §8.1). Pure functions, no database."""
import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from youtube_intel.config import load_config
from youtube_intel.signals.common import Obs, Video, format_guess, views_at_age
from youtube_intel.signals.run import DerivedMetricsDisabled, compute_rows, run_signals
from youtube_intel.signals.s1_outlier import OUTSTANDING, VERY, WATCH, compute_s1
from youtube_intel.signals.s2_velocity import compute_s2, early_views_per_hour
from youtube_intel.signals.s3_engagement import compute_s3
from youtube_intel.studio_config import load_studio_config

CFG = load_studio_config().signals
NOW = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
CH = "UCchan"


def video(vid, *, age_days, duration=600, views_at=lambda d: 100 * d, every_hours=24,
          channel=CH, likes=None, comments=None):
    """Daily snapshots from day 1 to today; views_at(age in days) gives the count."""
    pub = NOW - timedelta(days=age_days)
    obs, t = [], pub + timedelta(hours=every_hours)
    while t <= NOW:
        d = (t - pub).total_seconds() / 86400
        obs.append(Obs(t, int(views_at(d)), likes, comments))
        t += timedelta(hours=every_hours)
    return Video(vid, channel, pub, duration, tuple(obs))


def peers(n, *, age_days=40, duration=600, per_day=100):
    return [video(f"p{i}", age_days=age_days + i, duration=duration,
                  views_at=lambda d, k=per_day: k * d) for i in range(n)]


# ---- S1 -------------------------------------------------------------------------
def test_s1_compares_at_the_same_age_with_the_same_format():
    target = video("t", age_days=10, views_at=lambda d: 250 * d)        # 2.5x at day 7
    shorts = peers(6, duration=45, per_day=10_000)                      # must be ignored
    s1 = compute_s1(target, [target, *peers(6), *shorts], NOW, CFG)
    assert (s1.age_days, s1.views_at_age, s1.baseline, s1.peers) == (7, 1750, 700.0, 6)
    assert s1.value == 2.5 and s1.label == OUTSTANDING and not s1.baseline_approx


def test_s1_uses_the_most_mature_age_reached():
    target = video("t", age_days=30, views_at=lambda d: 600 * d)
    s1 = compute_s1(target, [target, *peers(6)], NOW, CFG)
    assert s1.age_days == 28 and s1.label == VERY                      # 6x


def test_approximate_baseline_is_never_outstanding():
    # Only 3 peers have a snapshot at day 7 -> baseline_approx.
    target = video("t", age_days=10, views_at=lambda d: 5000 * d)
    young = peers(3, age_days=20)
    s1 = compute_s1(target, [target, *young], NOW, CFG)
    assert s1.baseline_approx and s1.value >= CFG.s1_very_outstanding
    assert s1.label == WATCH


def test_no_snapshot_at_any_age_gives_no_s1():
    t = video("t", age_days=40)
    gappy = dataclasses.replace(t, observations=t.observations[35:])   # only days 36+
    assert compute_s1(gappy, [gappy, *peers(6)], NOW, CFG) is None


def test_views_at_age_respects_the_tolerance():
    v = video("t", age_days=10, every_hours=48)                         # days 2, 4, 6...
    assert views_at_age(v, 7, 12) is None and views_at_age(v, 6, 12) == 600


@pytest.mark.parametrize("seconds, fmt", [(45, "short"), (180, "short"), (181, "long"),
                                          (None, "unknown")])
def test_format_guess(seconds, fmt):
    assert format_guess(seconds, 180) == fmt


# ---- S2 / S3 ----------------------------------------------------------------------
def test_s2_early_velocity_against_the_channel_median():
    target = video("t", age_days=5, views_at=lambda d: 300 * d)        # 12.5 views/hour
    s2 = compute_s2(target, [target, *peers(6, per_day=96)], CFG)       # 4 views/hour
    assert (s2.views_per_hour, s2.baseline, s2.peers, s2.value) == (12.5, 4.0, 6, 3.125)


def test_s2_needs_enough_peers_and_a_valid_pair():
    target = video("t", age_days=5)
    assert compute_s2(target, [target, *peers(3)], CFG) is None
    sparse = video("s", age_days=5, every_hours=60)                      # no 18-30 h pair
    assert early_views_per_hour(sparse, CFG) is None


def test_s3_keeps_hidden_likes_as_unknown():
    s3 = compute_s3(video("t", age_days=3, views_at=lambda d: 1000, likes=None, comments=50))
    assert s3.like_rate is None and s3.comment_rate == 0.05


# ---- the run -----------------------------------------------------------------------
def test_only_recent_videos_are_scored_but_older_ones_are_peers():
    old = peers(6, age_days=100)
    rows = compute_rows([video("t", age_days=10), *old], NOW, CFG)
    assert [r.video.video_id for r in rows] == ["t"] and rows[0].s1 is not None


class NoDb:
    def connect(self):
        raise AssertionError("the database must not be touched with the flag off")


def test_flag_off_computes_and_stores_nothing():
    cfg = load_config(require=())
    assert cfg.settings.derived_metrics_enabled is False
    with pytest.raises(DerivedMetricsDisabled):
        run_signals(cfg, NoDb(), CFG)
