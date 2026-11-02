"""R-01: multilingual channel config, quota per bucket, channel candidates."""
import json
import math
from datetime import datetime, timedelta, timezone

import pytest
import requests
import yaml

from youtube_intel.channel_search import suggest_channels, write_proposal
from youtube_intel.config import DEFAULT_CONFIG_PATH, ConfigError, QuotaSettings, load_config
from youtube_intel.studio_config import SearchQuery, load_studio_config
from youtube_intel.youtube import QuotaMeter, YouTubeClient, quota_alerts

CFG = load_config(require=())


# ---- quota buckets (spec §8.4) ------------------------------------------------
def test_search_has_its_own_bucket_and_costs_one_unit():
    m = QuotaMeter()
    for ep in ("channels", "playlistItems", "videos", "search", "videos.insert"):
        m.charge(ep)
    assert m.by_bucket() == {"default": 3, "search": 1, "upload": 1}
    assert m.estimated_units == 3                     # the shared 10,000-unit bucket only
    assert [e[1] for e in m.events] == ["default"] * 3 + ["search", "upload"]


@pytest.mark.parametrize("used, alerts", [
    ({"default": 8000}, []),                          # exactly 80 % is not "above"
    ({"default": 8001}, ["default"]),
    ({"search": 81}, ["search"]),
    ({"upload": 81, "default": 10}, ["upload"]),
])
def test_alert_above_80_percent_of_a_bucket(used, alerts):
    got = quota_alerts(used, QuotaSettings())
    assert [a.split()[2].rstrip(":") for a in got] == alerts


class CannedSession:
    """requests.Session stand-in: answers by endpoint, counts calls."""
    def __init__(self, n_videos=50):
        self.headers = {}
        self.n = n_videos

    def get(self, url, params=None, timeout=None):
        ep = url.rsplit("/", 1)[-1]
        if ep == "channels":
            body = {"items": [{"id": "UC" + "x" * 22, "snippet": {"title": "t"},
                               "statistics": {"videoCount": "500"},
                               "contentDetails": {"relatedPlaylists": {"uploads": "UUx"}}}]}
        elif ep == "playlistItems":
            body = {"pageInfo": {"totalResults": 500}, "nextPageToken": "n",
                    "items": [{"contentDetails": {"videoId": f"v{i:010d}"}} for i in range(50)]}
        else:
            ids = params["id"].split(",")
            body = {"items": [{"id": i, "snippet": {}, "statistics": {}, "contentDetails": {},
                               "status": {}} for i in ids]}
        r = requests.Response()
        r.status_code, r._content = 200, json.dumps(body).encode()
        return r

    def close(self):
        pass


