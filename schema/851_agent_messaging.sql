-- Plan 025 Phase 3: agent sessions (presence) and the Postgres mailbox (§5).
-- Idempotent: safe to re-run.

-- ---------------------------------------------------------------------------
-- agent_sessions: one row per harness session, keyed '<vm>/<harness>/<id>'
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS agent_sessions (
    session_key    TEXT PRIMARY KEY,
    workspace_id   INTEGER NOT NULL REFERENCES workspaces(id),
    vm_id          TEXT NOT NULL,
    harness        TEXT NOT NULL,
    session_id     TEXT NOT NULL,
    paseo_agent_id TEXT,
    project_id     INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    cwd            TEXT,
    wake_policy    TEXT NOT NULL DEFAULT 'none'
                   CHECK (wake_policy IN ('none', 'direct', 'any-direct-from-project')),
    -- Set once a human picks the policy (`session policy`); registration then
    -- never recomputes the Paseo-derived default over it (D6).
    wake_policy_explicit BOOLEAN NOT NULL DEFAULT false,
    trust          TEXT NOT NULL DEFAULT 'normal'
                   CHECK (trust IN ('normal', 'untrusted-input')),
    auto_continues INTEGER NOT NULL DEFAULT 0,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS agent_sessions_vm_seen_idx
    ON agent_sessions (vm_id, last_seen_at);

CREATE INDEX IF NOT EXISTS agent_sessions_vm_cwd_idx
    ON agent_sessions (vm_id, cwd);

-- ---------------------------------------------------------------------------
-- agent_messages: direct, inbox, notice and request rows
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS agent_messages (
    id            BIGSERIAL PRIMARY KEY,
    workspace_id  INTEGER NOT NULL REFERENCES workspaces(id),
    from_session  TEXT,
    from_vm       TEXT NOT NULL,
    from_project  TEXT,
    to_session    TEXT,
    to_project    TEXT,
    to_vm         TEXT,
    kind          TEXT NOT NULL DEFAULT 'message'
                  CHECK (kind IN ('message', 'question', 'reply', 'handoff', 'notice', 'request')),
    -- SET NULL so pruning an expired question never blocks on a live reply.
    reply_to      BIGINT REFERENCES agent_messages(id) ON DELETE SET NULL,
    subject       TEXT,
    body          TEXT NOT NULL CHECK (length(body) <= 32768),
    payload       JSONB NOT NULL DEFAULT '{}',
    wake          BOOLEAN NOT NULL DEFAULT false,
    claimed_by    TEXT,
    claimed_at    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL DEFAULT now() + interval '7 days',
    CONSTRAINT agent_messages_addressed
        CHECK (num_nonnulls(to_session, to_project, to_vm) >= 1),
    CONSTRAINT agent_messages_direct_or_inbox
        CHECK (to_session IS NULL OR (to_project IS NULL AND to_vm IS NULL)),
    CONSTRAINT agent_messages_notice_not_direct
        CHECK (kind <> 'notice' OR to_session IS NULL),
    CONSTRAINT agent_messages_request_to_vm
        CHECK (kind <> 'request' OR (to_vm IS NOT NULL AND to_session IS NULL))
);

-- Direct delivery: unclaimed rows for one session.
CREATE INDEX IF NOT EXISTS agent_messages_direct_idx
    ON agent_messages (to_session, expires_at)
    WHERE to_session IS NOT NULL AND claimed_by IS NULL;

-- Inbox and notice delivery by (project, VM).
CREATE INDEX IF NOT EXISTS agent_messages_inbox_idx
    ON agent_messages (to_project, to_vm)
    WHERE to_session IS NULL AND kind <> 'request';

-- Dispatcher request queue per VM.
CREATE INDEX IF NOT EXISTS agent_messages_request_idx
    ON agent_messages (to_vm, id)
    WHERE kind = 'request' AND claimed_by IS NULL;

CREATE INDEX IF NOT EXISTS agent_messages_reply_to_idx
    ON agent_messages (reply_to)
    WHERE reply_to IS NOT NULL;

CREATE INDEX IF NOT EXISTS agent_messages_expires_idx
    ON agent_messages (expires_at);

-- ---------------------------------------------------------------------------
-- NOTIFY on insert: IDs and addresses only, never content
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION agent_messages_notify_fn() RETURNS TRIGGER AS $$
BEGIN
    PERFORM pg_notify(
        'agent_notes_messages',
        json_build_object(
            'id',         NEW.id,
            'kind',       NEW.kind,
            'to_session', NEW.to_session,
            'to_project', NEW.to_project,
            'to_vm',      NEW.to_vm,
            'wake',       NEW.wake
        )::text
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS agent_messages_notify ON agent_messages;

CREATE TRIGGER agent_messages_notify
    AFTER INSERT ON agent_messages
    FOR EACH ROW EXECUTE FUNCTION agent_messages_notify_fn();
