"""
Daily comment run (R-04): collect -> classify -> cluster questions -> viewer_questions.
Refuses to run with derived_metrics_enabled = false (R2). gateway=None collects only.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from youtube_intel.comments import classify as cls
from youtube_intel.comments import cluster as qc
from youtube_intel.comments.collect import save, select_videos, to_rows
from youtube_intel.signals.run import require_derived_metrics
from youtube_intel.youtube import CommentsDisabledError, NotFoundError

logger = logging.getLogger(__name__)


def run_comments(cfg, db, scfg, *, yt, gateway=None, embedder=None,
                 now: datetime | None = None) -> dict:
    from youtube_intel import repository as repo

    require_derived_metrics(cfg.settings)
    key = cfg.secrets.comment_author_key
    if not key:
        raise RuntimeError("COMMENT_AUTHOR_KEY is not set: author ids cannot be hashed (R8)")
    now = now or datetime.now(timezone.utc)
    stats = {"videos": 0, "comments": 0, "skipped_videos": 0, "labelled": 0, "questions": 0}

    with db.connect() as conn:
        with repo.dict_cursor(conn) as cur:
            videos = select_videos(cur, scfg)
    events_before = len(yt.meter.events)
    for v in videos:
        try:
            comments = yt.fetch_comment_threads(v["video_id"], max_results=scfg.per_video)
        except (CommentsDisabledError, NotFoundError) as e:
            stats["skipped_videos"] += 1
            logger.info("comments of %s skipped: %s", v["video_id"], type(e).__name__)
            continue
        with db.connect() as conn:
            with conn:
                with repo.dict_cursor(conn) as cur:
                    stats["comments"] += save(cur, to_rows(v, comments, key=key))
        stats["videos"] += 1
    with db.connect() as conn:
        with conn:
            with repo.dict_cursor(conn) as cur:
                repo.insert_quota_usage(cur, yt.meter.events[events_before:], purpose="comments")

    if gateway is None:
        return stats
    job_id = str(uuid.uuid4())                       # one budget for this run
    stats["job_id"] = job_id
    with db.connect() as conn:
        with repo.dict_cursor(conn) as cur:
            cur.execute("SELECT comment_id, language, text FROM yti.comment_labels "
                        "WHERE label IS NULL ORDER BY fetched_at DESC LIMIT 1000")
            todo = [dict(r) for r in cur.fetchall()]
    labels = cls.classify(gateway, todo, job_id=job_id, batch_size=scfg.classify_batch)
    with db.connect() as conn:
        with conn:
            with repo.dict_cursor(conn) as cur:
                for cid, (label, topic, call_id) in labels.items():
                    cur.execute("UPDATE yti.comment_labels SET label = %s, topic_vi = %s, "
                                "label_call_id = %s WHERE comment_id = %s",
                                (label, topic, call_id, cid))
    stats["labelled"] = len(labels)

    if embedder is not None:
        stats["questions"] = _questions(db, gateway, embedder, scfg, job_id=job_id, now=now)
    return stats


def _questions(db, gateway, embedder, scfg, *, job_id: str, now: datetime) -> int:
    from youtube_intel import repository as repo

    week_start = (now - timedelta(days=now.weekday())).date()
    with db.connect() as conn:
        with repo.dict_cursor(conn) as cur:
            cur.execute("""SELECT comment_id, video_id, channel_id, language, text, label,
                                  published_at, fetched_at
                             FROM yti.comment_labels
                            WHERE label IN ('question', 'request')""")
            comments = [dict(r) for r in cur.fetchall()]
    clusters = qc.group(comments, embedder, min_size=scfg.min_question_cluster)
    for c in clusters:
        c.paraphrase_vi = qc.paraphrase(gateway, c, job_id=job_id)
    with db.connect() as conn:
        with conn:
            with repo.dict_cursor(conn) as cur:
                cur.execute("DELETE FROM yti.viewer_questions WHERE week_start = %s", (week_start,))
                for c in clusters:
                    cur.execute(
                        """INSERT INTO yti.viewer_questions (week_start, cluster_key, kind,
                               paraphrase_vi, n_comments, n_videos, languages, comment_ids,
                               fetched_at)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (week_start, c.key, c.kind, c.paraphrase_vi, len(c.comments),
                         c.n_videos, c.languages, [x["comment_id"] for x in c.comments],
                         min(x["fetched_at"] for x in c.comments)))
    return len(clusters)