def test_daily_collection_of_90_channels_stays_under_500_units():
    """R-01 acceptance: the shared bucket stays under 500 units/day with 90 channels."""
    s = CFG.settings
    yt = YouTubeClient("k")
    yt._session = CannedSession()
    info = yt.fetch_channel(channel_id="UC" + "x" * 22)
    disc = yt.discover_video_ids(info.uploads_playlist_id,
                                 limit=s.initial_video_limit_per_channel,
                                 page_limit=s.discovery_page_limit_per_channel)
    yt.fetch_video_details(disc.video_ids)
    worst = 1 + s.discovery_page_limit_per_channel + math.ceil(
        s.initial_video_limit_per_channel / 50)
    assert yt.meter.estimated_units <= worst
    assert 90 * worst * (24 // s.snapshot_hours) < 500


# ---- channel config (spec §5) ----------------------------------------------------
def test_shipped_channels_follow_spec_section_5():
    by_title = {c.title.split(" ")[0]: c for c in CFG.channels}
    assert by_title["FightMediocrity"].enabled and by_title["Sprouts"].enabled
    for kid in ("Wolfoo", "Heo"):
        c = by_title[kid]
        assert not c.enabled and c.reference_names            # inactive, names still blocked
    assert all(c.language in ("en", "zh", "vi") for c in CFG.channels)


@pytest.fixture
def cfg_with(tmp_path):
    def make(mutate):
        raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
        mutate(raw)
        p = tmp_path / "c.yaml"
        p.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
        return p
    return make


@pytest.mark.parametrize("mutate", [
    lambda c: c["channels"][0].update(active=True, enabled=True),     # both names
    lambda c: c["channels"][0].update(language="fr"),
    lambda c: c["channels"][0].update(pillar="history"),
    lambda c: c["project"].update(quota={"alert_ratio": 1.5}),
    lambda c: c["project"].update(quota={"search_calls": 100}),        # unknown key
])
def test_bad_channel_or_quota_config_fails_fast(cfg_with, mutate):
    with pytest.raises(ConfigError):
        load_config(cfg_with(mutate), require=())


def test_new_pillar_channel_loads(cfg_with):
    def add(c):
        c["channels"].append({"id": "UC" + "z" * 22, "group": "stoic", "pillar": "stoic",
                              "language": "zh", "title": "x", "active": True,
                              "added_by": "thanh"})
    c = load_config(cfg_with(add), require=()).channels[-1]
    assert (c.group, c.pillar, c.language, c.enabled, c.added_by) == \
        ("stoic", "stoic", "zh", True, "thanh")


# ---- candidate channels (only proposed, never added) ------------------------------
NOW = datetime(2026, 11, 2, tzinfo=timezone.utc)


class FakeYT:
    def __init__(self):
        self.calls = []

    def search_channels(self, query, *, language=None, max_results=50):
        self.calls.append(("search", query))
        return {"stoicism": ["UCa", "UCknown", "UCb"], "khắc kỷ": ["UCb", "UCc"]}[query]

    def fetch_channels(self, ids):
        self.calls.append(("channels", tuple(ids)))
        return [{"channel_id": i, "title": i, "subscribers": n, "videos": 10,
                 "uploads_playlist_id": "UU" + i} for i, n in zip(ids, (5000, 900, None))]

    def latest_upload_at(self, playlist_id):
        if playlist_id == "UUUCgone":
            from youtube_intel.youtube import NotFoundError
            raise NotFoundError("playlistNotFound")
        days = {"UUUCa": 3, "UUUCb": 90, "UUUCc": 59}[playlist_id]
        return (NOW - timedelta(days=days)).isoformat().replace("+00:00", "Z")


def test_suggestions_skip_known_channels_and_mark_inactive_ones():
    yt = FakeYT()
    qs = [SearchQuery("stoic", "en", "stoicism"), SearchQuery("stoic", "vi", "khắc kỷ")]
    cands = suggest_channels(yt, qs, results_per_query=25, active_within_days=60,
                             known_ids={"UCknown"}, now=NOW)
    by_id = {c["channel_id"]: c for c in cands}
    assert set(by_id) == {"UCa", "UCb", "UCc"}
    assert (by_id["UCa"]["active"], by_id["UCb"]["active"], by_id["UCc"]["active"]) == \
        (True, False, True)
    assert by_id["UCb"]["queries"] == ["stoicism", "khắc kỷ"]          # found twice, kept once
    assert by_id["UCb"]["also_matched"]["languages"] == ["vi"]
    assert sum(1 for c in yt.calls if c[0] == "search") == 2           # one search per query


def test_proposal_file_is_reviewable(tmp_path):
    cands = suggest_channels(FakeYT(), [SearchQuery("stoic", "en", "stoicism")],
                             results_per_query=25, active_within_days=60,
                             known_ids={"UCknown"}, now=NOW)
    p = tmp_path / "p.yaml"
    write_proposal(cands, p, generated_at=NOW, searches=1)
    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert "ĐỀ XUẤT" in doc["note"] and doc["summary"]["stoic"]["en"] == {"active": 1, "inactive": 1}


def test_shipped_search_queries_cover_every_pillar_and_language():
    cs = load_studio_config().channel_search
    assert {(q.pillar, q.language) for q in cs.queries} == {
        (p, lang) for p in ("stoic", "east", "fable") for lang in ("en", "zh", "vi")}
    assert len(cs.queries) <= CFG.settings.quota.search_daily_calls // 2


def test_a_channel_with_a_hidden_uploads_playlist_is_kept_as_inactive():
    yt = FakeYT()
    yt.search_channels = lambda q, **k: ["UCgone"]
    [c] = suggest_channels(yt, [SearchQuery("fable", "zh", "x")], results_per_query=25,
                           active_within_days=60, now=NOW)
    assert (c["channel_id"], c["latest_upload"], c["active"]) == ("UCgone", None, False)
