-- Plan 026 D2: reviewer independence on the native review gate.
--
-- `agent_sessions.model` is the session's active model, as reported by the
-- harness's own hook input (Codex `model`, agy `modelName`, Hermes
-- `extra.model`, Claude Code SessionStart `model`). Write paths resolve the
-- calling session and stamp that model, and its lineage, into each authored
-- work-item op.
--
-- `model_lineages` maps a model identifier to a coarse vendor lineage (D2.2).
-- It is a vocabulary in data (AGENTS.md decision 5): add a row to teach the
-- gate a new model family without a release. `pattern` is a case-insensitive
-- POSIX regex matched against the lower-cased model string; the lowest
-- `sort_order` match wins. The lineage is only ever derived from a model
-- string; it is never declared separately.
--
-- Idempotent: safe to re-run.

ALTER TABLE agent_sessions ADD COLUMN IF NOT EXISTS model TEXT;

-- The item's highest op lamport when a delegated review was requested (D2.7).
-- An item content op with a higher lamport makes the attempt stale. Lamports
-- order ops by write, unlike created_at (transaction start).
ALTER TABLE review_delegation_attempts ADD COLUMN IF NOT EXISTS item_lamport BIGINT;

CREATE TABLE IF NOT EXISTS model_lineages (
    pattern     TEXT PRIMARY KEY,
    lineage     TEXT NOT NULL,
    sort_order  INTEGER NOT NULL DEFAULT 100
);

INSERT INTO model_lineages (pattern, lineage, sort_order) VALUES
    ('(^|/)(claude|fable|mythos|anthropic)', 'anthropic', 10),
    ('(^|/)(gpt|chatgpt|codex|openai|o[0-9])', 'openai', 20),
    ('(^|/)(gemini|google)', 'google', 30),
    ('(^|/)(glm|zai|z-ai|zhipu)', 'zhipu', 40),
    ('(^|/)(kimi|moonshot)', 'moonshot', 50),
    ('(^|/)minimax', 'minimax', 60),
    ('(^|/)(nemotron|nvidia)', 'nvidia', 70),
    ('(^|/)deepseek', 'deepseek', 80),
    ('(^|/)qwen', 'qwen', 90),
    ('(^|/)longcat', 'meituan', 100)
ON CONFLICT (pattern) DO NOTHING;
