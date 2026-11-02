"""
Collect public comments of outstanding videos (spec §8.3, R8).

Only videos labelled "nổi trội"/"rất nổi trội" (S1 on a full baseline) or with a high
comment/view are read, to save quota and LLM cost. Display names are never kept;
the author's channel id becomes a keyed one-way hash, so the same viewer can be
counted once without being identifiable.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import unicodedata

from youtube_intel.signals.s1_outlier import OUTSTANDING, VERY

_HAN = re.compile(r"[㐀-䶿一-鿿]")
_VI_ONLY = re.compile(r"[ăâđêôơưạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹ]", re.I)


def author_hash(channel_id: str | None, key: str) -> str | None:
    """HMAC-SHA256 with a secret key: one-way, and not reversible by hashing a list of
    known channel ids (which a plain hash would allow)."""
    if not channel_id:
        return None
    return hmac.new(key.encode(), channel_id.encode(), hashlib.sha256).hexdigest()[:32]


def detect_language(text: str) -> str:
    """'zh' | 'vi' | 'en' | other ISO code | 'und'. Script first (Han characters and
    Vietnamese-only letters are certain), then the langdetect library - no LLM."""
    t = unicodedata.normalize("NFC", text or "")
    if _HAN.search(t):
        return "zh"
    if _VI_ONLY.search(t):
        return "vi"
    if len(re.findall(r"\w+", t)) < 3:
        return "und"
    try:
        from langdetect import DetectorFactory, detect
        DetectorFactory.seed = 0                    # deterministic
        code = detect(t)
    except Exception:
        return "und"
    return "zh" if code.startswith("zh") else code


def select_videos(cur, cfg) -> list[dict]:
    """Outstanding videos from the latest signals, not read in the last refresh_days."""
    cur.execute(
        """
        WITH latest AS (
            SELECT DISTINCT ON (video_id) video_id, channel_id, s1, s1_label, s3_comment_rate
              FROM yti.video_signals ORDER BY video_id, signal_date DESC)
        SELECT l.video_id, l.channel_id
          FROM latest l
         WHERE (l.s1_label IN (%(very)s, %(out)s) OR l.s3_comment_rate >= %(rate)s)
           AND NOT EXISTS (SELECT 1 FROM yti.comment_labels c
                            WHERE c.video_id = l.video_id
                              AND c.fetched_at > now() - make_interval(days => %(days)s))
         ORDER BY l.s1 DESC NULLS LAST
         LIMIT %(n)s
        """,
        {"very": VERY, "out": OUTSTANDING, "rate": cfg.comment_rate_min,
         "days": cfg.refresh_days, "n": cfg.max_videos_per_run},
    )
    return [dict(r) for r in cur.fetchall()]


def to_rows(video: dict, comments: list[dict], *, key: str) -> list[tuple]:
    """Rows for yti.comment_labels; label stays NULL until classified."""
    return [(c["comment_id"], video["video_id"], video["channel_id"],
             author_hash(c.get("author_channel_id"), key), detect_language(c["text"]),
             c["text"], c.get("like_count"), c.get("published_at"))
            for c in comments if c.get("comment_id") and c.get("text")]


def save(cur, rows: list[tuple]) -> int:
    from psycopg2.extras import execute_values
    if not rows:
        return 0
    execute_values(
        cur,
        """INSERT INTO yti.comment_labels (comment_id, video_id, channel_id, author_hash,
               language, text, like_count, published_at, fetched_at)
           VALUES %s
           ON CONFLICT (comment_id) DO UPDATE SET
               -- an edited comment must be classified again
               label    = CASE WHEN EXCLUDED.text = yti.comment_labels.text
                               THEN yti.comment_labels.label END,
               topic_vi = CASE WHEN EXCLUDED.text = yti.comment_labels.text
                               THEN yti.comment_labels.topic_vi END,
               text = EXCLUDED.text, like_count = EXCLUDED.like_count, fetched_at = now()""",
        rows, template="(%s, %s, %s, %s, %s, %s, %s, %s, now())",
    )
    return len(rows)
