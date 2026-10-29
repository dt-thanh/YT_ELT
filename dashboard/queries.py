"""
Every database read and file read of the dashboard, as named functions.

app.py only calls these and draws; it computes no metric. Keeping the queries
here lets tests replace this module with a fake (see tests/unit/test_dashboard.py)
and run every screen without a database or an LLM.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from youtube_intel import repository as repo      # noqa: E402
from youtube_intel.config import load_config      # noqa: E402

EVAL_RESULTS = Path(__file__).resolve().parents[1] / "evals" / "results"


# =============================================================================
# Connection
# =============================================================================

@st.cache_resource
def config():
    """Config needing only the database (cache_resource: built once, shared)."""
    return load_config(require=("db",))


@st.cache_resource
def _db():
    s = config().secrets
    return repo.Database(host=s.db_host, port=s.db_port, name=s.db_name,
                         user=s.db_user, password=s.db_password)


@st.cache_data(ttl=60)
def q(sql: str, params: tuple = ()) -> pd.DataFrame:
    """Run SQL, return a DataFrame. Cached 60 s: data changes once a day."""
    with _db().connect() as conn:
        with repo.dict_cursor(conn) as cur:
            cur.execute(sql, params)
            return pd.DataFrame([dict(r) for r in cur.fetchall()])


# =============================================================================
# Screens 1-3 (data)
# =============================================================================

def health() -> pd.Series:
    return q("""
        SELECT (SELECT max(observed_at) FROM yti.video_observations)     AS last_obs,
               (SELECT count(*) FROM yti.video_observations)             AS n_obs,
               (SELECT count(*) FROM yti.videos_current)                 AS n_videos,
               (SELECT count(DISTINCT collection_id) FROM yti.video_observations) AS n_runs,
               (SELECT count(*) FROM yti.channel_batches
                 WHERE quality_status = 'quarantined')                   AS n_quarantined
    """).iloc[0]


def channels() -> pd.DataFrame:
    return q("""
        SELECT c.channel_id, c.title, c.group_code, c.enabled,
               count(DISTINCT v.video_id)        AS videos,
               count(o.observation_id)           AS observations,
               max(o.observed_at)                AS last_observed
          FROM yti.tracked_channels c
          LEFT JOIN yti.videos_current v USING (channel_id)
          LEFT JOIN yti.video_observations o ON o.video_id = v.video_id
         GROUP BY c.channel_id, c.title, c.group_code, c.enabled
         ORDER BY c.group_code, c.title;
    """)


def runs() -> pd.DataFrame:
    return q("""
        SELECT r.collection_id, r.scheduled_for, r.started_at, r.ended_at,
               r.status, r.code_version,
               count(b.batch_id) FILTER (WHERE b.quality_status = 'passed')      AS passed,
               count(b.batch_id) FILTER (WHERE b.quality_status = 'quarantined') AS quarantined,
               sum(b.received_count) AS rows_received
          FROM yti.collection_runs r
          LEFT JOIN yti.channel_batches b USING (collection_id)
         GROUP BY r.collection_id, r.scheduled_for, r.started_at, r.ended_at,
                  r.status, r.code_version
         ORDER BY r.started_at DESC LIMIT 15;
    """)


def growth() -> pd.DataFrame:
    return q("SELECT * FROM yti.v_video_growth;")


def growth_rankable() -> pd.DataFrame:
    return q("SELECT * FROM yti.v_video_growth WHERE is_rankable = true;")


def growth_count() -> int:
    return int(q("SELECT count(*) AS n FROM yti.v_video_growth;").iloc[0]["n"])


# =============================================================================
# Screen 4 - briefs
# =============================================================================

def reports() -> pd.DataFrame:
    """All briefs, newest first (group, date picker)."""
    return q("""
        SELECT report_id::text AS report_id, group_code, as_of, status, model_name,
               prompt_version, tokens_used, latency_ms, input_hash
          FROM yti.reports
         ORDER BY as_of DESC;
    """)


def report_detail(report_id: str) -> dict:
    df = q("""SELECT validated_payload, evidence_bundle FROM yti.reports
               WHERE report_id = %s::uuid""", (report_id,))
    return df.iloc[0].to_dict() if not df.empty else {}


def report_attempts(report_id: str) -> dict:
    """LLM calls behind one brief (session_id = report_id, migration 004)."""
    df = q("""
        SELECT count(*)                                         AS calls,
               max(attempt)                                     AS attempts,
               bool_or(fallback_used)                           AS fallback_used,
               sum(est_cost_usd)                                AS est_cost_usd,
               string_agg(validation_status, ' → ' ORDER BY attempt) AS validations
          FROM yti.llm_calls
         WHERE feature = 'report' AND session_id = %s::uuid
    """, (report_id,))
    return df.iloc[0].to_dict() if not df.empty else {}


# =============================================================================
# Screen 6 - LLM observability (all aggregation in SQL)
# =============================================================================

def llm_summary(days: int, features: tuple) -> dict:
    df = q("""
        SELECT count(*)                                                AS calls,
               sum(est_cost_usd)                                       AS est_cost_usd,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS latency_p95_ms,
               avg((status <> 'ok')::int)                              AS error_rate,
               avg(fallback_used::int)                                 AS fallback_rate,
               (count(*) FILTER (WHERE validation_status = 'failed'))::numeric
                 / NULLIF(count(*) FILTER (WHERE validation_status <> 'not_applicable'), 0)
                                                                       AS validation_fail_rate
          FROM yti.llm_calls
         WHERE called_at > now() - make_interval(days => %s)
           AND feature = ANY(%s)
    """, (days, list(features)))
    return df.iloc[0].to_dict() if not df.empty else {}


def llm_daily(days: int, features: tuple) -> pd.DataFrame:
    return q("""
        SELECT day, feature, sum(calls) AS calls,
               sum(coalesce(input_tokens, 0) + coalesce(output_tokens, 0)) AS tokens,
               sum(est_cost_usd) AS est_cost_usd
          FROM yti.v_llm_daily
         WHERE day > (now() AT TIME ZONE 'UTC')::date - %s
           AND feature = ANY(%s)
         GROUP BY day, feature
         ORDER BY day;
    """, (days, list(features)))


def llm_by_model(days: int, features: tuple) -> pd.DataFrame:
    return q("""
        SELECT model, coalesce(prompt_version, '-') AS prompt_version, feature,
               count(*) AS calls,
               sum(est_cost_usd) AS est_cost_usd,
               round(percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms)::numeric) AS p50_ms,
               round(percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)::numeric) AS p95_ms,
               round(avg((status <> 'ok')::int)::numeric, 3) AS error_rate,
               round(avg(fallback_used::int)::numeric, 3) AS fallback_rate
          FROM yti.llm_calls
         WHERE called_at > now() - make_interval(days => %s) AND feature = ANY(%s)
         GROUP BY 1, 2, 3
         ORDER BY calls DESC;
    """, (days, list(features)))


def llm_errors(days: int, features: tuple) -> pd.DataFrame:
    return q("""
        SELECT called_at, feature, model, status, error_type, validation_status,
               fallback_used, latency_ms
          FROM yti.llm_calls
         WHERE called_at > now() - make_interval(days => %s) AND feature = ANY(%s)
           AND (status <> 'ok' OR validation_status = 'failed' OR fallback_used)
         ORDER BY called_at DESC
         LIMIT 20;
    """, (days, list(features)))


@st.cache_data(ttl=60)
def eval_results() -> pd.DataFrame:
    """Latest result file per (kind, prompt/version) in evals/results/. Read-only;
    the numbers are the ones stored by the eval commands, not recomputed here."""
    rows = []
    for path in sorted(EVAL_RESULTS.glob("*.json")):
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        s = d.get("summary", {})
        if d.get("kind") == "assistant":
            rows.append({"kind": f"assistant ({d.get('split')})", "version": d.get("prompt_version"),
                         "result": f"{s.get('passed')}/{s.get('records')}",
                         "detail": f"95% CI {s.get('pass_rate_ci95')}, repeat {d.get('repeat')}",
                         "model": d.get("model"), "run_at": d.get("run_at"), "file": path.name})
        elif "embedding_model" in d and "summary" in d:
            hy = s.get("hybrid", {})
            rows.append({"kind": "rag", "version": d.get("embedding_model"),
                         "result": f"hybrid recall@5 {hy.get('recall@5')}, MRR@5 {hy.get('mrr@5')}",
                         "detail": f"{d.get('n_labeled')} labeled queries",
                         "model": d.get("embedding_model"), "run_at": d.get("run_at"), "file": path.name})
        elif "cases_passed" in s:
            rows.append({"kind": "brief", "version": d.get("prompt_version"),
                         "result": f"{s.get('cases_passed')}/{s.get('cases_total')}",
                         "detail": f"{d.get('n_cases')} synthetic cases",
                         "model": d.get("model"), "run_at": d.get("run_at"), "file": path.name})
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).sort_values("run_at")
    return df.groupby(["kind", "version"], as_index=False).last().sort_values(["kind", "version"])


# =============================================================================
# Screen 5 - assistant
# =============================================================================

@st.cache_resource
def assistant():
    """(Assistant, None) when configured, else (None, reason to show the user)."""
    try:
        cfg = load_config(require=("db", "llm", "assistant"))
    except Exception as e:                       # missing OPENAI_API_KEY / ASSISTANT_DB_PASSWORD
        return None, str(e)
    from youtube_intel.assistant.agent import build_assistant
    return build_assistant(cfg), None
