-- =============================================================================
-- 011_comment_insights.sql  -  R-04: comment labels and viewer questions,
-- plus job_id / agent on llm_calls for the gateway (spec §12)
--
-- Spec §11 calls this 010_comment_insights (numbers shifted by one after P0-07).
-- Written ONLY when derived_metrics_enabled = true (R2).
--
-- R8: no display names are stored; the author's channel id is replaced by a keyed
-- one-way hash (HMAC-SHA256, key COMMENT_AUTHOR_KEY in .env). Comment text and the
-- paraphrased questions derived from it are 'metadata_text': deleted after 30 days
-- (R3); the label alone would be allowed 36 months, but it lives on the same row and
-- goes with the text - the conservative choice.
--
-- llm_calls.job_id / agent: the spec puts them in 015_learning_ops, but the gateway
-- that logs them arrives with comment classification, so they are added here.
--
-- Runs as the ELT user. Safe to re-run.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS yti.comment_labels (
    comment_id      TEXT PRIMARY KEY,
    video_id        TEXT NOT NULL,
    channel_id      TEXT NOT NULL,
    author_hash     TEXT,                    -- HMAC of the author's channel id; never the name
    language        TEXT,                    -- detected by a library, not an LLM
    text            TEXT NOT NULL,           -- for classification only; never quoted in a video
    like_count      INTEGER,
    published_at    TIMESTAMPTZ,
    label           TEXT CHECK (label IN ('question', 'request', 'disagreement',
                                          'personal_story', 'praise', 'spam')),
    topic_vi        TEXT,                    -- short topic in Vietnamese (paraphrase)
    label_call_id   UUID,                    -- llm_calls row that produced the label
    fetched_at      TIMESTAMPTZ NOT NULL,
    data_class      TEXT NOT NULL DEFAULT 'metadata_text' CHECK (data_class = 'metadata_text')
);

CREATE INDEX IF NOT EXISTS ix_comment_labels_video   ON yti.comment_labels (video_id);
CREATE INDEX IF NOT EXISTS ix_comment_labels_fetched ON yti.comment_labels (fetched_at);
CREATE INDEX IF NOT EXISTS ix_comment_labels_label   ON yti.comment_labels (label);

CREATE TABLE IF NOT EXISTS yti.viewer_questions (
    question_id     BIGSERIAL PRIMARY KEY,
    week_start      DATE NOT NULL,
    cluster_key     INTEGER NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('question', 'request')),
    paraphrase_vi   TEXT,                    -- restated, never verbatim (R8)
    n_comments      INTEGER NOT NULL,
    n_videos        INTEGER NOT NULL,
    languages       TEXT[] NOT NULL DEFAULT '{}',
    comment_ids     TEXT[] NOT NULL DEFAULT '{}',
    fetched_at      TIMESTAMPTZ NOT NULL,
    data_class      TEXT NOT NULL DEFAULT 'metadata_text' CHECK (data_class = 'metadata_text'),
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT viewer_questions_uniq UNIQUE (week_start, kind, cluster_key)
);

CREATE INDEX IF NOT EXISTS ix_viewer_questions_fetched ON yti.viewer_questions (fetched_at);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'yti_readonly') THEN
        GRANT SELECT ON yti.viewer_questions TO yti_readonly;
        -- Column-level: lets read-only tools map questions to channels, never read text.
        GRANT SELECT (comment_id, channel_id, video_id) ON yti.comment_labels TO yti_readonly;
    END IF;
END
$$;

-- ---- gateway bookkeeping on llm_calls ---------------------------------------
ALTER TABLE yti.llm_calls
    ADD COLUMN IF NOT EXISTS job_id UUID,
    ADD COLUMN IF NOT EXISTS agent  TEXT;
CREATE INDEX IF NOT EXISTS ix_llm_calls_job ON yti.llm_calls (job_id);
ALTER TABLE yti.llm_calls DROP CONSTRAINT IF EXISTS llm_calls_feature_check;
ALTER TABLE yti.llm_calls ADD CONSTRAINT llm_calls_feature_check
    CHECK (feature IN ('report', 'assistant', 'embedding', 'eval', 'studio'));

INSERT INTO schema_migrations (version) VALUES ('011_comment_insights')
ON CONFLICT (version) DO NOTHING;

COMMIT;
