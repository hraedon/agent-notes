-- WI-089: a prerequisite blocks until it is terminal, including while it is
-- in progress, blocked, deferred or in review. Local statuses use the target
-- workspace's vocabulary. The foreign cache has no workspace vocabulary, so
-- only its canonical done/legacy closed statuses establish completion; unknown
-- and NULL statuses fail closed. Dangling links retain the existing behavior.
-- Forward-only, idempotent view replacement; no rows or lease rules change.

CREATE OR REPLACE VIEW work_items_ready_v AS
SELECT
    wi.id,
    wi.entity_id,
    wi.project_id,
    wi.identifier,
    wi.title,
    wi.status,
    wi.kind,
    wi.severity,
    wi.created_at,
    wi.updated_at,
    p.workspace_id,
    p.slug AS project_slug,
    w.slug AS workspace_slug
FROM work_items wi
JOIN projects p ON p.id = wi.project_id
JOIN workspaces w ON w.id = p.workspace_id
WHERE wi.status = 'open'
  -- Exclude same-project blockers (links table)
  AND NOT EXISTS (
      SELECT 1 FROM links l
      WHERE l.from_kind = 'work_item'
        AND l.from_workspace = p.workspace_id
        AND l.from_project = wi.project_id
        AND l.from_identifier = wi.identifier
        AND l.relationship = 'blocks'
        AND l.to_kind = 'work_item'
        AND EXISTS (
            SELECT 1 FROM work_items target
            WHERE target.project_id = l.to_project
              AND target.identifier = l.to_identifier
              AND NOT EXISTS (
                  SELECT 1 FROM vocabularies v
                  JOIN projects tp ON tp.id = target.project_id
                  WHERE v.workspace_id = tp.workspace_id
                    AND v.kind_namespace = 'wi_status'
                    AND v.name = target.status
                    AND v.is_terminal
              )
        )
  )
  -- Exclude cross-repo blockers (cross_project_links + cross_project_work_items)
  AND NOT EXISTS (
      SELECT 1 FROM cross_project_links cpl
      WHERE cpl.from_project_id = wi.project_id
        AND cpl.from_identifier = wi.identifier
        AND cpl.relationship = 'blocks'
        AND EXISTS (
            SELECT 1 FROM cross_project_work_items cp
            WHERE cp.source_project_slug = cpl.to_project_slug
              AND cp.identifier = cpl.to_identifier
              AND (cp.status IS NULL OR cp.status NOT IN ('done', 'closed'))
        )
  );
