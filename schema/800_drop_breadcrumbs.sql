-- Drop the legacy breadcrumbs table and its associated objects.
-- All data now lives in work_items (Plan 008 Tier A migration).
-- The breadcrumbs table was kept as a safety net during burn-in;
-- it is now safe to remove.

-- Drop trigger and function first (dependencies).
DROP TRIGGER IF EXISTS bc_status_changed ON breadcrumbs;
DROP FUNCTION IF EXISTS bc_status_changed_fn();

-- Drop the identifier allocation function (references breadcrumbs table).
DROP FUNCTION IF EXISTS allocate_bc_identifier(INTEGER);

-- Drop the sequence table (no longer used).
DROP TABLE IF EXISTS bc_sequences;

-- Drop the breadcrumbs table itself.
DROP TABLE IF EXISTS breadcrumbs;

-- Drop indexes (already dropped with the table, but explicit for migration tracking).
DROP INDEX IF EXISTS idx_breadcrumbs_embedding;
DROP INDEX IF EXISTS idx_breadcrumbs_status;
