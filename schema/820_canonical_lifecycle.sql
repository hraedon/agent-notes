-- Plan 010: Canonical lifecycle convergence. Expands the work_items.status
-- CHECK constraint to accept the canonical v2 states (in_progress, blocked,
-- in_review, in_human_review, done) alongside the legacy breadcrumb states
-- (open, claimed, closed, deferred) so the projection can hold both during
-- the transition. Seeds the canonical wi_status vocabulary entries (with correct
-- is_terminal / is_open flags) for every workspace that already has a
-- wi_status vocabulary, and corrects `deferred` to non-terminal (canonical
-- semantics — deferred is an idle state, not closed; dossier Plan 008).
--
-- This is additive and backward-compatible: the legacy op_log path (flag off)
-- continues to write breadcrumb states, which remain valid. The regista-branch
-- path (flag on) writes canonical states.

-- 1. Expand the status CHECK constraint.
ALTER TABLE work_items
    DROP CONSTRAINT IF EXISTS work_items_status_check;

ALTER TABLE work_items
    ADD CONSTRAINT work_items_status_check
    CHECK (status IN (
        -- legacy breadcrumb (v1)
        'open', 'claimed', 'closed', 'deferred',
        -- canonical (v2)
        'in_progress', 'blocked', 'in_review', 'in_human_review', 'done'
    ));

-- 2. Seed / correct wi_status vocabulary entries for every workspace that has
--    a wi_status namespace. `done` is the canonical terminal (closed_at fires);
--    `closed` remains terminal for legacy items. `deferred` is corrected to
--    non-terminal (it was mis-marked terminal in the breadcrumb vocabulary).
WITH ws_ids AS (
    SELECT DISTINCT workspace_id AS ws_id
    FROM vocabularies
    WHERE kind_namespace = 'wi_status'
)
INSERT INTO vocabularies (workspace_id, kind_namespace, name, is_terminal, is_open, sort_order)
-- canonical states
SELECT ws_id, 'wi_status', 'open',           false, true,   10 FROM ws_ids UNION ALL
SELECT ws_id, 'wi_status', 'in_progress',    false, true,   30 FROM ws_ids UNION ALL
SELECT ws_id, 'wi_status', 'blocked',        false, false,  40 FROM ws_ids UNION ALL
SELECT ws_id, 'wi_status', 'deferred',       false, false,  50 FROM ws_ids UNION ALL
SELECT ws_id, 'wi_status', 'in_review',      false, false,  60 FROM ws_ids UNION ALL
SELECT ws_id, 'wi_status', 'in_human_review',false, false,  70 FROM ws_ids UNION ALL
SELECT ws_id, 'wi_status', 'done',           true,  false, 100 FROM ws_ids UNION ALL
-- legacy states (kept; corrected is_terminal/is_open for consistency)
SELECT ws_id, 'wi_status', 'claimed',        false, true,   20 FROM ws_ids UNION ALL
SELECT ws_id, 'wi_status', 'closed',         true,  false, 110 FROM ws_ids
ON CONFLICT (workspace_id, kind_namespace, name) DO UPDATE SET
    is_terminal = EXCLUDED.is_terminal,
    is_open     = EXCLUDED.is_open,
    sort_order  = EXCLUDED.sort_order;

-- 3. Clear closed_at for items in non-terminal states whose closed_at was set
--    by the old (incorrect) deferred-is-terminal marking, so the projection is
--    honest post-migration. Only touches non-terminal statuses.
UPDATE work_items
SET closed_at = NULL
WHERE closed_at IS NOT NULL
  AND status IN ('deferred', 'blocked', 'in_review', 'in_human_review', 'in_progress');
