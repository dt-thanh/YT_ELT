"""
Candidate channels for the studio's tracking list (R-01).

The agent only PROPOSES (spec §5): search.list per configured query, then public
facts (channels.list, 50 per call) and the date of the newest upload (one
playlistItems call per channel). The owner picks the 60-90 channels and copies
them into config/channels.yaml - nothing here writes that file.

The proposal holds channel titles, i.e. public API data: it is written to
proposals/ (git-ignored) and falls under the 30-day limit of R3.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import yaml

from youtube_intel.youtube import NotFoundError


def suggest_channels(yt, queries, *, results_per_query: int, active_within_days: int,
                     known_ids=(), now: datetime | None = None) -> list[dict]:
    """Candidates from `queries` (studio_config.SearchQuery), excluding known ids."""
    now = now or datetime.now(timezone.utc)
    found: dict[str, dict] = {}
    known = set(known_ids)
    for q in queries:
        for cid in yt.search_channels(q.query, language=q.language,
                                      max_results=results_per_query):
            if cid in known:
                continue
            c = found.setdefault(cid, {"pillars": [], "languages": [], "queries": []})
            for key, value in (("pillars", q.pillar), ("languages", q.language),
                               ("queries", q.query)):
                if value not in c[key]:
                    c[key].append(value)

    cutoff = now - timedelta(days=active_within_days)
    out = []
    for info in yt.fetch_channels(list(found)):
        try:
            latest = (yt.latest_upload_at(info["uploads_playlist_id"])
                      if info.get("uploads_playlist_id") else None)
        except NotFoundError:          # empty or hidden uploads playlist
            latest = None
        latest_dt = (datetime.fromisoformat(latest.replace("Z", "+00:00")) if latest else None)
        meta = found[info["channel_id"]]
        out.append({
            "channel_id": info["channel_id"],
            "title": info["title"],
            "source_url": f"https://www.youtube.com/channel/{info['channel_id']}",
            "pillar": meta["pillars"][0],
            "language": meta["languages"][0],
            "also_matched": {"pillars": meta["pillars"][1:], "languages": meta["languages"][1:]},
            "queries": meta["queries"],
            "country": info.get("country"),
            "subscribers": info.get("subscribers"),
            "videos": info.get("videos"),
            "latest_upload": latest,
            "active": bool(latest_dt and latest_dt >= cutoff),
        })
    out.sort(key=lambda c: (c["pillar"], c["language"], not c["active"],
                            -(c["subscribers"] or 0)))
    return out


def write_proposal(candidates: list[dict], path, *, generated_at: datetime,
                   searches: int) -> None:
    """YAML for the owner to review. Inactive channels stay listed (marked)."""
    doc = {
        "generated_at": generated_at.isoformat(),
        "note": ("ĐỀ XUẤT - chưa chốt. Chọn kênh rồi chép vào config/channels.yaml với "
                 "group = pillar, language, active: true, added_by. File chứa dữ liệu API "
                 "công khai: R3 - xóa trong 30 ngày, không commit."),
        "searches_used": searches,
        "summary": _summary(candidates),
        "candidates": candidates,
    }
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(doc, f, allow_unicode=True, sort_keys=False, width=110)


def _summary(candidates: list[dict]) -> dict:
    out: dict[str, dict] = {}
    for c in candidates:
        cell = out.setdefault(c["pillar"], {}).setdefault(c["language"], {"active": 0, "inactive": 0})
        cell["active" if c["active"] else "inactive"] += 1
    return out
