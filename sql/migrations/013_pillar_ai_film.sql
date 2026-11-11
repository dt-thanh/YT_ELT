-- =============================================================================
-- 013 - The third pillar is now ai_film (owner decision D9, 2026-11-11)
--
-- "Ngụ ngôn kể cho người lớn" (fable) is replaced by "Phim AI": original AI-made
-- short dramas written by the channel. Tracked AI-drama channels are studied for
-- trends only (tropes, hooks, pacing); stories are never copied (spec §5, R5).
--
-- Only CHANNEL pillars and group codes change. The canon keeps its own pillar
-- 'fable' (Aesop, migration 012): source material, not a channel pillar.
-- No row used 'fable' when this was written; if one does, this migration fails
-- loudly on the CHECK instead of relabelling data silently.
-- Safe to re-run.
-- =============================================================================

ALTER TABLE yti.tracked_channels DROP CONSTRAINT IF EXISTS tracked_channels_pillar_check;
ALTER TABLE yti.tracked_channels ADD CONSTRAINT tracked_channels_pillar_check
    CHECK (pillar IS NULL OR pillar IN ('stoic', 'east', 'ai_film'));

-- Keep in sync with `groups` in config/channels.yaml.
ALTER TABLE yti.tracked_channels DROP CONSTRAINT IF EXISTS tracked_channels_group_code_check;
ALTER TABLE yti.tracked_channels ADD CONSTRAINT tracked_channels_group_code_check
    CHECK (group_code IN ('books_learning', 'kids_animation', 'stoic', 'east', 'ai_film'));
ALTER TABLE yti.reports DROP CONSTRAINT IF EXISTS reports_group_code_check;
ALTER TABLE yti.reports ADD CONSTRAINT reports_group_code_check
    CHECK (group_code IN ('books_learning', 'kids_animation', 'stoic', 'east', 'ai_film'));
