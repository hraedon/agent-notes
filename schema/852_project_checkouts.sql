-- Plan 025 WI-2.6: per-VM checkouts of a project.
--
-- projects.repo_root holds one path, so only one machine can resolve a project
-- from its working directory. A project is checked out at a different path on
-- each machine (/projects/<slug> on Linux, C:\projects\<slug> on Windows). This
-- table maps (vm, path) -> project; resolve_project tries it before repo_root.
--
-- Not backfilled from projects.repo_root: which VM wrote that value is unknown,
-- and guessing would register a path on a machine that may never have had it.
-- resolve_project records a checkout the first time a repo_root match is
-- confirmed on a VM (the directory exists there), and `init`, `checkout add`
-- and `checkout scan` record them explicitly.
--
-- A path is one project on one VM, hence the (vm_id, path) key. Paths are
-- stored as written (normalised, original case and separators); matching
-- folds case and separators for Windows-style paths in code, not here.
--
-- Idempotent: safe to re-run.

CREATE TABLE IF NOT EXISTS project_checkouts (
    project_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    vm_id        TEXT NOT NULL,
    path         TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (vm_id, path)
);

CREATE INDEX IF NOT EXISTS project_checkouts_project_vm_idx
    ON project_checkouts (project_id, vm_id);
