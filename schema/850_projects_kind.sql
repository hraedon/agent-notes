-- Plan 025 WI-2.1: memory scope as a property of the project row.
--
-- Memory tiers (repo / vm / global) reuse the existing memories table and its
-- per-project name uniqueness: a VM scope is a project row with slug 'vm:<id>',
-- the workspace-wide scope is the seeded 'global' row. `kind` tells listing code
-- which rows are real repositories. Every query that lists projects for a user
-- or an agent filters on kind = 'repo' (WI-2.4).
--
-- All DDL is idempotent (IF NOT EXISTS; the backfills only touch rows whose kind
-- is wrong).

ALTER TABLE projects
    ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'repo'
    CHECK (kind IN ('repo', 'vm', 'global'));

UPDATE projects SET kind = 'global' WHERE slug = 'global' AND kind <> 'global';
UPDATE projects SET kind = 'vm' WHERE slug LIKE 'vm:%' AND kind <> 'vm';

-- Synthetic rows are the rare case; the partial index finds them without
-- indexing every repo.
CREATE INDEX IF NOT EXISTS idx_projects_workspace_kind
    ON projects (workspace_id, kind)
    WHERE kind <> 'repo';
