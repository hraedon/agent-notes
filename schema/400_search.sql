-- Cross-kind search infrastructure (Phase 4 / §6).
-- Run via: agent-notes-migrate --all  (decision 18).
--
-- Design notes:
-- - all_notes_search_v is explicitly search-only (Kimi D); kind-specific fields
--   are fetched via kind tools.
-- - Uses updated_at consistently for the since filter (both breadcrumbs and
--   memories have this column).
-- - Memories filtered to active rows only.
-- - Breadcrumbs need a JOIN to projects for workspace_id.

CREATE OR REPLACE VIEW all_notes_search_v AS
SELECT
    'breadcrumb'::text   AS kind,
    p.workspace_id       AS workspace_id,
    b.project_id         AS project_id,
    b.identifier         AS identifier,
    b.title              AS title,
    b.body               AS body,
    b.embedding          AS embedding,
    b.updated_at         AS updated_at
FROM breadcrumbs b
JOIN projects p ON p.id = b.project_id

UNION ALL

SELECT
    'memory'::text       AS kind,
    m.workspace_id       AS workspace_id,
    m.project_id         AS project_id,
    m.name               AS identifier,
    m.name               AS title,
    m.body               AS body,
    m.embedding          AS embedding,
    m.updated_at         AS updated_at
FROM memories m
WHERE m.active = true AND m.embedding IS NOT NULL;
