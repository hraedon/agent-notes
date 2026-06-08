-- Cross-project layer schema (Plan 008 Phase P3).
-- Run via: agent-notes-migrate --all
--
-- Design notes:
-- - `projects.log_location` and `projects.wake_channel` form the registry
--   (Backstage-style descriptors: project → repo_root → log location → wake channel).
-- - `cross_project_ops` is the derived index: ingested JSONL op-logs from other repos.
-- - `cross_project_work_items` is the folded cache of foreign work items (rebuildable).
-- - `cross_project_links` stores cross-repo edges (the dependent owns the edge).
-- - `work_items_ready_v` is updated to check cross-project blockers.
-- - All DDL is idempotent (IF NOT EXISTS / OR REPLACE).

-- ---------------------------------------------------------------------------
-- Registry columns on projects (log location + wake channel)
-- ---------------------------------------------------------------------------

ALTER TABLE projects
    ADD COLUMN IF NOT EXISTS log_location TEXT,
    ADD COLUMN IF NOT EXISTS wake_channel TEXT;

-- ---------------------------------------------------------------------------
-- Cross-project links: cross-repo edges owned by the dependent
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS cross_project_links (
    id              BIGSERIAL PRIMARY KEY,
    from_project_id INTEGER NOT NULL REFERENCES projects(id),
    from_identifier TEXT NOT NULL,
    to_project_slug TEXT NOT NULL,
    to_identifier   TEXT NOT NULL,
    relationship    TEXT NOT NULL DEFAULT 'blocks',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (from_project_id, from_identifier, to_project_slug, to_identifier, relationship)
);

CREATE INDEX IF NOT EXISTS idx_cross_project_links_from
    ON cross_project_links (from_project_id, from_identifier, relationship);

CREATE INDEX IF NOT EXISTS idx_cross_project_links_to
    ON cross_project_links (to_project_slug, to_identifier, relationship);

-- ---------------------------------------------------------------------------
-- Cross-project ops: derived index of foreign op-logs
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS cross_project_ops (
    id              BIGSERIAL PRIMARY KEY,
    source_project_slug TEXT NOT NULL,
    op_id           TEXT NOT NULL,
    entity_id       TEXT NOT NULL,
    entity_type     TEXT NOT NULL,
    op_type         TEXT NOT NULL,
    lamport         BIGINT NOT NULL,
    actor_id        TEXT,
    payload         JSONB NOT NULL DEFAULT '{}',
    parent_op_ids   TEXT[] NOT NULL DEFAULT '{}',
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    freshness_offset BIGINT,
    UNIQUE (source_project_slug, op_id)
);

CREATE INDEX IF NOT EXISTS idx_cross_project_ops_entity
    ON cross_project_ops (source_project_slug, entity_id, lamport, op_id);

CREATE INDEX IF NOT EXISTS idx_cross_project_ops_type
    ON cross_project_ops (source_project_slug, entity_type, op_type);

-- ---------------------------------------------------------------------------
-- Cross-project freshness: per-source-project offset tracking
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS cross_project_freshness (
    source_project_slug TEXT PRIMARY KEY,
    last_offset         BIGINT,
    last_ingested_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Cross-project work items: folded cache of foreign work items
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS cross_project_work_items (
    id              BIGSERIAL PRIMARY KEY,
    source_project_slug TEXT NOT NULL,
    entity_id       TEXT NOT NULL,
    identifier      TEXT NOT NULL,
    title           TEXT,
    body_hash       TEXT,
    kind            TEXT,
    status          TEXT,
    severity        TEXT,
    external_refs   JSONB,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_project_slug, entity_id),
    UNIQUE (source_project_slug, identifier)
);

CREATE INDEX IF NOT EXISTS idx_cross_project_work_items_status
    ON cross_project_work_items (source_project_slug, status);

-- ---------------------------------------------------------------------------
-- Reverse-edge map: who is blocked by whom (cross-project)
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW cross_project_reverse_edges_v AS
SELECT
    cpl.from_project_id,
    cpl.from_identifier AS blocked_identifier,
    cpl.to_project_slug AS blocker_project,
    cpl.to_identifier AS blocker_identifier,
    cpl.relationship,
    cpl.created_at
FROM cross_project_links cpl;

-- ---------------------------------------------------------------------------
-- Updated ready view: considers both same-project and cross-project blockers
-- ---------------------------------------------------------------------------

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
              AND target.status IN ('open', 'claimed')
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
              AND cp.status IN ('open', 'claimed')
        )
  );

-- ---------------------------------------------------------------------------
-- Claimable view: ready AND not currently leased (P0: same as ready)
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW work_items_claimable_v AS
SELECT * FROM work_items_ready_v;

-- ---------------------------------------------------------------------------
-- NOTIFY trigger on op_log_events (for cross-project wake routing)
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION op_log_events_notify_fn() RETURNS TRIGGER AS $$
BEGIN
    PERFORM pg_notify('agent_notes_op_log_events',
        json_build_object(
            'event_id', NEW.id,
            'op_id', NEW.op_id,
            'event_type', NEW.event_type,
            'payload', NEW.payload
        )::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS op_log_events_notify ON op_log_events;

CREATE TRIGGER op_log_events_notify
    AFTER INSERT ON op_log_events
    FOR EACH ROW EXECUTE FUNCTION op_log_events_notify_fn();
