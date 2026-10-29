-- Plan 025 section 6: the loop-guard counter decays.
--
-- `auto_continues` was reset only by the per-prompt hooks on a human prompt. A
-- harness without one (the installed Codex plugin has no UserPromptSubmit
-- hook) only ever counted up, and after the cap the session could never be
-- woken again. `auto_continue_at` is the time of the last bump: the cap check
-- treats a counter older than AGENT_NOTES_AUTO_CONTINUE_WINDOW seconds (default
-- 1800) as 0, and the next bump then starts again at 1. A NULL value with a
-- nonzero counter (rows from before this migration) counts as lapsed.
--
-- Idempotent: safe to re-run.

ALTER TABLE agent_sessions ADD COLUMN IF NOT EXISTS auto_continue_at TIMESTAMPTZ;
