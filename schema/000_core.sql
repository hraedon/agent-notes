-- Core shared schema for agent-notes-mcp.
-- Translated from §4.1 of plans/001-architecture-and-implementation.md.
-- Run via: agent-notes-migrate --all  (decision 18; setup is a thin alias).

CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- Workspaces
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS workspaces (
    id         SERIAL PRIMARY KEY,
    slug       TEXT UNIQUE NOT NULL,
    name       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Projects
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS projects (
    id              SERIAL PRIMARY KEY,
    workspace_id    INTEGER NOT NULL REFERENCES workspaces(id),
    slug            TEXT    NOT NULL,
    name            TEXT    NOT NULL,
    -- repo_root restored per Kimi round-5 #3: used by resolve_project (Plan 002).
    repo_root       TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, slug)
);

-- ---------------------------------------------------------------------------
-- Vocabularies
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS vocabularies (
    workspace_id   INTEGER NOT NULL REFERENCES workspaces(id),
    kind_namespace TEXT    NOT NULL,  -- 'bc_kind', 'bc_status', 'memory_type', ...
    name           TEXT    NOT NULL,
    -- Critical, frequently-queried attributes as first-class columns (decision 23)
    -- so triggers never have to reach into JSONB:
    is_terminal    BOOLEAN NOT NULL DEFAULT FALSE,
    is_open        BOOLEAN NOT NULL DEFAULT TRUE,
    sort_order     INTEGER NOT NULL DEFAULT 100,
    archived       BOOLEAN NOT NULL DEFAULT FALSE,
    -- Free-form bag for presentation-only extras (e.g. identifier_format):
    attributes     JSONB   NOT NULL DEFAULT '{}',
    PRIMARY KEY (workspace_id, kind_namespace, name)
);

-- ---------------------------------------------------------------------------
-- Links
-- ---------------------------------------------------------------------------

-- Generic cross-kind link table. No FKs to kind tables; dangling rows are
-- accepted and filtered at query time (§10 risk register). All kinds are
-- project-scoped (decision 13), so from_project/to_project are NOT NULL and
-- participate in the PK, preventing cross-project identifier collisions
-- (decision 14).
CREATE TABLE IF NOT EXISTS links (
    from_kind        TEXT    NOT NULL,
    from_workspace   INTEGER NOT NULL,
    from_project     INTEGER NOT NULL,
    from_identifier  TEXT    NOT NULL,
    to_kind          TEXT    NOT NULL,
    to_workspace     INTEGER NOT NULL,
    to_project       INTEGER NOT NULL,
    to_identifier    TEXT    NOT NULL,
    relationship     TEXT    NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (from_kind, from_workspace, from_project, from_identifier,
                 to_kind,   to_workspace,   to_project,   to_identifier,
                 relationship)
);

-- Index ordering: node-identifier columns BEFORE relationship for kind-local
-- trace_graph traversal efficiency (Kimi round-3 #3).
CREATE INDEX IF NOT EXISTS idx_links_from
    ON links (from_kind, from_workspace, from_project, from_identifier, relationship);

CREATE INDEX IF NOT EXISTS idx_links_to
    ON links (to_kind, to_workspace, to_project, to_identifier, relationship);

-- ---------------------------------------------------------------------------
-- Change log
-- ---------------------------------------------------------------------------

-- Unified audit log. Single source for changes_since, NOTIFY, history tools
-- (decision 7). Designed for future RANGE partitioning by changed_at if volume
-- warrants.
-- project_id is intentionally nullable (Kimi round-3 #1): workspace-level
-- events like 'vocabulary_added' or 'workspace_created' legitimately have no
-- associated project. All kind-row events carry a non-null project_id.
CREATE TABLE IF NOT EXISTS change_log (
    id           BIGSERIAL PRIMARY KEY,
    kind         TEXT NOT NULL,
    workspace_id INTEGER NOT NULL,
    project_id   INTEGER,  -- nullable: see comment above
    identifier   TEXT NOT NULL,
    event        TEXT NOT NULL,  -- 'filed' | 'updated' | 'status_changed'
                                 -- | 'deleted' | 'projection_written'
    payload      JSONB NOT NULL DEFAULT '{}',
    actor        TEXT,
    changed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_change_log_recent
    ON change_log (changed_at DESC);

CREATE INDEX IF NOT EXISTS idx_change_log_kind
    ON change_log (kind, changed_at DESC);

CREATE INDEX IF NOT EXISTS idx_change_log_target
    ON change_log (kind, workspace_id, project_id, identifier);

-- ---------------------------------------------------------------------------
-- NOTIFY trigger on change_log
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION change_log_notify_fn() RETURNS TRIGGER AS $$
BEGIN
    PERFORM pg_notify(
        'agent_notes_changes',
        json_build_object(
            'kind',         NEW.kind,
            'event',        NEW.event,
            'workspace_id', NEW.workspace_id,
            'project_id',   NEW.project_id,
            'identifier',   NEW.identifier
        )::text
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Drop and recreate so the function body stays current on re-run.
DROP TRIGGER IF EXISTS change_log_notify ON change_log;

CREATE TRIGGER change_log_notify
    AFTER INSERT ON change_log
    FOR EACH ROW EXECUTE FUNCTION change_log_notify_fn();

-- ---------------------------------------------------------------------------
-- Seed data: 'default' workspace and 'global' project (decisions 13, 18)
-- ---------------------------------------------------------------------------

-- Memories (and future cross-cutting kinds) need a 'global' project per
-- workspace for content that isn't scoped to a single repo (decision 13).
INSERT INTO workspaces (slug, name)
VALUES ('default', 'Default Workspace')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO projects (workspace_id, slug, name)
VALUES (
    (SELECT id FROM workspaces WHERE slug = 'default'),
    'global',
    'Global Project'
)
ON CONFLICT (workspace_id, slug) DO NOTHING;
