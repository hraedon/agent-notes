-- Phase 8a: Drop markdown projection apparatus (Plan 003, decisions 39-40).
--
-- IMPORTANT: Back up your database before running this migration.
-- This migration is irreversible (decision 48).
--
-- On-disk markdown files in existing repos are NOT deleted by this migration.
-- They are simply no longer touched by the server.

ALTER TABLE breadcrumbs
    DROP COLUMN IF EXISTS projection_sha256,
    DROP COLUMN IF EXISTS projection_dirty,
    DROP COLUMN IF EXISTS file_path;

ALTER TABLE projects
    DROP COLUMN IF EXISTS breadcrumbs_dir;

DROP INDEX IF EXISTS idx_breadcrumbs_projection_dirty;
