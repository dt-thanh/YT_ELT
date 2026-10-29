-- =============================================================================
-- 007_rag.sql  -  chunks of video titles + descriptions, for hybrid search
--
-- Grain: one chunk of one video's text, per embedding model.
--   * embedding  : vector(1536) for text-embedding-3-small, cosine HNSW index
--   * tsv        : full-text vector over search_text (accents stripped in Python,
--                  'simple' config so Vietnamese is not stemmed as English)
-- Written by `youtube-intel embed` (src/youtube_intel/rag/indexer.py); only new
-- or changed chunks are embedded (content_hash).
--
-- Runs as the ELT user, after 006 (needs the vector extension). Safe to re-run.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS yti.video_chunks (
    chunk_id        BIGSERIAL PRIMARY KEY,
    video_id        TEXT NOT NULL
                    REFERENCES yti.videos_current(video_id) ON DELETE CASCADE,
    chunk_index     SMALLINT NOT NULL CHECK (chunk_index >= 0),
    content         TEXT NOT NULL,           -- original text, shown as a snippet
    search_text     TEXT NOT NULL,           -- lowercased, accents stripped
    tsv             tsvector GENERATED ALWAYS AS (to_tsvector('simple', search_text)) STORED,
    content_hash    TEXT NOT NULL,
    embedding       vector(1536) NOT NULL,
    embedding_model TEXT NOT NULL,
    embedded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT video_chunks_uniq UNIQUE (video_id, chunk_index, embedding_model)
);

CREATE INDEX IF NOT EXISTS ix_video_chunks_embedding
    ON yti.video_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS ix_video_chunks_tsv
    ON yti.video_chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS ix_video_chunks_video
    ON yti.video_chunks (video_id);

COMMENT ON TABLE yti.video_chunks IS
    'Grain: one chunk of a video title+description per embedding model. Public metadata only.';

-- The assistant reads this through search_videos. GRANT ... ON ALL TABLES in 005
-- only covered tables that existed then, so grant explicitly (if 005 has run).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'yti_readonly') THEN
        GRANT SELECT ON yti.video_chunks TO yti_readonly;
    END IF;
END
$$;

INSERT INTO schema_migrations (version) VALUES ('007_rag')
ON CONFLICT (version) DO NOTHING;

COMMIT;
