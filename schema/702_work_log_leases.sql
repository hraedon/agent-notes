-- Plan 008 P4 — Lease table and coordinator-ready schema.
-- Run via: agent-notes-migrate --all
--
-- Design:
-- - work_item_leases holds local lease records (P4 coordinator integration).
-- - The claimable view excludes leased items.
-- - A sweep function requeues expired leases (returning them to claimable).
-- - All DDL is idempotent (IF NOT EXISTS / OR REPLACE).

-- ---------------------------------------------------------------------------
-- Lease table
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS work_item_leases (
    id              BIGSERIAL PRIMARY KEY,
    entity_id       TEXT NOT NULL REFERENCES work_items(entity_id),
    actor_id        TEXT NOT NULL,
    acquired_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,
    heartbeat_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE (entity_id)
);

CREATE INDEX IF NOT EXISTS idx_leases_expires
    ON work_item_leases (expires_at);

-- ---------------------------------------------------------------------------
-- Claimable view: ready AND NOT currently leased
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW work_items_claimable_v AS
SELECT wi.*
FROM work_items_ready_v wi
WHERE NOT EXISTS (
    SELECT 1 FROM work_item_leases l
    WHERE l.entity_id = wi.entity_id
      AND l.expires_at > now()
);

-- ---------------------------------------------------------------------------
-- Sweep expired leases: return them to claimable
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION sweep_expired_leases()
RETURNS INTEGER AS $$
DECLARE
    _count INTEGER;
BEGIN
    DELETE FROM work_item_leases
    WHERE expires_at <= now();

    GET DIAGNOSTICS _count = ROW_COUNT;
    RETURN _count;
END;
$$ LANGUAGE plpgsql;

-- ---------------------------------------------------------------------------
-- Trigger: on lease release, emit an event
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION lease_release_notify_fn() RETURNS TRIGGER AS $$
BEGIN
    PERFORM pg_notify('agent_notes_changes',
        json_build_object(
            'kind', 'work_item',
            'event', 'lease_released',
            'entity_id', OLD.entity_id,
            'actor_id', OLD.actor_id
        )::text);
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS lease_release_notify ON work_item_leases;
CREATE TRIGGER lease_release_notify
    AFTER DELETE ON work_item_leases
    FOR EACH ROW
    EXECUTE FUNCTION lease_release_notify_fn();

-- ---------------------------------------------------------------------------
-- op_type 'claim' and 'release' are valid in the op_log schema
-- (already declared in 700_work_log_kernel.sql, but reassert here
-- so the CHECK constraint is documented in P4 context).
-- ---------------------------------------------------------------------------

-- No ALTER needed — the op_type CHECK in 700_work_log_kernel.sql already
-- includes 'claim' and 'release'.
