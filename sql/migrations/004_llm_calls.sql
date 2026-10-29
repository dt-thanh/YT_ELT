-- =============================================================================
-- 004_llm_calls.sql  -  LLM observability
--
-- One row per LLM or embedding call, written by youtube_intel.llm (the only
-- module allowed to call OpenAI). Rows are written in a finally block, so
-- failed calls are logged too.
--
-- Run:
--   docker exec -i postgres psql -U <user> -d elt_db < sql/migrations/004_llm_calls.sql
-- Safe to re-run (IF NOT EXISTS / CREATE OR REPLACE / ON CONFLICT DO NOTHING).
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS yti.llm_calls (
    call_id           UUID PRIMARY KEY,
    called_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    feature           TEXT NOT NULL
                      CHECK (feature IN ('report', 'assistant', 'embedding', 'eval')),
    -- Groups the calls of one question / one report (report: session_id = report_id).
    session_id        UUID,
    turn_id           UUID,

    model             TEXT NOT NULL,         -- model returned by the API, else the one requested
    prompt_version    TEXT,

    -- NULL when the API did not report usage (e.g. the call failed). Never 0.
    input_tokens      INTEGER CHECK (input_tokens  IS NULL OR input_tokens  >= 0),
    output_tokens     INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
    -- NULL when the model has no entry in llm_pricing (config/channels.yaml).
    est_cost_usd      NUMERIC(12, 6) CHECK (est_cost_usd IS NULL OR est_cost_usd >= 0),
    latency_ms        INTEGER NOT NULL CHECK (latency_ms >= 0),

    status            TEXT NOT NULL
                      CHECK (status IN ('ok', 'error', 'timeout', 'refused')),
    error_type        TEXT,

    -- Set by the caller after it has checked the output (sink.mark()).
    validation_status TEXT NOT NULL DEFAULT 'not_applicable'
                      CHECK (validation_status IN ('passed', 'repaired', 'failed', 'not_applicable')),
    fallback_used     BOOLEAN NOT NULL DEFAULT FALSE,

    tool_call_count   SMALLINT NOT NULL DEFAULT 0 CHECK (tool_call_count >= 0),
    attempt           SMALLINT NOT NULL DEFAULT 1 CHECK (attempt >= 1),

    -- Truncated SHA-256 of the request. The prompt text itself is not stored.
    input_hash        TEXT
);

CREATE INDEX IF NOT EXISTS ix_llm_calls_called_at ON yti.llm_calls (called_at DESC);
CREATE INDEX IF NOT EXISTS ix_llm_calls_feature   ON yti.llm_calls (feature, called_at DESC);
CREATE INDEX IF NOT EXISTS ix_llm_calls_session   ON yti.llm_calls (session_id);

COMMENT ON TABLE yti.llm_calls IS
    'Grain: one LLM or embedding call. Written by youtube_intel.llm.LLMClient, including failed calls.';


-- Daily rollup for the dashboard and `youtube-intel llm-stats`. Days are UTC.
CREATE OR REPLACE VIEW yti.v_llm_daily AS
SELECT
    (called_at AT TIME ZONE 'UTC')::date                         AS day,
    feature,
    model,
    count(*)                                                     AS calls,
    sum(input_tokens)                                            AS input_tokens,
    sum(output_tokens)                                           AS output_tokens,
    sum(est_cost_usd)                                            AS est_cost_usd,
    -- Calls with no known price: the cost total above is a lower bound.
    count(*) FILTER (WHERE est_cost_usd IS NULL)                 AS calls_without_cost,
    percentile_cont(0.5)  WITHIN GROUP (ORDER BY latency_ms)     AS latency_p50_ms,
    percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)     AS latency_p95_ms,
    avg((status <> 'ok')::int)::numeric(5, 4)                    AS error_rate,
    -- Share of VALIDATED calls that failed validation (not_applicable excluded).
    (count(*) FILTER (WHERE validation_status = 'failed'))::numeric
        / NULLIF(count(*) FILTER (WHERE validation_status <> 'not_applicable'), 0)
                                                                 AS validation_fail_rate,
    avg(fallback_used::int)::numeric(5, 4)                       AS fallback_rate
FROM yti.llm_calls
GROUP BY 1, 2, 3;

COMMENT ON VIEW yti.v_llm_daily IS
    'Daily LLM usage per feature x model (UTC days): calls, tokens, cost, p50/p95 latency, error/validation/fallback rates.';

INSERT INTO schema_migrations (version) VALUES ('004_llm_calls')
ON CONFLICT (version) DO NOTHING;

COMMIT;
