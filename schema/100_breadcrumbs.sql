-- ---------------------------------------------------------------------------
-- bc_sequences: per-project identifier auto-allocation
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS bc_sequences (
    project_id INTEGER NOT NULL PRIMARY KEY REFERENCES projects(id),
    prefix     TEXT NOT NULL DEFAULT 'BC',
    next_number INTEGER NOT NULL DEFAULT 1
);

-- Seed bc_sequences for existing projects using default prefix 'BC'.
INSERT INTO bc_sequences (project_id, prefix, next_number)
SELECT id, 'BC', 1 FROM projects
ON CONFLICT (project_id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Helper: allocate a new identifier from bc_sequences
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION allocate_bc_identifier(_project_id INTEGER)
RETURNS TEXT AS $$
DECLARE
    _prefix TEXT;
    _next   INTEGER;
    _result TEXT;
BEGIN
    SELECT prefix, next_number INTO _prefix, _next
    FROM bc_sequences WHERE project_id = _project_id
    FOR UPDATE;

    IF _prefix IS NULL THEN
        _prefix := 'BC';
        _next   := 1;
        INSERT INTO bc_sequences (project_id, prefix, next_number)
        VALUES (_project_id, _prefix, _next);
    END IF;

    _result := _prefix || '-' || LPAD(_next::text, 3, '0');

    -- Keep incrementing until we find an unused identifier (collision-safe loop).
    LOOP
        PERFORM 1 FROM breadcrumbs
        WHERE project_id = _project_id AND identifier = _result;
        EXIT WHEN NOT FOUND;
        _next := _next + 1;
        _result := _prefix || '-' || LPAD(_next::text, 3, '0');
    END LOOP;

    UPDATE bc_sequences SET next_number = _next + 1
    WHERE project_id = _project_id;

    RETURN _result;
END;
$$ LANGUAGE plpgsql;

-- ---------------------------------------------------------------------------
-- Breadcrumbs table
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS breadcrumbs (
    project_id          INTEGER NOT NULL REFERENCES projects(id),
    identifier          TEXT    NOT NULL,
    -- Composite PK; project-scoped per decision 13
    PRIMARY KEY (project_id, identifier),

    title               TEXT    NOT NULL,
    body                TEXT    NOT NULL DEFAULT '',

    kind                TEXT    NOT NULL,
    status              TEXT    NOT NULL,
    severity            TEXT    NOT NULL DEFAULT 'medium',

    external_refs       JSONB   NOT NULL DEFAULT '{}',
    diagnostic_keys     JSONB   NOT NULL DEFAULT '{}',

    -- Embedding vector (dim is env-configurable; default 768 matches nomic-embed-text-v1.5)
    embedding           vector(768),

    frontmatter_version SMALLINT NOT NULL DEFAULT 1,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at           TIMESTAMPTZ
);

-- HNSW index on embedding vector; dim is env-configurable (decision 2).
-- Default dim 768 matches nomic-embed-text-v1.5.
CREATE INDEX IF NOT EXISTS idx_breadcrumbs_embedding
    ON breadcrumbs USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX IF NOT EXISTS idx_breadcrumbs_status
    ON breadcrumbs (project_id, status);

-- ---------------------------------------------------------------------------
-- Status-change trigger: writes to change_log + maintains closed_at
-- ---------------------------------------------------------------------------

-- Terminal-status vocab lookup: reads the first-class `is_terminal` column
-- (decision 23 / Kimi round-5 #1) so triggers never reach into JSONB.
CREATE OR REPLACE FUNCTION bc_status_changed_fn() RETURNS TRIGGER AS $$
DECLARE
    _is_terminal BOOLEAN;
    _workspace_id INTEGER;
BEGIN
    -- INSERT handled by the tool; we only write change_log on UPDATE (decision 20).
    IF TG_OP = 'INSERT' THEN
        -- Still maintain closed_at if the initial status is terminal.
        SELECT workspace_id INTO _workspace_id
        FROM projects WHERE id = NEW.project_id;

        SELECT COALESCE(v.is_terminal, FALSE)
          INTO _is_terminal
          FROM vocabularies v
         WHERE v.workspace_id = _workspace_id
           AND v.kind_namespace = 'bc_status'
           AND v.name = NEW.status;

        IF _is_terminal THEN
            NEW.closed_at := COALESCE(NEW.closed_at, now());
        ELSE
            NEW.closed_at := NULL;
        END IF;

        RETURN NEW;
    END IF;

    -- UPDATE path:
    -- Only act when status actually changed (skip UPDATE with no change).
    IF OLD.status = NEW.status THEN
        RETURN NEW;
    END IF;

    -- Resolve workspace from project (needed for vocab lookup).
    SELECT workspace_id INTO _workspace_id
    FROM projects WHERE id = NEW.project_id;

    -- Look up whether the new status is terminal (decision 23).
    SELECT COALESCE(v.is_terminal, FALSE)
      INTO _is_terminal
      FROM vocabularies v
     WHERE v.workspace_id = _workspace_id
       AND v.kind_namespace = 'bc_status'
       AND v.name = NEW.status;

    IF _is_terminal THEN
        NEW.closed_at := COALESCE(NEW.closed_at, now());
    ELSE
        NEW.closed_at := NULL;
    END IF;

    -- Write change_log row (decision 7 / 20) -- only for status changes.
    INSERT INTO change_log (kind, workspace_id, project_id, identifier, event, payload)
    VALUES (
        'breadcrumb',
        _workspace_id,
        NEW.project_id,
        NEW.identifier,
        'status_changed',
        jsonb_build_object(
            'old_status', OLD.status,
            'new_status', NEW.status,
            'is_terminal', _is_terminal
        )
    );

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS bc_status_changed ON breadcrumbs;

CREATE TRIGGER bc_status_changed
    BEFORE INSERT OR UPDATE ON breadcrumbs
    FOR EACH ROW
    EXECUTE FUNCTION bc_status_changed_fn();

-- ---------------------------------------------------------------------------
-- Seed breadcrumb vocabularies (for the default workspace)
-- ---------------------------------------------------------------------------

INSERT INTO vocabularies (workspace_id, kind_namespace, name, is_terminal, is_open, sort_order)
VALUES
    -- bc_kind entries
    (1, 'bc_kind', 'todo',      false, true,  10),
    (1, 'bc_kind', 'observation', false, true,  20),
    (1, 'bc_kind', 'decision',    false, true,  30),
    (1, 'bc_kind', 'risk',        false, true,  40),
    (1, 'bc_kind', 'task',        false, true,  50),
    (1, 'bc_kind', 'bug',         false, true,  60),
    (1, 'bc_kind', 'feature',     false, true,  70),
    (1, 'bc_kind', 'improvement', false, true,  80),
    (1, 'bc_kind', 'question',    false, true,  90),
    (1, 'bc_kind', 'experiment',  false, true,  100),
    (1, 'bc_kind', 'spike',      false, true,  110),
    (1, 'bc_kind', 'refactor',   false, true,  120),
    (1, 'bc_kind', 'docs',       false, true,  130),
    (1, 'bc_kind', 'ci',         false, true,  140),
    (1, 'bc_kind', 'job',        false, true,  150),
    -- bc_status entries
    (1, 'bc_status', 'new',        false, true,   10),
    (1, 'bc_status', 'open',       false, true,   20),
    (1, 'bc_status', 'in_progress', false, true,   30),
    (1, 'bc_status', 'blocked',    false, true,   40),
    (1, 'bc_status', 'under_review', false, true, 50),
    (1, 'bc_status', 'resolved',   true,  false,  100),
    (1, 'bc_status', 'closed',     true,  false,  110),
    (1, 'bc_status', 'wont_fix',   true,  false,  120),
    (1, 'bc_status', 'duplicate',  true,  false,  130),
    -- bc_severity entries
    (1, 'bc_severity', 'low',      false, true,  10),
    (1, 'bc_severity', 'medium',   false, true,  20),
    (1, 'bc_severity', 'high',     false, true,  30),
    (1, 'bc_severity', 'critical', false, true,  40)
ON CONFLICT (workspace_id, kind_namespace, name) DO UPDATE SET
    is_terminal = EXCLUDED.is_terminal,
    is_open     = EXCLUDED.is_open,
    sort_order  = EXCLUDED.sort_order;
