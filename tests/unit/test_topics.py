"""R-03: weekly clusters and S4-S6 on sample data, with a fake embedder."""
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("sklearn")

from youtube_intel.config import load_config                                  # noqa: E402
from youtube_intel.signals.clustering import (                                 # noqa: E402
    TopicVideo, cluster_labels, pair_agreement, video_text,
)
from youtube_intel.signals.gaps import assess                                  # noqa: E402
from youtube_intel.signals.run import DerivedMetricsDisabled, run_topics       # noqa: E402
from youtube_intel.studio_config import load_studio_config                     # noqa: E402

CFG = load_studio_config().topics
NOW = datetime(2026, 11, 2, tzinfo=timezone.utc)
AXES = {"anger": (1.0, 0.0, 0.0), "tao": (0.0, 1.0, 0.0), "tortoise": (0.0, 0.0, 1.0)}


class FakeEmbedder:
    """Topic word -> its axis, plus a small per-text offset (multilingual by fiat)."""
    name = "fake"

    def __init__(self):
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        out = []
        for i, t in enumerate(texts):
            base = next(v for k, v in AXES.items() if k in t)
            out.append([x + 0.01 * ((i * 7) % 5) for x in base])
        return out


def tv(vid, topic, lang, *, channel=None, days_ago=3, s1=None, approx=False):
    return TopicVideo(vid, channel or f"UC{vid}", lang, NOW - timedelta(days=days_ago),
                      f"{topic} {lang} {vid}", s1, approx)


def sample():
    vids = []
    for topic in AXES:
        for i, lang in enumerate(["en", "en", "zh", "vi", "vi"]):
            vids.append(tv(f"{topic}{i}", topic, lang))
    return vids


def test_same_idea_in_different_languages_lands_in_one_cluster():
    labels = cluster_labels(sample(), FakeEmbedder(), min_cluster_size=3)
    pairs = [("anger0", "anger3"), ("tao2", "tao4"), ("tortoise1", "tortoise3")]
    got = pair_agreement(labels, pairs + [("anger0", "nope")])
    assert got == {"pairs": 3, "same_cluster": 3, "rate": 1.0, "missing": [["anger0", "nope"]]}
    assert labels["anger0"] != labels["tao0"] != labels["tortoise0"]


def test_too_few_videos_are_noise_without_calling_the_model():
    emb = FakeEmbedder()
    assert cluster_labels(sample()[:2], emb, min_cluster_size=3) == {"anger0": -1, "anger1": -1}
    assert emb.calls == 0


def strong(vid, lang, channel, s1=3.0, approx=False, days_ago=3):
    return tv(vid, "anger", lang, channel=channel, s1=s1, approx=approx, days_ago=days_ago)


def test_rising_needs_three_channels_with_full_baseline():
    vids = [strong("a", "en", "UC1"), strong("b", "en", "UC2"), strong("c", "zh", "UC3")]
    [c] = assess({v.video_id: 0 for v in vids}, vids, NOW, CFG)
    assert c.is_rising and c.evidence["strong_channels"] == ["UC1", "UC2", "UC3"]
    vids[2] = strong("c", "zh", "UC3", approx=True)                      # approx never counts
    [c] = assess({v.video_id: 0 for v in vids}, vids, NOW, CFG)
    assert not c.is_rising


def test_rising_ignores_videos_older_than_14_days():
    vids = [strong("a", "en", "UC1"), strong("b", "en", "UC2"),
            strong("c", "zh", "UC3", days_ago=20)]
    [c] = assess({v.video_id: 0 for v in vids}, vids, NOW, CFG)
    assert not c.is_rising


def test_gap_is_rising_abroad_with_fewer_than_two_vietnamese_videos():
    base = [strong("a", "en", "UC1"), strong("b", "en", "UC2"), strong("c", "zh", "UC3")]
    [c] = assess({v.video_id: 0 for v in base + [tv("v1", "anger", "vi")]},
                 base + [tv("v1", "anger", "vi")], NOW, CFG)
    assert c.is_gap
    two_vi = base + [tv("v1", "anger", "vi"), tv("v2", "anger", "vi")]
    [c] = assess({v.video_id: 0 for v in two_vi}, two_vi, NOW, CFG)
    assert c.is_rising and not c.is_gap


def vi_series(s1_oldest_first):
    days = [60, 50, 20, 5]
    return [tv(f"v{i}", "tao", "vi", s1=s, days_ago=d)
            for i, (s, d) in enumerate(zip(s1_oldest_first, days))]


def test_saturated_when_vietnamese_s1_keeps_falling():
    falling = vi_series([3.0, 2.5, 1.2, 0.8])
    [c] = assess({v.video_id: 0 for v in falling}, falling, NOW, CFG)
    assert c.is_saturated and c.evidence["vi_median_s1_newer"] < c.evidence["vi_median_s1_older"]
    climbing = vi_series([0.8, 1.2, 2.5, 3.0])
    [c] = assess({v.video_id: 0 for v in climbing}, climbing, NOW, CFG)
    assert not c.is_saturated


def test_video_text_is_title_plus_start_of_description():
    assert video_text("Title", "x" * 500, 10) == "Title " + "x" * 10


class NoDb:
    def connect(self):
        raise AssertionError("no database access with the flag off")


def test_flag_off_clusters_nothing():
    emb = FakeEmbedder()
    with pytest.raises(DerivedMetricsDisabled):
        run_topics(load_config(require=()), NoDb(), CFG, emb)
    assert emb.calls == 0
