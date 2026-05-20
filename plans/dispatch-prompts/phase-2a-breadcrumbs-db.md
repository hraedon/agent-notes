# Phase 2a — Breadcrumbs DB-only server + legacy import

You are implementing Phase 2a of the agent-notes-mcp project: the breadcrumbs kind schema, the breadcrumbs MCP server (DB-only — no markdown projection yet; that's Phase 2b), and the one-shot import from the existing `/projects/breadcrumb-mcp/` Postgres DB.

## Project state at HEAD

The core library (Phase 1a + 1b) is complete and merged. You build on top.

- `src/agent_notes/core/` provides: `mcp.py`, `server.py` (Server base class with `register_tool`, `register_resource_handler`, and 9 inherited core tools including `add_link`/`remove_link`/`history`/`changes_since`/`list_*`), `db.py` (sync `psycopg_pool.ConnectionPool`, workspace/project/vocabulary CRUD, dataclass models), `embed.py` (lazy singleton), `projection.py` (frontmatter + `safe_write` hash-check — you'll USE this in Phase 2b, not yet), `links.py` (generic links + kind-local `trace_graph` CTE with TODO for the kind-table JOIN — **you finish that JOIN here**), `change_log.py`, `notify.py`, `resources.py`.
- `schema/000_core.sql` defines `workspaces`, `projects` (with `repo_root` and `breadcrumbs_dir`), `vocabularies` (with first-class `is_terminal`/`is_open`/`sort_order`), `links` (9-column natural composite PK, NOT NULL project columns), `change_log` (nullable `project_id`, with NOTIFY trigger).
- `tests/conftest.py` exposes the `ephemeral_db` session-scoped fixture.
- 78 tests pass.

The legacy server `/projects/breadcrumb-mcp/src/breadcrumb_mcp/` is your reference for tool shapes — read its `server.py`, `db.py`, and `schema.sql`. Don't modify it; you only read for patterns and import its data.

## Read first

1. `plans/001-architecture-and-implementation.md` — entire document, especially §2 (all 26 decisions), §4.2 (breadcrumbs schema), §6 (Breadcrumbs server tool list), §9 Phase 2a (task table), §12 (peer-review history for context on WHY decisions landed where they did).
2. `AGENTS.md` for conventions.
3. `plans/dispatch-prompts/README.md` for common dispatch conventions, especially the **hard requirement** that every state-mutating MCP tool has a stdin-driven regression test asserting its `change_log` row.
4. `/projects/breadcrumb-mcp/src/breadcrumb_mcp/server.py` — the legacy server's 12 tool implementations. Your new tools have the same external shape EXCEPT where decisions 13/15/17/20 (project-scoping, no git in server, projection columns on BCs, change_log writes) change things.

## Scope

### 2a.1 — `schema/100_breadcrumbs.sql`

Translate plan §4.2 into SQL. Required elements:

- `breadcrumbs` table keyed by `(project_id, identifier)`. Columns: `title TEXT NOT NULL`, `body TEXT NOT NULL DEFAULT ''`, `diagnostic_keys JSONB NOT NULL DEFAULT '{}'`, `external_refs JSONB NOT NULL DEFAULT '{}'`, `kind`/`status`/`severity` (composite FKs to `vocabularies` — see below), `parent_id TEXT` with self-FK, `author TEXT NOT NULL`, `document_date DATE`, `tags TEXT[] NOT NULL DEFAULT '{}'`, `complexity TEXT`, `file_path TEXT` (repo-relative under `projects.breadcrumbs_dir`), `sha TEXT`, `filed_at TIMESTAMPTZ NOT NULL DEFAULT now()`, `updated_at TIMESTAMPTZ NOT NULL DEFAULT now()`, `closed_at TIMESTAMPTZ`, `embedding vector(768)`, `frontmatter_version SMALLINT NOT NULL DEFAULT 1`, `projection_sha256 BYTEA`, `projection_dirty BOOLEAN NOT NULL DEFAULT FALSE`.
- `bc_sequences` table (same as legacy `/projects/breadcrumb-mcp/src/breadcrumb_mcp/schema.sql` lines 11–16): `(project_id, prefix, next_number)`.
- Composite FKs from `breadcrumbs.kind`/`status`/`severity` to `vocabularies (workspace_id, kind_namespace, name)`. The FK can't enforce that the row's project belongs to the right workspace directly in SQL — that's enforced application-side via `db.get_or_create_project`. For the FK itself, add a generated/computed column `workspace_id` on `breadcrumbs` (look up via project_id) OR — simpler — defer FK enforcement to triggers. **Pragmatic recommendation:** add `workspace_id INTEGER NOT NULL` redundantly to `breadcrumbs`, populated by trigger from `projects.workspace_id` on insert, and use it for the composite FKs. Document the trigger in a SQL comment citing this paragraph.
- HNSW index: `CREATE INDEX idx_bc_embedding_hnsw ON breadcrumbs USING hnsw (embedding vector_cosine_ops);`
- GIN index: `CREATE INDEX idx_bc_diagnostic_keys ON breadcrumbs USING GIN (diagnostic_keys jsonb_path_ops);`
- GIN on tags: `CREATE INDEX idx_bc_tags ON breadcrumbs USING GIN (tags);`
- Composite: `CREATE INDEX idx_bc_status_severity ON breadcrumbs (project_id, status, severity);`
- **Status trigger** (`bc_status_trigger_fn`): on INSERT, write `change_log` row event=`filed`; on UPDATE OF status, write `change_log` row event=`status_changed`, AND check `vocabularies.is_terminal` (the first-class column per decision 23 — NOT JSONB) for the new status to set/clear `closed_at`. Reads from the column, not from `attributes JSONB`.
- Migration must be idempotent (use `CREATE ... IF NOT EXISTS`; `CREATE OR REPLACE FUNCTION`; etc.). `agent-notes-migrate` runs it after `000_core.sql`.

### 2a.2 — `src/agent_notes/servers/breadcrumbs.py`

Thin server class subclassing `Server` from `core/server.py`. Registers 12 tools matching the legacy server's external shape, with these specific differences:

1. **`file_breadcrumb`** — no `file_path` argument (server derives it from `projects.breadcrumbs_dir`); also no actual filesystem write yet (Phase 2b adds that). Just inserts row + writes `change_log` row in the same transaction. Embedding call OUTSIDE the transaction (decision 26): embed → BEGIN → INSERT bc + change_log → COMMIT. Uses `db._conn()` directly for the transaction.

   Required args: `project`, `title`, `kind`, `author`. Optional: `body`, `severity`, `status` (default `proposed`), `tags`, `parent_id`, `diagnostic_keys`, `document_date`, `complexity`, `external_refs` (replaces `filed_in_run`). Identifier auto-assigned via `bc_sequences` using the kind's `identifier_format` from `vocabularies.attributes.identifier_format` (default `"{n}"`).

2. **`update_breadcrumb`** — same as legacy but re-embeds on title/body change. Writes `change_log` event=`updated`. For terminal-status transitions, updates `file_path` to the canonical resolved-dir form (e.g. `resolved/RFC-031.md`) but **does NOT touch the filesystem or run git** (decision 15). Phase 2b adds the filesystem move; the `/end` skill does the `git mv`.

3. **`query_breadcrumbs`**, **`find_breadcrumbs`**, **`get_breadcrumb`**, **`suggest_duplicates`**, **`diagnose`**, **`trace_graph`** (kind-local — see below), **`compute_projection_paths`** (helper for the `/end` skill — see signature below) — straight ports from the legacy server.

4. `trace_graph` is the kind-local variant from `core/links.py` with the kind-table JOIN finished. **You wire the JOIN here.** The TODO in `core/links.py` says to add `JOIN {kind}s k ON k.project_id = node_project AND k.identifier = node_id` once kind tables exist. Since breadcrumbs is the first kind table, do it now: add an optional `target_table: str | None = None` kwarg to `trace_graph` in core/links.py (default None means no JOIN; existing tests still pass), and pass `target_table='breadcrumbs'` from the breadcrumbs server's wrapper. The JOIN populates `LinkedNode.title` and `LinkedNode.status`.

5. **`compute_projection_paths(identifier, target_status) -> dict`** — returns `{"old_absolute": str, "new_absolute": str, "old_repo_relative": str, "new_repo_relative": str}`. Uses `projects.repo_root` and `projects.breadcrumbs_dir` to compose paths. Used by the `/end` skill in Phase 2b for git operations.

Tool list registered: the 9 inherited core tools + the 12 above = ~21 tools total. The legacy server's `add_relationship` and `remove_relationship` tools are replaced by the inherited `add_link`/`remove_link` (which already route through `links.py` per the Phase 1b fix).

### 2a.3 — `scripts/import_legacy_bc.py`

One-shot ETL from `/projects/breadcrumb-mcp/`'s DSN to the new `agent_notes` DB. Driven by a CLI arg `--source-dsn` and reading `AGENT_NOTES_DSN` for the destination.

Steps in order:

1. Connect to source DB. Read all `projects` rows; for each, `get_or_create_project` in the destination with `workspace_id=default-workspace`, `slug` from source, and `breadcrumbs_dir` set to the appropriate path under `/projects/<slug>/breadcrumbs`. `repo_root` set to `/projects/<slug>`.
2. For each project, read every `breadcrumbs` row from source. Collect the union of observed `kind`, `status`, and `severity` values across all rows.
3. **Seed `vocabularies` with first-class attributes populated** (Gemini round-2 #3 — load-bearing): for kind values, just `name`. For statuses, set `is_terminal=true` for `resolved`, `implemented`, `obsolete`, `wont_fix`; `is_open=true` for `proposed`, `open`, `in_progress`, `blocked`, `decision-pending`; `sort_order` such that critical/open statuses appear first in indexes. For severities, set `sort_order` 1/2/3/4 for `critical`/`high`/`medium`/`low`. Identifier formats: `rfc` and `design` get `"RFC-{n:03d}"`; `defect-class` gets `"CLASS-{n:03d}"`; everything else `"{n}"` — stored in `vocabularies.attributes.identifier_format`.
4. **Normalize legacy `file_path` to repo-relative-under-`breadcrumbs_dir`** (DeepSeek round-4 #3). Legacy values may be absolute (e.g. `/projects/substrate/breadcrumbs/195-foo.md`); strip the prefix so the stored value is `195-foo.md` or `resolved/195-foo.md`. Verify by composing back with `repo_root + breadcrumbs_dir + file_path` and asserting the result matches the original.
5. Disable the `change_log_notify` trigger before bulk insert; re-enable after (Kimi round-3 #2). Bulk insert via `COPY` (rows) and a single batch INSERT for `change_log` (GLM #4). For ~250 rows total across all repos, this should take seconds.
6. Replay status history into `change_log`: the legacy `bc_status_history` table records old/new transitions; write one `change_log` row per transition with `event='status_changed'` and `payload={"old_status": old, "new_status": new}`.
7. Recreate relationships in the generic `links` table: legacy `bc_relationships` rows become `links` rows with `from_kind='breadcrumb'`, `relationship` from the legacy `kind` column.
8. Re-embed every breadcrumb via `core/embed.py`. In-process; sequential is fine for ~250 rows.

Idempotent: re-running should be a no-op (use `ON CONFLICT DO NOTHING` where appropriate, or check for existence before insert). Print a summary at the end: N projects, N breadcrumbs, N status transitions, N relationships imported.

### 2a.4 — Tests

For every state-mutating tool (`file_breadcrumb`, `update_breadcrumb`, the inherited `add_link`/`remove_link`), write a stdin-driven test that:
1. Drives the tool end-to-end via fake stdin/stdout against a real `Server` instance.
2. Asserts the corresponding `change_log` row exists with the expected event type.

Pattern: copy `tests/test_core_tools_audit.py` for the structure.

For query tools (`query_breadcrumbs`, `find_breadcrumbs`, `get_breadcrumb`, `diagnose`, `suggest_duplicates`, `trace_graph`, `compute_projection_paths`), unit-style tests against the breadcrumbs server instance are fine — but at least one end-to-end test per tool category.

For the trigger: insert a BC, update status to `resolved`, assert `closed_at` is set and a `change_log` row event=`status_changed` exists. Update back to `open`, assert `closed_at` is cleared.

For the import script: minimal test that uses a tiny seed DSN (or a programmatically created source DB) and verifies idempotency.

## Validation

1. `uv pip install -e ".[test]"` still succeeds.
2. `ruff check` clean.
3. All 78 previous tests still pass.
4. New tests for Phase 2a all pass (target: ~30 new tests).
5. `agent-notes-migrate --all` runs `000_core.sql` then `100_breadcrumbs.sql` cleanly against a fresh PG.
6. The `agent-notes-breadcrumbs` binary starts, responds to `initialize` + `tools/list` + `tools/call`, and the 12 new tools + 9 inherited tools all appear in `tools/list`.
7. Import script runs against the existing `/projects/breadcrumb-mcp/` data and produces a reasonable summary.

## Out of scope

- Markdown projection / `safe_write` calls — Phase 2b.
- `/end` skill changes — Phase 2b.
- `render_index`, `audit`, `reconcile_projection` tools — Phase 2b.
- Memory server, search server, reflections — later phases.

## Style + commit hygiene

- Commit per logical unit: schema, server, import script, tests.
- Imperative commit messages, citing decision numbers where relevant.
- No new third-party dependencies without justification.
- Match the line length and lint rules already configured (`pyproject.toml`).

## Report at end

- Files added/modified (file tree).
- Validation results (test counts before/after, ruff status).
- Any deviations from the plan with justification.
- Honest assessment: is the breadcrumbs server ready for Phase 2b to add projection on top? Are there any tool surface gaps I should know about before dispatching Phase 2b?
