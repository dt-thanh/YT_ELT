-- =============================================================================
-- 010_signals.sql  -  R-02/R-03: video signals S1-S3 and weekly topic clusters
--
-- Spec §11 calls this 009_signals (numbers shifted by one after P0-07).
-- Written ONLY when derived_metrics_enabled = true (R2); the code checks the flag
-- before computing anything. Every signal row keeps the raw API numbers it was
-- computed from (spec §8). data_class = 'derived': kept up to 36 months once the
-- derived-metrics use case is approved (R3, retention.py).
--
-- Runs as the ELT user. Safe to re-run.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS yti.video_signals (
    signal_date      DATE NOT NULL,
    video_id         TEXT NOT NULL,
    channel_id       TEXT NOT NULL,
    format_guess     TEXT NOT NULL CHECK (format_guess IN ('short', 'long', 'unknown')),
    duration_seconds BIGINT,
    -- raw API numbers of the latest snapshot used
    views            BIGINT,
    likes            BIGINT,
    comments         BIGINT,
    -- S1: same age, same format
    s1_age_days      SMALLINT,
    s1_views_at_age  BIGINT,
    s1_baseline      NUMERIC,
    s1_peers         SMALLINT,
    s1               NUMERIC,
    baseline_approx  BOOLEAN,
    s1_label         TEXT CHECK (s1_label IN ('rất nổi trội', 'nổi trội', 'cần xem')),
    -- S2: views per hour in hours 24-72, vs the channel's median
    s2_views_per_hour NUMERIC,
    s2_baseline      NUMERIC,
    s2_peers         SMALLINT,
    s2               NUMERIC,
    -- S3: engagement ratios
    s3_like_rate     NUMERIC,
    s3_comment_rate  NUMERIC,
    fetched_at       TIMESTAMPTZ NOT NULL,           -- the latest snapshot's observed_at
    data_class       TEXT NOT NULL DEFAULT 'derived' CHECK (data_class = 'derived'),
    computed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (signal_date, video_id),
    -- Spec §8.1: an approximate baseline is never "outstanding".
    CONSTRAINT approx_never_outstanding
        CHECK (NOT (baseline_approx AND s1_label IN ('rất nổi trội', 'nổi trội')))
);

CREATE INDEX IF NOT EXISTS ix_video_signals_channel ON yti.video_signals (channel_id, signal_date);
CREATE INDEX IF NOT EXISTS ix_video_signals_fetched ON yti.video_signals (fetched_at);

COMMENT ON TABLE yti.video_signals IS
    'Grain: one video on one day. S1-S3 with the raw numbers behind them. Only with derived_metrics_enabled.';

CREATE TABLE IF NOT EXISTS yti.topic_clusters (
    cluster_id      BIGSERIAL PRIMARY KEY,
    week_start      DATE NOT NULL,
    cluster_key     INTEGER NOT NULL,                -- label from the clustering run
    label           TEXT,                            -- short name (LLM may only name it)
    n_videos        INTEGER NOT NULL,
    n_channels      INTEGER NOT NULL,
    languages       TEXT[] NOT NULL DEFAULT '{}',
    is_rising       BOOLEAN NOT NULL DEFAULT FALSE,  -- S4
    is_gap          BOOLEAN NOT NULL DEFAULT FALSE,  -- S5
    is_saturated    BOOLEAN NOT NULL DEFAULT FALSE,  -- S6
    evidence        JSONB,
    fetched_at      TIMESTAMPTZ NOT NULL,
    data_class      TEXT NOT NULL DEFAULT 'derived' CHECK (data_class = 'derived'),
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT topic_clusters_uniq UNIQUE (week_start, cluster_key)
);

CREATE TABLE IF NOT EXISTS yti.topic_cluster_members (
    cluster_id      BIGINT NOT NULL REFERENCES yti.topic_clusters(cluster_id) ON DELETE CASCADE,
    video_id        TEXT NOT NULL,
    channel_id      TEXT NOT NULL,
    language        TEXT,
    probability     NUMERIC,
    PRIMARY KEY (cluster_id, video_id)
);

CREATE INDEX IF NOT EXISTS ix_topic_clusters_fetched ON yti.topic_clusters (fetched_at);

-- Research tools read these through the read-only login (if 005 has run).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'yti_readonly') THEN
        GRANT SELECT ON yti.video_signals, yti.topic_clusters, yti.topic_cluster_members
            TO yti_readonly;
    END IF;
END
$$;

INSERT INTO schema_migrations (version) VALUES ('010_signals')
ON CONFLICT (version) DO NOTHING;

COMMIT;
