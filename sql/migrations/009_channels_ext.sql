-- =============================================================================
-- 009_channels_ext.sql  -  R-01: multilingual channels and quota per bucket
--
-- Spec §11 calls this 008_channels_ext; it is 009 because P0-07 took 008
-- (owner's decision, 2026-10-31).
--
-- 1. tracked_channels gets language (en | zh | vi), pillar (stoic | east | fable,
--    NULL for legacy channels outside the three pillars) and added_by. The spec's
--    `active` already exists as the `enabled` column; config/channels.yaml now
--    calls it `active` and the collector syncs it.
-- 2. The group CHECKs allow the three pillars as groups (a new channel's group
--    is its pillar).
-- 3. quota_usage: one row per YouTube API call, by bucket (search, upload,
--    default), so daily use per bucket can be watched (spec §8.4). Our own
--    operational data, not API data: no fetched_at / data_class.
--
-- Runs as the ELT user. Safe to re-run.
-- =============================================================================

BEGIN;

ALTER TABLE yti.tracked_channels
    ADD COLUMN IF NOT EXISTS language TEXT,
    ADD COLUMN IF NOT EXISTS pillar   TEXT,
    ADD COLUMN IF NOT EXISTS added_by TEXT;

ALTER TABLE yti.tracked_channels DROP CONSTRAINT IF EXISTS tracked_channels_language_check;
ALTER TABLE yti.tracked_channels ADD CONSTRAINT tracked_channels_language_check
    CHECK (language IS NULL OR language IN ('en', 'zh', 'vi'));
ALTER TABLE yti.tracked_channels DROP CONSTRAINT IF EXISTS tracked_channels_pillar_check;
ALTER TABLE yti.tracked_channels ADD CONSTRAINT tracked_channels_pillar_check
    CHECK (pillar IS NULL OR pillar IN ('stoic', 'east', 'fable'));

-- Groups: the two legacy groups plus the three pillars. Keep in sync with
-- `groups` in config/channels.yaml.
ALTER TABLE yti.tracked_channels DROP CONSTRAINT IF EXISTS tracked_channels_group_code_check;
ALTER TABLE yti.tracked_channels ADD CONSTRAINT tracked_channels_group_code_check
    CHECK (group_code IN ('books_learning', 'kids_animation', 'stoic', 'east', 'fable'));
ALTER TABLE yti.reports DROP CONSTRAINT IF EXISTS reports_group_code_check;
ALTER TABLE yti.reports ADD CONSTRAINT reports_group_code_check
    CHECK (group_code IN ('books_learning', 'kids_animation', 'stoic', 'east', 'fable'));

CREATE TABLE IF NOT EXISTS yti.quota_usage (
    usage_id        BIGSERIAL PRIMARY KEY,
    called_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    bucket          TEXT NOT NULL CHECK (bucket IN ('default', 'search', 'upload')),
    endpoint        TEXT NOT NULL,
    units           SMALLINT NOT NULL CHECK (units >= 0),
    purpose         TEXT NOT NULL,          -- collect | suggest_channels | verify_channels | ...
    collection_id   UUID,
    channel_id      TEXT
);

CREATE INDEX IF NOT EXISTS ix_quota_usage_called ON yti.quota_usage (called_at DESC);

COMMENT ON TABLE yti.quota_usage IS
    'Grain: one YouTube Data API call. Buckets: default 10,000 units/day, search and upload 100 calls/day.';

-- Quota days reset at midnight Pacific Time (Quota cost page).
CREATE OR REPLACE VIEW yti.v_quota_daily AS
SELECT (called_at AT TIME ZONE 'America/Los_Angeles')::date  AS quota_day,
       bucket,
       count(*)                                                AS calls,
       sum(units)                                              AS units
  FROM yti.quota_usage
 GROUP BY 1, 2;

COMMENT ON VIEW yti.v_quota_daily IS 'YouTube quota use per Pacific-time day and bucket.';

INSERT INTO schema_migrations (version) VALUES ('009_channels_ext')
ON CONFLICT (version) DO NOTHING;

COMMIT;
