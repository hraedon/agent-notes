-- Memory kind schema (Phase 3 / §4.3).
-- Run via: agent-notes-migrate --all  (decision 18).
--
-- Design notes:
-- - Surrogate id BIGSERIAL PK; name uniqueness via partial unique index on active rows.
-- - active BOOLEAN for soft-delete / supersede (Kimi round-5 #2).
-- - supersedes BIGINT for revision chain; mirrored into links table.
-- - No projection_sha256 / projection_dirty — memories don't project to disk
--   (decision 24).
-- - All memories are project-scoped (decision 13); cross-cutting memories live
--   in the conventional 'global' project per workspace.

-- ---------------------------------------------------------------------------
-- memories table
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS memories (
    id              BIGSERIAL PRIMARY KEY,
    workspace_id    INTEGER    NOT NULL REFERENCES workspaces(id),
    project_id      INTEGER    NOT NULL REFERENCES projects(id),
    name            TEXT        NOT NULL,
    memory_type     TEXT        NOT NULL,
    body            TEXT        NOT NULL DEFAULT '',
    embedding       vector(768),
    active          BOOLEAN     NOT NULL DEFAULT TRUE,
    supersedes      BIGINT      REFERENCES memories(id),
    attributes      JSONB       NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Partial unique index: only one active memory per (project, name) at a time.
-- Soft-delete flips active=false, freeing the name for a new revision.
CREATE UNIQUE INDEX IF NOT EXISTS memories_name_active_unique
    ON memories (project_id, name) WHERE active = true;

-- HNSW index for cosine similarity search.
CREATE INDEX IF NOT EXISTS memories_embedding_hnsw
    ON memories USING hnsw (embedding vector_cosine_ops)
    WHERE active = true AND embedding IS NOT NULL;

-- Lookup indexes
CREATE INDEX IF NOT EXISTS memories_project_active
    ON memories (project_id, active, created_at DESC)
    WHERE active = true;

CREATE INDEX IF NOT EXISTS memories_workspace_active
    ON memories (workspace_id, active, created_at DESC)
    WHERE active = true;

CREATE INDEX IF NOT EXISTS memories_supersedes
    ON memories (supersedes)
    WHERE supersedes IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Foreign key: memory_type → vocabularies (composite FK across workspace)
-- ---------------------------------------------------------------------------

-- memory_type references vocabularies(kind_namespace='memory_type') within the
-- same workspace. Since vocabularies PK is (workspace_id, kind_namespace, name),
-- we need a composite check. Enforced at application level (not via FK) because
-- the memories table stores workspace_id separately and the vocabularies PK
-- spans three columns. A trigger would add overhead for marginal gain; the MCP
-- server validates memory_type against vocabularies before INSERT.

-- ---------------------------------------------------------------------------
-- updated_at trigger
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION memories_updated_at_fn() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS memories_updated_at ON memories;

CREATE TRIGGER memories_updated_at
    BEFORE UPDATE ON memories
    FOR EACH ROW EXECUTE FUNCTION memories_updated_at_fn();
