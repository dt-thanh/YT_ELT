-- =============================================================================
-- 012_canon.sql  -  P2: the canon, the only source of quotations (spec §9)
--
-- Spec §11 calls this 011_canon (numbers shifted by one after P0-07).
-- Canon text is not YouTube API data: no fetched_at / data_class, no R3 limit.
-- A work can only be loaded with a license (public_domain | own) and the date its
-- year and license were verified (C-01, config/canon_sources.yaml).
--
-- Search: `search_text` is produced in Python by canon/text.py search_tokens() - the
-- SAME function tokenises queries - and indexed with the 'simple' configuration, so
-- index and query can never use different tokenisers (the RegScan bug, spec §9).
-- Chinese has no spaces: search_tokens() emits character unigrams and bigrams.
--
-- embedding: bge-m3, 1024 dimensions, NULL until `youtube-intel embed-canon` runs.
-- Runs as the ELT user, after 006 (vector extension). Safe to re-run.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS yti.canon_works (
    work_id         TEXT PRIMARY KEY,                -- e.g. meditations.long1862
    source_id       TEXT NOT NULL,                   -- entry in config/canon_sources.yaml
    work            TEXT NOT NULL,                   -- original title
    work_vi         TEXT NOT NULL,                   -- Vietnamese title used in ref_label
    author          TEXT NOT NULL,
    translator      TEXT,                            -- NULL = original language
    pub_year        SMALLINT NOT NULL,
    language        TEXT NOT NULL CHECK (language IN ('en', 'zh', 'la', 'fr', 'vi')),
    pillar          TEXT NOT NULL CHECK (pillar IN ('stoic', 'east', 'fable')),
    license         TEXT NOT NULL CHECK (license IN ('public_domain', 'own')),
    source_url      TEXT NOT NULL,
    verified_on     DATE NOT NULL,
    loaded_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS yti.canon_passages (
    passage_id      TEXT PRIMARY KEY,                -- e.g. meditations.long1862.4.3
    work_id         TEXT NOT NULL REFERENCES yti.canon_works(work_id) ON DELETE CASCADE,
    ref             TEXT NOT NULL,                   -- 4.3
    ref_label       TEXT NOT NULL,                   -- "Suy tưởng, quyển 4, đoạn 3"
    seq             INTEGER NOT NULL,                -- reading order within the work
    text            TEXT NOT NULL,
    text_norm       TEXT NOT NULL,                   -- for exact citation checks (P3)
    search_text     TEXT NOT NULL,
    tsv             tsvector GENERATED ALWAYS AS (to_tsvector('simple', search_text)) STORED,
    license         TEXT NOT NULL CHECK (license IN ('public_domain', 'own')),
    align_group     TEXT,                            -- links an original with its translation
    content_hash    TEXT NOT NULL,
    embedding       vector(1024),
    embedding_model TEXT,
    CONSTRAINT canon_passages_ref_uniq UNIQUE (work_id, ref)
);

CREATE INDEX IF NOT EXISTS ix_canon_passages_work  ON yti.canon_passages (work_id, seq);
CREATE INDEX IF NOT EXISTS ix_canon_passages_align ON yti.canon_passages (align_group);
CREATE INDEX IF NOT EXISTS ix_canon_passages_tsv   ON yti.canon_passages USING gin (tsv);
CREATE INDEX IF NOT EXISTS ix_canon_passages_vec
    ON yti.canon_passages USING hnsw (embedding vector_cosine_ops);

-- The channel's own Vietnamese translations, approved by the owner (spec §9, P3).
CREATE TABLE IF NOT EXISTS yti.canon_translations (
    translation_id  BIGSERIAL PRIMARY KEY,
    passage_id      TEXT NOT NULL REFERENCES yti.canon_passages(passage_id) ON DELETE CASCADE,
    text_vi         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'approved', 'rejected')),
    back_translation TEXT,
    judge_score     NUMERIC,
    approved_by     TEXT,
    approved_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_canon_translations_passage ON yti.canon_translations (passage_id);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'yti_readonly') THEN
        GRANT SELECT ON yti.canon_works, yti.canon_passages, yti.canon_translations
            TO yti_readonly;
    END IF;
END
$$;

INSERT INTO schema_migrations (version) VALUES ('012_canon')
ON CONFLICT (version) DO NOTHING;

COMMIT;
