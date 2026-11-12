-- =============================================================================
-- 014_studio_jobs.sql  -  jobs, job_events, approvals (spec §7.1, §11; D6)
--
-- Spec §11 calls this 012_studio_jobs (numbers shifted: P0-07 took 008, D9 took 013).
--
-- One job = one video (an AI-film job = one episode). Nothing is made before the
-- owner says yes, at three gates (D6; owner, 2026-11-12):
--   1. topic   - a LIST of proposed videos; the owner picks which get made
--   2. script  - the owner reads the script; "changes" sends it back with a reason
--   3. final   - the owner watches the video; only then is it ready to publish
-- Every decision is a row in approvals: who, when, why, and the minutes the owner
-- actually worked on that step (human_effort_minutes, never waiting time).
--
-- script_sha256: the exact script the owner approved. Rendering refuses a script
-- that changed after approval (studio/jobs.py), so an edit cannot skip gate 2.
--
-- Runs as the ELT user. Safe to re-run.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS yti.jobs (
    job_id          BIGSERIAL PRIMARY KEY,
    pillar          TEXT NOT NULL CHECK (pillar IN ('stoic', 'east', 'ai_film')),
    format          TEXT NOT NULL CHECK (format IN ('short', 'long', 'film_episode')),
    title           TEXT NOT NULL,
    proposal        JSONB NOT NULL,              -- what the owner saw at gate 1
    series_id       TEXT,                        -- ai_film: content/films/<series_id>/
    episode         INTEGER CHECK (episode IS NULL OR episode >= 1),
    state           TEXT NOT NULL CHECK (state IN (
                        'planned', 'researching', 'awaiting_topic_approval', 'writing',
                        'checking', 'awaiting_script_approval', 'producing', 'qc',
                        'awaiting_final_approval', 'ready_to_publish', 'published',
                        'analyzed', 'rejected', 'failed')),
    script_path     TEXT,
    script_sha256   TEXT,
    outputs         JSONB NOT NULL DEFAULT '{}'::jsonb,   -- rendered files and their QC
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (format <> 'film_episode' OR (series_id IS NOT NULL AND episode IS NOT NULL))
);

-- one job per episode of a series (a rejected one may be proposed again)
CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_episode ON yti.jobs (series_id, episode)
    WHERE series_id IS NOT NULL AND state NOT IN ('rejected', 'failed');
CREATE INDEX IF NOT EXISTS ix_jobs_state ON yti.jobs (state);

CREATE TABLE IF NOT EXISTS yti.job_events (
    event_id        BIGSERIAL PRIMARY KEY,
    job_id          BIGINT NOT NULL REFERENCES yti.jobs (job_id) ON DELETE CASCADE,
    at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    from_state      TEXT,
    to_state        TEXT NOT NULL,
    actor           TEXT NOT NULL,               -- 'owner', an agent name, or 'cli'
    note            TEXT
);

CREATE INDEX IF NOT EXISTS ix_job_events_job ON yti.job_events (job_id, at);

CREATE TABLE IF NOT EXISTS yti.approvals (
    approval_id     BIGSERIAL PRIMARY KEY,
    job_id          BIGINT NOT NULL REFERENCES yti.jobs (job_id) ON DELETE CASCADE,
    gate            TEXT NOT NULL CHECK (gate IN ('topic', 'script', 'final')),
    decision        TEXT NOT NULL CHECK (decision IN ('approved', 'changes_requested', 'rejected')),
    reason          TEXT,
    reviewer        TEXT NOT NULL,
    human_effort_minutes NUMERIC(6, 1) CHECK (human_effort_minutes IS NULL OR human_effort_minutes >= 0),
    script_sha256   TEXT,                        -- gate 2: the script this decision is about
    decided_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- sending back or rejecting without saying why helps nobody
    CHECK (decision = 'approved' OR length(btrim(coalesce(reason, ''))) > 0)
);

CREATE INDEX IF NOT EXISTS ix_approvals_job ON yti.approvals (job_id, decided_at);

INSERT INTO schema_migrations (version) VALUES ('014_studio_jobs')
ON CONFLICT (version) DO NOTHING;

COMMIT;
