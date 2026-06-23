-- Plan 009: convergence on regista. Adds the columns the write-through path and
-- the outbox/reconcile layer need on the local work_items projection. regista is
-- the authority; this table is a search/read projection (dossier-006 §5).

ALTER TABLE work_items
    ADD COLUMN IF NOT EXISTS regista_work_item_id UUID,
    ADD COLUMN IF NOT EXISTS pending_sync BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_work_items_regista_id
    ON work_items (regista_work_item_id)
    WHERE regista_work_item_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_work_items_pending_sync
    ON work_items (project_id, pending_sync)
    WHERE pending_sync = TRUE;
