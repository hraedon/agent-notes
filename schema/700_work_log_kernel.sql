-- Work-log coordination kernel schema (Plan 008 Phase P0).
-- Run via: agent-notes-migrate --all
--
-- Design notes:
-- - op_log is the append-only source of truth (op-CRDT model).
-- - work_items is the folded cache (rebuildable from op_log alone).
-- - content_blobs stores bodies by hash so metadata edits don't re-log bodies.
-- - For P0 (single-writer), lamport is a simple monotonic counter per project.
-- - DSSE envelopes are stored in op_log.payload['envelope'] (unverified in P0).

-- ---------------------------------------------------------------------------
-- Content-addressed blobs
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS content_blobs (
    hash    TEXT PRIMARY KEY,
    content TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Op-log: the append-only source of truth
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS op_log (
    id              BIGSERIAL PRIMARY KEY,
    op_id           TEXT NOT NULL UNIQUE,     -- content hash of this op
    entity_id       TEXT NOT NULL,            -- hash of first op (entity identity)
    entity_type     TEXT NOT NULL
        CHECK (entity_type IN ('work_item', 'memory', 'link')),
    op_type         TEXT NOT NULL
        CHECK (op_type IN (
            'create', 'set_status', 'set_field', 'add_link', 'remove_link',
            'claim', 'release', 'heartbeat', 'request', 'wait', 'close',
            'snapshot', 'merge'
        )),
    lamport         BIGINT NOT NULL,
    actor_id        TEXT,                     -- pubkey / signer identifier
    payload         JSONB NOT NULL DEFAULT '{}',
    parent_op_ids   TEXT[] NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Index ordering: entity_id, lamport, op_id for fold/rebuild efficiency.
CREATE INDEX IF NOT EXISTS idx_op_log_entity
    ON op_log (entity_id, lamport, op_id);

CREATE INDEX IF NOT EXISTS idx_op_log_op_id
    ON op_log (op_id);

CREATE INDEX IF NOT EXISTS idx_op_log_entity_type
    ON op_log (entity_type, created_at DESC);

-- ---------------------------------------------------------------------------
-- Work items: folded cache of work_item entities
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS work_item_sequences (
    project_id      INTEGER NOT NULL PRIMARY KEY REFERENCES projects(id),
    prefix          TEXT NOT NULL DEFAULT 'WI',
    next_number     INTEGER NOT NULL DEFAULT 1
);

-- Seed work_item_sequences for existing projects.
INSERT INTO work_item_sequences (project_id, prefix, next_number)
SELECT id, 'WI', 1 FROM projects
ON CONFLICT (project_id) DO NOTHING;

-- Helper: allocate a new identifier from work_item_sequences
CREATE OR REPLACE FUNCTION allocate_work_item_identifier(_project_id INTEGER)
RETURNS TEXT AS $$
DECLARE
    _prefix TEXT;
    _next   INTEGER;
    _result TEXT;
BEGIN
    SELECT prefix, next_number INTO _prefix, _next
    FROM work_item_sequences WHERE project_id = _project_id
    FOR UPDATE;

    IF _prefix IS NULL THEN
        _prefix := 'WI';
        _next   := 1;
        INSERT INTO work_item_sequences (project_id, prefix, next_number)
        VALUES (_project_id, _prefix, _next);
    END IF;

    _result := _prefix || '-' || LPAD(_next::text, 3, '0');

    LOOP
        PERFORM 1 FROM work_items
        WHERE project_id = _project_id AND identifier = _result;
        EXIT WHEN NOT FOUND;
        _next := _next + 1;
        _result := _prefix || '-' || LPAD(_next::text, 3, '0');
    END LOOP;

    UPDATE work_item_sequences SET next_number = _next + 1
    WHERE project_id = _project_id;

    RETURN _result;
END;
$$ LANGUAGE plpgsql;

CREATE TABLE IF NOT EXISTS work_items (
    id              BIGSERIAL PRIMARY KEY,
    entity_id       TEXT NOT NULL UNIQUE,     -- references op_log.entity_id
    project_id      INTEGER NOT NULL REFERENCES projects(id),
    identifier      TEXT NOT NULL,
    title           TEXT NOT NULL,
    body_hash       TEXT NOT NULL REFERENCES content_blobs(hash),
    kind            TEXT NOT NULL,
    status          TEXT NOT NULL
        CHECK (status IN ('open', 'claimed', 'closed', 'deferred')),
    severity        TEXT NOT NULL DEFAULT 'medium',
    external_refs   JSONB NOT NULL DEFAULT '{}',
    diagnostic_keys JSONB NOT NULL DEFAULT '{}',
    embedding       vector(768),
    frontmatter_version SMALLINT NOT NULL DEFAULT 1,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at       TIMESTAMPTZ,
    UNIQUE (project_id, identifier)
);

CREATE INDEX IF NOT EXISTS idx_work_items_status
    ON work_items (project_id, status);

CREATE INDEX IF NOT EXISTS idx_work_items_embedding
    ON work_items USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- ---------------------------------------------------------------------------
-- Status-change trigger: writes to op_log + maintains closed_at
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION wi_status_changed_fn() RETURNS TRIGGER AS $$
DECLARE
    _workspace_id INTEGER;
    _is_terminal BOOLEAN;
BEGIN
    -- INSERT: handled by the kernel; we only maintain closed_at.
    IF TG_OP = 'INSERT' THEN
        SELECT workspace_id INTO _workspace_id
        FROM projects WHERE id = NEW.project_id;

        SELECT COALESCE(v.is_terminal, FALSE)
          INTO _is_terminal
          FROM vocabularies v
         WHERE v.workspace_id = _workspace_id
           AND v.kind_namespace = 'wi_status'
           AND v.name = NEW.status;

        IF _is_terminal THEN
            NEW.closed_at := COALESCE(NEW.closed_at, now());
        ELSE
            NEW.closed_at := NULL;
        END IF;
        RETURN NEW;
    END IF;

    -- UPDATE: only act when status actually changed.
    IF OLD.status = NEW.status THEN
        RETURN NEW;
    END IF;

    SELECT workspace_id INTO _workspace_id
    FROM projects WHERE id = NEW.project_id;

    SELECT COALESCE(v.is_terminal, FALSE)
      INTO _is_terminal
      FROM vocabularies v
     WHERE v.workspace_id = _workspace_id
       AND v.kind_namespace = 'wi_status'
       AND v.name = NEW.status;

    IF _is_terminal THEN
        NEW.closed_at := COALESCE(NEW.closed_at, now());
    ELSE
        NEW.closed_at := NULL;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS wi_status_changed ON work_items;

CREATE TRIGGER wi_status_changed
    BEFORE INSERT OR UPDATE ON work_items
    FOR EACH ROW
    EXECUTE FUNCTION wi_status_changed_fn();

-- ---------------------------------------------------------------------------
-- Op-log event table: post-commit hook surface
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS op_log_events (
    id          BIGSERIAL PRIMARY KEY,
    op_id       TEXT NOT NULL REFERENCES op_log(op_id),
    event_type  TEXT NOT NULL,
    payload     JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_op_log_events_type
    ON op_log_events (event_type, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_op_log_events_op
    ON op_log_events (op_id);

-- ---------------------------------------------------------------------------
-- Seed work_item vocabularies (for the default workspace)
-- ---------------------------------------------------------------------------
-- Use a CTE to resolve the default workspace ID dynamically so this works
-- regardless of whether the default workspace was created with id=1 or
-- id=4 (existing databases where workspaces were created before this
-- schema ran).  See Plan 008 Tier-A migration fix.
-- ---------------------------------------------------------------------------

WITH default_ws AS (
    SELECT id AS ws_id FROM workspaces WHERE slug = 'default' LIMIT 1
)
INSERT INTO vocabularies (workspace_id, kind_namespace, name, is_terminal, is_open, sort_order)
SELECT ws_id, 'wi_kind', 'todo',      false, true,  10 FROM default_ws UNION ALL
SELECT ws_id, 'wi_kind', 'observation', false, true,  20 FROM default_ws UNION ALL
SELECT ws_id, 'wi_kind', 'decision',    false, true,  30 FROM default_ws UNION ALL
SELECT ws_id, 'wi_kind', 'risk',        false, true,  40 FROM default_ws UNION ALL
SELECT ws_id, 'wi_kind', 'task',        false, true,  50 FROM default_ws UNION ALL
SELECT ws_id, 'wi_kind', 'bug',         false, true,  60 FROM default_ws UNION ALL
SELECT ws_id, 'wi_kind', 'feature',     false, true,  70 FROM default_ws UNION ALL
SELECT ws_id, 'wi_kind', 'improvement', false, true,  80 FROM default_ws UNION ALL
SELECT ws_id, 'wi_kind', 'question',    false, true,  90 FROM default_ws UNION ALL
SELECT ws_id, 'wi_kind', 'experiment',  false, true,  100 FROM default_ws UNION ALL
SELECT ws_id, 'wi_kind', 'spike',      false, true,  110 FROM default_ws UNION ALL
SELECT ws_id, 'wi_kind', 'refactor',   false, true,  120 FROM default_ws UNION ALL
SELECT ws_id, 'wi_kind', 'docs',       false, true,  130 FROM default_ws UNION ALL
SELECT ws_id, 'wi_kind', 'ci',         false, true,  140 FROM default_ws UNION ALL
SELECT ws_id, 'wi_kind', 'job',        false, true,  150 FROM default_ws UNION ALL
SELECT ws_id, 'wi_status', 'open',      false, true,   10 FROM default_ws UNION ALL
SELECT ws_id, 'wi_status', 'claimed',   false, true,   20 FROM default_ws UNION ALL
SELECT ws_id, 'wi_status', 'closed',    true,  false,  100 FROM default_ws UNION ALL
SELECT ws_id, 'wi_status', 'deferred',  true,  false,  110 FROM default_ws UNION ALL
SELECT ws_id, 'wi_severity', 'low',      false, true,  10 FROM default_ws UNION ALL
SELECT ws_id, 'wi_severity', 'medium',   false, true,  20 FROM default_ws UNION ALL
SELECT ws_id, 'wi_severity', 'high',     false, true,  30 FROM default_ws UNION ALL
SELECT ws_id, 'wi_severity', 'critical', false, true,  40 FROM default_ws
ON CONFLICT (workspace_id, kind_namespace, name) DO UPDATE SET
    is_terminal = EXCLUDED.is_terminal,
    is_open     = EXCLUDED.is_open,
    sort_order  = EXCLUDED.sort_order;

-- ---------------------------------------------------------------------------
-- Ready query: returns work items that are ready (not blocked)
-- ---------------------------------------------------------------------------

-- A work item is ready when:
--   status = 'open' AND status != 'deferred'
--   AND NOT EXISTS (blocks-edge whose target.status IN ('open', 'claimed'))
--
-- We create a view for this so the query is reusable and documented.

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
  );

-- ---------------------------------------------------------------------------
-- Claimable view: ready AND not currently leased
-- For P0, we don't have a lease table yet, so claimable = ready.
-- P4 adds the lease table and coordinator.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW work_items_claimable_v AS
SELECT * FROM work_items_ready_v;
