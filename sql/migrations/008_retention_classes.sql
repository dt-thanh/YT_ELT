-- =============================================================================
-- 008_retention_classes.sql  -  R3: every table holding YouTube API data says
-- WHEN that data was fetched (fetched_at) and WHAT kind of data it is (data_class).
--
-- youtube_intel.retention deletes or clears rows by these two columns
-- (docs/policy_notes.md, Developer Policies III.E.4):
--
--   data_class            what                                  basic mode   approved
--   raw_snapshot          raw JSON on MinIO (not a table)       30 days      30 days
--   stats_non_authorized  views/likes/comments of channels we   30 days      36 months
--                         track with an API key
--   metadata_text         titles, descriptions, channel names,  30 days      30 days
--                         anything mixing text with stats
--   derived               embeddings etc. computed from API     follows the source text
--                         data
--   authorized            our own channel via OAuth (later)     re-check access every 30 days
--   non_api               not from the API                      no limit
--
-- Existing rows are backfilled from the timestamp that already meant "fetched":
--   tracked_channels.metadata_refreshed_at, videos_current.refreshed_at,
--   video_observations.observed_at, video_observations_stg.observed_at,
--   video_chunks.embedded_at, reports.as_of
-- tracked_channels.fetched_at stays NULL for registry rows with no API data yet.
--
-- Numbered 008 by decision of 2026-10-31: the spec's later migrations
-- (008_channels_ext ... 015_learning_ops) move up by one.
-- Runs as the ELT user. Safe to re-run.
-- =============================================================================

BEGIN;

-- ---- tracked_channels: registry (ours) + channel metadata (API) ---------------
ALTER TABLE yti.tracked_channels
    ADD COLUMN IF NOT EXISTS fetched_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS data_class TEXT NOT NULL DEFAULT 'metadata_text';
UPDATE yti.tracked_channels SET fetched_at = metadata_refreshed_at
 WHERE fetched_at IS NULL AND metadata_refreshed_at IS NOT NULL;

-- ---- videos_current: video metadata ------------------------------------------
ALTER TABLE yti.videos_current
    ADD COLUMN IF NOT EXISTS fetched_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS data_class TEXT NOT NULL DEFAULT 'metadata_text';
UPDATE yti.videos_current SET fetched_at = refreshed_at WHERE fetched_at IS NULL;
ALTER TABLE yti.videos_current
    ALTER COLUMN fetched_at SET DEFAULT now(),
    ALTER COLUMN fetched_at SET NOT NULL;

-- ---- video_observations: public statistics ------------------------------------
ALTER TABLE yti.video_observations
    ADD COLUMN IF NOT EXISTS fetched_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS data_class TEXT NOT NULL DEFAULT 'stats_non_authorized';
UPDATE yti.video_observations SET fetched_at = observed_at WHERE fetched_at IS NULL;
ALTER TABLE yti.video_observations
    ALTER COLUMN fetched_at SET DEFAULT now(),
    ALTER COLUMN fetched_at SET NOT NULL;

-- ---- staging: titles, descriptions and statistics together -> strictest class --
ALTER TABLE yti_staging.video_observations_stg
    ADD COLUMN IF NOT EXISTS fetched_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS data_class TEXT NOT NULL DEFAULT 'metadata_text';
UPDATE yti_staging.video_observations_stg SET fetched_at = observed_at WHERE fetched_at IS NULL;
ALTER TABLE yti_staging.video_observations_stg
    ALTER COLUMN fetched_at SET DEFAULT now(),
    ALTER COLUMN fetched_at SET NOT NULL;

-- ---- video_chunks: embeddings of titles/descriptions ---------------------------
ALTER TABLE yti.video_chunks
    ADD COLUMN IF NOT EXISTS fetched_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS data_class TEXT NOT NULL DEFAULT 'derived';
UPDATE yti.video_chunks SET fetched_at = embedded_at WHERE fetched_at IS NULL;
ALTER TABLE yti.video_chunks
    ALTER COLUMN fetched_at SET DEFAULT now(),
    ALTER COLUMN fetched_at SET NOT NULL;

-- ---- reports: evidence_bundle holds titles and statistics ----------------------
ALTER TABLE yti.reports
    ADD COLUMN IF NOT EXISTS fetched_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS data_class TEXT NOT NULL DEFAULT 'metadata_text';
UPDATE yti.reports SET fetched_at = as_of WHERE fetched_at IS NULL;
ALTER TABLE yti.reports
    ALTER COLUMN fetched_at SET DEFAULT now(),
    ALTER COLUMN fetched_at SET NOT NULL;

-- ---- one vocabulary for data_class ----------------------------------------------
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['yti.tracked_channels', 'yti.videos_current',
                             'yti.video_observations', 'yti_staging.video_observations_stg',
                             'yti.video_chunks', 'yti.reports']
    LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_constraint
                        WHERE conname = 'data_class_known' AND conrelid = t::regclass) THEN
            EXECUTE format(
                'ALTER TABLE %s ADD CONSTRAINT data_class_known CHECK (data_class IN '
                '(''raw_snapshot'', ''stats_non_authorized'', ''metadata_text'', '
                '''derived'', ''authorized'', ''non_api''))', t);
        END IF;
    END LOOP;
END
$$;

CREATE INDEX IF NOT EXISTS ix_videos_current_fetched ON yti.videos_current (fetched_at);
CREATE INDEX IF NOT EXISTS ix_obs_fetched            ON yti.video_observations (fetched_at);
CREATE INDEX IF NOT EXISTS ix_obs_stg_fetched        ON yti_staging.video_observations_stg (fetched_at);
CREATE INDEX IF NOT EXISTS ix_reports_fetched        ON yti.reports (fetched_at);

INSERT INTO schema_migrations (version) VALUES ('008_retention_classes')
ON CONFLICT (version) DO NOTHING;

COMMIT;
