-- Cross-kind search infrastructure (Phase 4 / §6).
-- Run via: agent-notes-migrate --all  (decision 18).
--
-- Must run after 700_work_log_kernel.sql: all_notes_search_v reads work_items
-- and content_blobs, which that file creates. (Numbered 750 for this reason —
-- it was 400 until the Plan 008 work-item migration moved its dependencies.)
--
-- Design notes:
-- - all_notes_search_v is explicitly search-only (Kimi D); kind-specific fields
--   are fetched via kind tools.
-- - Uses updated_at consistently for the since filter (both work_items and
--   memories have this column).
-- - Memories filtered to active rows only.
-- - Work items need a JOIN to projects for workspace_id and to content_blobs
--   for the body text.

CREATE OR REPLACE VIEW all_notes_search_v AS
SELECT
    'work_item'::text   AS kind,
    p.workspace_id       AS workspace_id,
    wi.project_id        AS project_id,
    wi.identifier        AS identifier,
    wi.title             AS title,
    cb.content           AS body,
    wi.embedding         AS embedding,
    wi.updated_at        AS updated_at
FROM work_items wi
JOIN projects p ON p.id = wi.project_id
JOIN content_blobs cb ON cb.hash = wi.body_hash

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
