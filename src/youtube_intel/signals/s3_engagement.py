"""
S3 - engagement (spec §8.1, derived metric): like/view and comment/view from the
latest snapshot. A high comment/view often means a debated topic or one viewers
want to ask about - the first place to read comments (when allowed).
"""

from __future__ import annotations

from dataclasses import dataclass

from youtube_intel.signals.common import Video


@dataclass(frozen=True)
class S3:
    like_rate: float | None          # None when likes are hidden
    comment_rate: float | None       # None when comments are off


def compute_s3(video: Video) -> S3 | None:
    o = video.latest
    if o is None or not o.views:
        return None
    rate = lambda n: None if n is None else round(n / o.views, 6)   # noqa: E731
    return S3(like_rate=rate(o.likes), comment_rate=rate(o.comments))
