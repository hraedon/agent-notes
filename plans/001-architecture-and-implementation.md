# Plan 001 — agent-notes-mcp Architecture and Phased Implementation

Status: Complete (all phases shipped). Projection layer removed in Plan 003 Phase 8a (decisions 39–48). **MCP transport removed in Plan 004 Phase 9d (decisions 1, 6, 10, 12, 16, 21 superseded).** CLI is now the primary sync surface.
Scope: Greenfield consolidation of `breadcrumb-mcp` and `memory-mcp` into a shared-core, thin-server architecture. Foundation for `reflections`, `decisions`, and future agent-notes kinds.
Consumers: sf2, substrate, sf1, and the Claude Code / OpenCode / Gemini CLI harness configs.
Supersedes: `breadcrumb-mcp/plans/001..003`, `memory-mcp/plans/001`.

## 1. Vision

One repository, one core library, one Postgres database, in-process embedding, multiple thin per-kind MCP servers. The shared 80–85% of plumbing (MCP JSON-RPC loop, workspaces/projects/vocabularies, pgvector wiring, markdown projection, link graph, NOTIFY, MCP resources surface) lives in `agent_notes.core`. Each kind (breadcrumbs, memories, reflections, …) is a ~300-line server that composes the core and contributes its kind-specific schema, tool surface, and lifecycle.

## 2. Design decisions

1. **Shared library, separate tables, one database.** Not an omnibus single table with a discriminator. Each kind keeps its honest schema; cross-kind queries go through a thin search server.
2. **In-process embedding via thread-safe lazy singleton.** Each server loads the 270MB nomic model on first use; cached in process for the lifetime. (Reversed from v1.1 sidecar design after DeepSeek's pushback: 5 × 270MB on a 64GB homelab is not worth a permanent HTTP dependency, retry policy, systemd unit, and SPOF. If running many simultaneous servers becomes painful, fall back to the optional omnibus binary (decision 12) before reaching for a sidecar.)
3. **Single Postgres database** `agent_notes` with kind tables (`breadcrumbs`, `memories`, `reflections`) plus shared infra tables (`workspaces`, `projects`, `vocabularies`, `links`, `change_log`).
4. **DB is working SoT; markdown is a deterministic projection** rebuilt by `/end`. Spec-amendment items stay reviewable in git because the projection is committed. **Note:** Projection removed in Plan 003; DB is now the sole source of truth.
5. **Per-workspace, per-kind vocabularies** live in data, not in `CHECK` constraints.
6. **One installable Python package, multiple console-script entry points** — simpler than a true monorepo. **Superseded by Plan 004:** single `agent-notes` CLI entry point replaces per-kind MCP binaries.

Revisions from peer review:

7. **`change_log` is the single audit/history source** (Kimi C). No per-kind history tables. The breadcrumbs status trigger writes `event = 'status_changed'` rows directly into `change_log`. NOTIFY fires off `change_log` inserts. One table, one truth, no double-writing.
8. **Projection writer hash-checks before overwriting** (Gemini 1). On every projection write, compute SHA-256 of the existing file bytes; if it doesn't match the hash recorded on the last successful write (`projection_sha256` column on the row), refuse and surface a `reconcile_projection` tool call. This catches human edits before they're silently clobbered. Eventual-consistency stance: DB commits first, file writes second; failure leaves a "dirty projection" flag for re-run.
9. **Vocabulary deletes are reference-checked** (Gemini 3). `vocabularies` deletes raise if any kind table still references the entry. Provide `archive_vocabulary` for soft-deprecation (hides in pick-lists, keeps existing rows valid).
10. **`trace_graph` is kind-local by default; cross-kind traversal is `trace_graph_all` in the search server** (Kimi B). The CTE within a single kind table is clean and indexable; the cross-kind variant uses `LATERAL UNION ALL` against per-kind row sources and is slower by construction. Don't pay for it on every traversal. **Note:** Now exposed via `agent-notes link trace` CLI subcommand.
11. ~~Embedding client hides transient sidecar blips.~~ **Withdrawn in v1.3** with the sidecar (decision 2 reversed). In-process embedding has no network failure mode.
12. **`Server` base class composable into a future omnibus process** (Gemini 2). Each kind server defines tools as a class registry, not a module-level dict. Default deployment runs one binary per kind; a future `agent-notes-omnibus` entry point can mount all four registries in one process if resident-memory overhead becomes a problem. **Superseded by Plan 004:** MCP server removed; CLI is the primary surface. Model layer lives in `core/` modules.

Round-2 revisions:

13. **Memories are project-scoped** (Kimi #4). Every kind table — breadcrumbs, memories, reflections — carries a non-null `project_id`. Cross-cutting memories (user preferences, global feedback) live in a conventional `global` project per workspace. This eliminates the nullable-FK awkwardness in `links` (decision 14) and lets every kind use the same lookup shape.
14. **Links use a natural composite PK including `project`** (Kimi #1). With memories project-scoped, `links.from_project` and `links.to_project` are `NOT NULL` and participate in the PK. No surrogate key, no functional unique index, no `RFC-031` collision across projects.
15. **MCP servers do not run `git`** (Kimi #3). Status transitions that change canonical disk location update `file_path` in the DB to the new canonical path. The `/end` skill — which already owns git context — performs the filesystem move and commit. Servers may run as systemd units without git installed.
16. **Link mutation is a generic core tool exposed by every kind server** (Kimi #2). `add_link` and `remove_link` come from `core.server.Server` (like `list_projects`/`changes_since`); every kind server inherits them. An agent connected to the breadcrumbs server can link a breadcrumb to a memory without switching connections. **Superseded by Plan 004:** Link operations exposed via `agent-notes link add/remove/trace` CLI subcommands.
17. **Any kind table that projects to markdown gets `projection_sha256 BYTEA` and `projection_dirty BOOLEAN NOT NULL DEFAULT FALSE`** (Kimi #5.1). Documented as a core convention; checked by tests for every kind table in Phase 5+.
18. **Schema setup is `migrate --all` against an empty DB** (Kimi #5.2). One source of DDL truth: the numbered files in `schema/`. `agent-notes-setup` is a thin alias. No double-maintenance.
19. **Reflections: body append-only, metadata mutable** (Kimi #5.3). `mark_gaps_filed` updates `gaps_filed_as` / `gaps_extracted_at` in place; the narrative body and section JSONB are immutable. Freshness still flows through `replaced_by`.
20. **`change_log` writes have explicit owners** (Kimi #5.4). `event = 'filed'` is written by the kind tool (`file_breadcrumb`, `add_memory`, `add_reflection`) inside the same transaction as the row insert. `event = 'status_changed'` is written by the per-kind status trigger. `event = 'updated'` by the update tools. `event = 'deleted'` by delete tools. `event = 'projection_written'` by `safe_write` on success. Documented in each kind's schema file and enforced by tests.

Round-3 revisions:

21. **Evaluate the official `mcp` Python SDK before hand-rolling the JSON-RPC loop** (DeepSeek #8). Phase 1a.2 is now a decision point, not a copy-extract. If the SDK supports stdio transport cleanly, adopt it; if it imposes more friction than it removes, document the rationale and extract the hand-rolled loop from the existing servers. **Superseded by Plan 004:** MCP transport removed entirely; CLI + skills replace the JSON-RPC surface.
22. **Sync everything.** The MCP loop is stdin-blocking sync (async stdin gains nothing for our throughput); `core/db.py` uses `psycopg_pool.ConnectionPool` (the sync variant), not `AsyncConnectionPool` (DeepSeek #9). Pool sized `min_size=2`, `max_size=5` per process (GLM #6) — agents are single-threaded callers; the trigger now also writes to `change_log` so each tool call uses 1–2 connections. Implemented in Phase 1a.4, not deferred — both legacy servers open/close per operation today, which is the wrong baseline.
23. **Critical vocabulary attributes are first-class columns** (DeepSeek #6, partial). `is_terminal BOOLEAN NOT NULL DEFAULT FALSE`, `is_open BOOLEAN NOT NULL DEFAULT TRUE`, `sort_order INTEGER NOT NULL DEFAULT 100` on `vocabularies`. Triggers read columns, not JSONB. Free-form `attributes JSONB` stays for genuinely flexible per-kind extras (e.g., `identifier_format`). **Schema-evolution convention (Kimi round-4):** attributes used by triggers or core queries get columns (paid via `ALTER TABLE` when added); presentation-only metadata stays in `attributes JSONB`. Documented in the core README so future contributors don't bury load-bearing logic in JSONB.
24. **Projection columns are opt-in per kind** (DeepSeek #5, partial). Only kinds that participate in user-editable markdown projection carry `projection_sha256` and `projection_dirty`. Breadcrumbs do; memories don't; reflections TBD. The hash-check apparatus is justified for breadcrumbs (humans edit them by hand) but unnecessary elsewhere.
25. **Reflections start as a memory experiment, not a dedicated server** (DeepSeek #10, partial). Phase 5 demoted from "build reflections-mcp" to "spike: store reflections as memories with `memory_type='reflection'` plus JSONB metadata; if schema strains or query patterns get ugly, then build the dedicated server." Saves 300 lines + a schema + a binary if memories suffice.
26. **Embedding call precedes the DB transaction** (DeepSeek #11). Order: embed → `BEGIN` → insert with vector → insert `change_log` row → `COMMIT`. Embedding is a pure function and recomputable; doing it outside the transaction means failure aborts before any DB writes. With decision 2 reversed (no sidecar), the call is in-process and synchronous — no leaked-embedding concern at all.

## 3. Repository layout

```
agent-notes-mcp/
  pyproject.toml
  README.md
  src/agent_notes/
    __init__.py
    core/
      __init__.py
      mcp.py                  # JSON-RPC main loop, _send/_ok/_err, ToolRegistry
      server.py               # Server base class: register_tool, register_resource, run()
      db.py                   # psycopg_pool, workspaces, projects, vocabularies CRUD
      embed.py                # in-process lazy singleton; thread-safe; decision 2
      projection.py           # frontmatter parse/write, render_index, hash-check
      links.py                # generic links + kind-local trace_graph CTE
      notify.py               # LISTEN/NOTIFY helpers
      resources.py            # MCP resources/list + resources/read scaffolding
      change_log.py           # write helpers + changes_since query
    servers/
      breadcrumbs.py
      memory.py
      reflections.py          # Phase 5
      search.py               # cross-kind UNION ALL + trace_graph_all (Phase 4)
    schema/
      000_core.sql
      100_breadcrumbs.sql
      200_memory.sql
      300_reflections.sql
  scripts/
    setup_db.py
    import_legacy_bc.py
    import_legacy_memory.py
    regenerate_markdown.py
    migrate.py
  tests/
  plans/
    001-architecture-and-implementation.md   (this file)
```

`pyproject.toml` exposes:

```toml
[project.scripts]
# One generic binary; --kind selects which registry is mounted. The per-kind
# names below are thin shims that call `serve(kinds=[...])` so harness configs
# stay ergonomic (Claude/OpenCode/Gemini register a binary per server). This
# also IS the omnibus capability — invoke `agent-notes serve --kinds bc,memory`
# to mount multiple kinds in one process (decision 12). GLM #7 simplification.
agent-notes             = "agent_notes.cli:main"
agent-notes-breadcrumbs = "agent_notes.cli:main_breadcrumbs"   # serve(['breadcrumbs'])
agent-notes-memory      = "agent_notes.cli:main_memory"        # serve(['memory'])
agent-notes-search      = "agent_notes.cli:main_search"        # serve(['search'])
agent-notes-setup       = "agent_notes.scripts.setup_db:main"
agent-notes-migrate     = "agent_notes.scripts.migrate:main"
# agent-notes-reflections shim added only if the Phase 3 spike (moved earlier
# per GLM #9, formerly Phase 5; decision 25) concludes a dedicated server is
# needed; otherwise reflections are stored via agent-notes-memory with
# memory_type='reflection'.
```

## 4. Schema

### 4.1 Core (shared)

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE workspaces (
    id   SERIAL PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE projects (
    id              SERIAL PRIMARY KEY,
    workspace_id    INTEGER NOT NULL REFERENCES workspaces(id),
    slug            TEXT    NOT NULL,
    name            TEXT    NOT NULL,
    repo_root       TEXT,   -- e.g. '/projects/substrate' — restores the dropped
                            -- repo_path field per Kimi round-5 #3; needed so the
                            -- /end skill can compute repo-relative paths for git
    breadcrumbs_dir TEXT,   -- repo-relative or absolute prefix where BC files live,
                            -- e.g. 'breadcrumbs' or '/projects/substrate/breadcrumbs'
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, slug)
);

CREATE TABLE vocabularies (
    workspace_id   INTEGER NOT NULL REFERENCES workspaces(id),
    kind_namespace TEXT    NOT NULL,    -- 'bc_kind', 'bc_status', 'bc_severity',
                                        -- 'memory_type', 'link_relationship', ...
    name           TEXT    NOT NULL,
    -- Critical, frequently-queried attributes get first-class columns
    -- (decision 23) so triggers don't reach into JSONB:
    is_terminal    BOOLEAN NOT NULL DEFAULT FALSE,
    is_open        BOOLEAN NOT NULL DEFAULT TRUE,
    sort_order     INTEGER NOT NULL DEFAULT 100,
    archived       BOOLEAN NOT NULL DEFAULT FALSE,
    -- Free-form bag for genuinely flexible extras (e.g. identifier_format):
    attributes     JSONB   NOT NULL DEFAULT '{}',
    PRIMARY KEY (workspace_id, kind_namespace, name)
);

-- Generic cross-kind link table. No FKs to kind tables; dangling rows accepted
-- and filtered at query time (see §10 risks). All kinds are project-scoped
-- (decision 13), so from_project/to_project are NOT NULL and participate in the
-- PK, preventing identifier collisions across projects (decision 14).
CREATE TABLE links (
    from_kind        TEXT    NOT NULL,
    from_workspace   INTEGER NOT NULL,
    from_project     INTEGER NOT NULL,
    from_identifier  TEXT    NOT NULL,
    to_kind          TEXT    NOT NULL,
    to_workspace     INTEGER NOT NULL,
    to_project       INTEGER NOT NULL,
    to_identifier    TEXT    NOT NULL,
    relationship     TEXT    NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (from_kind, from_workspace, from_project, from_identifier,
                 to_kind,   to_workspace,   to_project,   to_identifier,
                 relationship)
);
-- Index column order optimized for kind-local trace_graph traversal
-- (Kimi round-3 #3): narrow to the exact node first, then filter by
-- relationship. The composite PK already enforces uniqueness.
CREATE INDEX idx_links_from ON links (from_kind, from_workspace, from_project, from_identifier, relationship);
CREATE INDEX idx_links_to   ON links (to_kind,   to_workspace,   to_project,   to_identifier,   relationship);

-- Unified audit log. Single source for changes_since, NOTIFY, history tools.
-- Designed for future RANGE partitioning by changed_at if volume warrants.
-- project_id intentionally nullable (Kimi round-3 #1): workspace-level events
-- like 'vocabulary_added' or 'workspace_created' legitimately have no project.
-- All kind-row events carry a non-null project_id.
CREATE TABLE change_log (
    id            BIGSERIAL PRIMARY KEY,
    kind          TEXT NOT NULL,
    workspace_id  INTEGER NOT NULL,
    project_id    INTEGER,
    identifier    TEXT NOT NULL,
    event         TEXT NOT NULL,  -- 'filed' | 'updated' | 'status_changed'
                                  -- | 'deleted' | 'projection_written'
    payload       JSONB NOT NULL DEFAULT '{}',
    actor         TEXT,
    changed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_change_log_recent ON change_log (changed_at DESC);
CREATE INDEX idx_change_log_kind   ON change_log (kind, changed_at DESC);
CREATE INDEX idx_change_log_target ON change_log (kind, workspace_id, project_id, identifier);

CREATE OR REPLACE FUNCTION change_log_notify_fn() RETURNS TRIGGER AS $$
BEGIN
    PERFORM pg_notify('agent_notes_changes',
        json_build_object(
            'kind', NEW.kind, 'event', NEW.event,
            'workspace_id', NEW.workspace_id,
            'project_id', NEW.project_id,
            'identifier', NEW.identifier
        )::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER change_log_notify
    AFTER INSERT ON change_log
    FOR EACH ROW EXECUTE FUNCTION change_log_notify_fn();
```

### 4.2 Breadcrumbs (kind-specific)

- `breadcrumbs` keyed by `(project_id, identifier)`, vector column dim pinned by `EMBED_DIM` env (server refuses to start on mismatch).
- `kind`, `status`, `severity` are composite FKs into `vocabularies`.
- `external_refs JSONB` replaces `filed_in_run`/`closed_in_run`.
- `diagnostic_keys JSONB` retained — load-bearing differentiator.
- `frontmatter_version SMALLINT NOT NULL DEFAULT 1`.
- `projection_sha256 BYTEA`, `projection_dirty BOOLEAN NOT NULL DEFAULT FALSE` per decision 17.
- `file_path TEXT` — canonical projection path **relative to `projects.breadcrumbs_dir`** (Kimi round-3 #4). Path composition (Kimi round-5 #3 — needs `repo_root` to compute repo-relative paths):
  - `absolute = repo_root + breadcrumbs_dir + file_path`
  - `repo_relative = breadcrumbs_dir + file_path` (relative to repo root, for `git mv`)
  e.g. `repo_root='/projects/substrate'`, `breadcrumbs_dir='breadcrumbs'`, `file_path='resolved/RFC-031.md'` → absolute `/projects/substrate/breadcrumbs/resolved/RFC-031.md`, repo-relative `breadcrumbs/resolved/RFC-031.md`. Status transitions update `file_path`; the `/end` skill performs the actual `git mv` and commit (decision 15).
- **`file_breadcrumb` writes `event='filed'` to `change_log` in the same transaction as the row insert.** **Status trigger writes `event='status_changed'`** and reads `is_terminal` from the first-class `vocabularies.is_terminal` column (Kimi round-5 #1; decision 23 promoted it out of JSONB precisely so triggers don't reach into JSONB) to maintain `closed_at`. `update_breadcrumb` writes `event='updated'`. No per-kind history table.

### 4.3 Memory (kind-specific)

- `memories` row PK is the surrogate `id BIGSERIAL`; name uniqueness is enforced via partial unique index on active rows only (see below). Project-scoped per decision 13; cross-cutting memories live in a conventional `global` project per workspace, created by `agent-notes-setup`.
- `active BOOLEAN NOT NULL DEFAULT TRUE` (Kimi round-5 #2). Soft-delete and supersede both flip `active=false` on the old row and insert a new active row. Partial unique index:
  ```sql
  CREATE UNIQUE INDEX memories_name_active_unique
      ON memories (project_id, name) WHERE active = true;
  ```
  This makes `list_memories` and `get_memory` cheap (filter `WHERE active`) without scanning revision history. Without this column, "fast active-version lookup" doesn't work.
- `memory_type` FK into `vocabularies`.
- `supersedes BIGINT REFERENCES memories(id)` for the revision chain; also mirrored into `links` (kind=memory, relationship=supersedes) for `trace_graph`.
- `[[name]]` references in bodies parsed into `relates_to` link rows (skip fenced code spans). The link's `to_project` defaults to the source memory's project; cross-project links use the explicit `add_link` tool.
- `add_memory` writes `event='filed'` to `change_log`; updates write `event='updated'`; soft delete writes `event='deleted'`.
- **No `projection_sha256` / `projection_dirty` columns** — memories don't project to disk today (decision 24, Kimi round-4 catch). If memory projection ever becomes desirable, the two columns are a trivial `ALTER TABLE`.

### 4.4 Reflections (Phase 5)

- `reflections` keyed by `(project_id, slug)` where slug is `YYYY-MM-DD-<model>`. Project-scoped per decision 13.
- **Body and section JSONB are append-only**; metadata fields (`gaps_extracted_at`, `gaps_filed_as TEXT[]`, `replaced_by`) are mutable (decision 19).
- Freshness via `replaced_by` link in the generic `links` table.
- `projection_sha256` / `projection_dirty` per decision 17.
- `add_reflection` writes `event='filed'` to `change_log`; `mark_gaps_filed` writes `event='updated'`.

## 5. Embedding (in-process, lazy singleton)

Reversed from v1.1 in v1.3 per DeepSeek #2 — see decision 2.

`core/embed.py` exposes `embed(text, task='document' | 'query') -> np.ndarray`. First call loads `nomic-embed-text-v1.5` (~270MB, ~1.5s); thread-safe double-checked-locking singleton keeps it resident for the process lifetime. Identical pattern to the existing two servers' `embed.py`.

`AGENT_NOTES_EMBED_MODEL` and `AGENT_NOTES_EMBED_DIM` env vars allow swapping models for experiments (MiniLM-384, BGE-large-1024). Server refuses to start if the configured dim doesn't match the column type (read via `information_schema`).

If running many simultaneous server processes ever becomes painful, fall back to the `agent-notes-omnibus` binary (decision 12, Phase 6.2) before reaching for a sidecar.

**Call ordering vs DB transactions (decision 26):** always embed first, outside the transaction. Embedding is a pure function; if it fails, never open the transaction. If the transaction later rolls back, the embedding bytes are simply discarded. No leak, no recovery code.

## 6. MCP tool surface

`core.server.Server` provides:
- `register_tool(name, schema, fn)` and `register_resource_handler(uri_prefix, fn)` (composable into a future omnibus server, per design decision 12).
- Auto-wired `initialize`, `notifications/initialized`, `tools/list`, `tools/call`, `resources/list`, `resources/read`, `ping`.
- Cross-kind helpers from core: `list_workspaces`, `list_projects`, `list_vocabulary`, `archive_vocabulary`, `changes_since`, `history(kind, ws, project, identifier)`, `add_link`, `remove_link` (per decision 16 — every kind server exposes link mutation so an agent connected to e.g. breadcrumbs can link a BC to a memory without switching servers).

### Breadcrumbs server

`file_breadcrumb`, `update_breadcrumb`, `query_breadcrumbs`, `find_breadcrumbs`, `get_breadcrumb`, `suggest_duplicates`, `diagnose`, `trace_graph` (kind-local), `render_index`, `audit`, `export_project`, `reconcile_projection`, `compute_projection_paths(identifier, target_status) -> {old_absolute, new_absolute, old_repo_relative, new_repo_relative}` (Kimi round-4: the `/end` skill needs repo-relative paths for `git mv`; `safe_write` needs absolute paths; return both so the skill author never has to compose them).

Removed: `file_path` parameter on `file_breadcrumb`. Derived from `projects.breadcrumbs_dir`. Server needs write access to the path; failure sets `projection_dirty` and returns a clear tool error. Status transitions update `file_path` in the DB to the new canonical path; the `/end` skill performs the `git mv` and commit.

### Memory server

`add_memory` (requires `project`, no default), `search_memory` (body-elided by default; `include_body: true` opt-in), `list_memories`, `get_memory`, `delete_memory`, `trace_graph` (kind-local). Inherits `add_link`/`remove_link` from core.

### Search server (Phase 4)

- `search_all_notes(query, kinds?, workspaces?, projects?, since?)` — UNION ALL across kind tables ranked by cosine similarity. Returns name/title/kind/score; clients fetch full bodies through kind-specific tools.
- `trace_graph_all(from_kind, ws, project, identifier, direction, max_depth)` — cross-kind traversal via LATERAL UNION ALL against per-kind row sources. Acknowledged-slower path; isolated to this server so callers explicitly opt in.
- View `all_notes_search_v` exposes the common columns only (`kind`, `workspace_id`, `project_id`, `identifier`, `title`, `body`, `embedding`, `updated_at`). Explicitly **search-only** (Kimi D); kind-specific fields fetched via kind tools.

### Reflections server (Phase 5)

`add_reflection`, `find_reflections`, `get_reflection`, `extract_gaps`, `mark_gaps_filed`.

## 7. Markdown projection

`core/projection.py` primitives: `parse_frontmatter`, `render_frontmatter`, `render_index`, `slugify`, `safe_write(path, content, expected_sha256) -> Result`.

**Hash-check before overwrite (Gemini 1).** `safe_write` reads the existing file, computes SHA-256, compares to `projection_sha256` on the DB row:

| Existing file state | Action |
|---|---|
| Matches recorded hash | Write new content; update `projection_sha256` |
| Bytes-equal to new content | Skip write (idempotent re-run) |
| Mismatches recorded hash | Refuse; set `projection_dirty=true`; surface `reconcile_projection` prompt listing both versions |

**Eventual-consistency stance (Kimi minor flag).** DB commits first, files written second. On file-write failure the row stays committed with `projection_dirty=true`; `audit` reports it; `render_index` re-attempts. Perfect atomicity isn't the goal — re-runnability is.

## 8. Migration from existing servers

One-shot ETL:

1. `agent-notes-setup` creates `agent_notes` DB with core + per-kind schemas.
2. `import_legacy_bc.py` reads existing breadcrumb-mcp DSN, creates `default` workspace, recreates projects, seeds vocabularies from observed values **with `is_terminal` / `is_open` / `sort_order` columns populated correctly** (Gemini round-2 #3), copies BCs, replays status history into `change_log`, recreates relationships in `links`. Re-embeds in-process (~250 rows, ~1 minute). **Disables `change_log_notify` trigger around the bulk insert** (Kimi round-3 #2) so listeners don't see hundreds of NOTIFY payloads during a one-shot import; re-enables on completion.
3. `import_legacy_memory.py` same for memory-mcp; `supersedes` mirrored into `links`.
4. `regenerate_markdown.py` writes canonical frontmatter-v1 files for each project's `breadcrumbs/` dir. Sets `projection_sha256` after each write. One "frontmatter v1 normalize" commit per repo (human-reviewed first).
5. Update harness configs to point at new binaries + single `AGENT_NOTES_DSN`.
6. After one week of clean operation, archive `breadcrumb-mcp/` and `memory-mcp/`.

## 9. Phased implementation

Phases re-scoped per Kimi A (Phase 1 was oversized). Each sub-phase is a 4–8 hour Sonnet dispatch; sub-phases within a phase can parallelize where noted.

### Phase 0 — Legacy prep (small, dispatchable today)

| # | Task | Outcome |
|---|---|---|
| 0.1 | ~~Fix `valid_kinds` / JSON-schema enum mismatch~~ — **verified false alarm**: `valid_kinds` set (server.py:190), three JSON-schema enums (lines 533, 564, 600), and the SQL `CHECK` constraint all consistently list 15 kinds including `job`. GLM miscounted. No fix required. | Verified inline 2026-05-20; no PR needed |
| 0.2 | Audit existing `memory-mcp/plans/001-generalization-and-link-graph.md` (superseded but its sf2-default and history-tool observations are still load-bearing) (GLM #12). **Done 2026-05-20**: load-bearing observations are (a) `tool_add_memory` line 54 hardcodes `project="sf2"` default — Phase 3.2 drops it; (b) `supersedes` chain is recorded but invisible without a `history` tool — `history` is now a core helper (decision 16); (c) `[[name]]` link parsing isn't implemented — Phase 3.3 adds it. Import script (Phase 3.5) treats sf2-tagged memories as project=sf2 and skips the "global" fallback unless explicit. | Notes captured for Phase 3.2 / 3.5 |

### Phase 1a — Repo skeleton + core protocol layer

| # | Task | Outcome |
|---|---|---|
| 1a.1 | Init repo: `pyproject.toml`, src layout, ruff/pytest config, **`README.md` linking to this plan, `AGENTS.md` (substrate/sf2 convention) with build/test/lint commands, `.gitignore`** (GLM #10) | `uv pip install -e .` succeeds; new contributor can read AGENTS.md and run `make test` |
| 1a.2 | **Evaluate official `mcp` Python SDK** (decision 21); if it supports stdio transport cleanly, build `core/mcp.py` on top of it; otherwise extract the hand-rolled JSON-RPC loop from the existing servers with a one-paragraph rationale | Either an SDK-backed loop OR a documented hand-rolled one; unit tests pass |
| 1a.3 | `core/server.py`: `Server` base class with class-registry tools (composable, design 12) | A stub server answers `tools/list`/`tools/call` |
| 1a.4 | `core/db.py`: `psycopg_pool.ConnectionPool` (sync, decision 22) singleton + workspaces/projects/vocabularies CRUD with reference-checked deletes | Tests pass against ephemeral Postgres |
| 1a.5 | `schema/000_core.sql` + numbered `schema/*.sql` migration runner (`agent-notes-migrate --all`); `agent-notes-setup` is a thin alias per decision 18 | Idempotent setup; re-run is no-op; single source of DDL truth |

### Phase 1b — Core infra modules

1b.1/1b.2/1b.3 parallelize across two Sonnet dispatches.

| # | Task | Outcome |
|---|---|---|
| 1b.1 | `core/embed.py` in-process lazy singleton (decision 2/5) extracted from the existing servers' `embed.py` (essentially copy-paste; ~45 lines) | First call loads model; subsequent calls cached; thread-safe |
| 1b.2 | `core/projection.py` (frontmatter parse/write/render_index + `safe_write` hash-check for opt-in kinds per decision 24) | Round-trips existing BC files byte-identical; hash mismatch refuses write |
| 1b.3 | `core/links.py` (generic links + kind-local `trace_graph` CTE) | Mirrors current bc-mcp behavior for single-kind traversal |
| 1b.4 | `core/notify.py` + `core/change_log.py` | LISTEN test sees payload after change_log insert |
| 1b.5 | `core/resources.py` + `resources/list`/`read` scaffold | Stub kind exposes a resource by URI |
| 1b.6 | **Test infrastructure** (GLM #5): testcontainers or Docker Compose for ephemeral Postgres in CI; integration-test harness for every core module; document the convention in `AGENTS.md` (no mocked DB unit tests for trigger/CTE logic — those must run against real Postgres) | `make test` spins up a fresh PG, runs all tests, tears down |

### Phase 2a — Breadcrumbs server (DB only, no projection)

| # | Task | Outcome |
|---|---|---|
| 2a.1 | `schema/100_breadcrumbs.sql`: bc table, vocab FKs, HNSW index, status trigger writing to `change_log` | Migration runs; trigger fires on status change |
| 2a.2 | `servers/breadcrumbs.py` composing core + bc tools (no file writes yet) | All 12 current bc tools work against new schema |
| 2a.3 | `import_legacy_bc.py`: import BCs via `COPY` (not per-row INSERT) for speed; **insert `change_log` rows in a single batch INSERT** (GLM #4) so even with the trigger disabled per K3.2 we don't pay 250 round trips; seed vocabularies with `is_terminal`/`is_open`/`sort_order` columns populated (Gemini round 2 #3); **normalize legacy `file_path` values — strip any absolute prefix down to repo-relative-under-`breadcrumbs_dir` form** (DeepSeek round-4 #3) so `safe_write` and the `/end` skill compose paths identically | BCs imported in seconds, not minutes; vocabularies correct; all `file_path` values are clean relative paths like `resolved/RFC-031.md` |
| 2a.4 | Integration tests | `make test` green |

### Phase 2b — Breadcrumbs projection + skill update

| # | Task | Outcome |
|---|---|---|
| 2b.1 | `file_breadcrumb`: embed text (decision 26, outside txn) → `BEGIN` → insert breadcrumb + change_log row → `COMMIT` → `safe_write` markdown; FS-write failure sets `projection_dirty=true` (eventual consistency) | DB and disk consistent on success; dirty flag on FS failure; embedding never leaked because it precedes the txn |
| 2b.2 | `update_breadcrumb` re-writes file at the current `file_path`; on terminal-status transitions it **updates `file_path` in the DB** to the canonical resolved path (e.g., `breadcrumbs/resolved/RFC-031.md`) but does not move the file or invoke git (decision 15). Adds `compute_projection_paths` helper for the `/end` skill | DB-side path update verified; server never shells out to git |
| 2b.2b | Update `/end` skill to perform the `git mv` from the old to new `file_path` and commit, using `compute_projection_paths` | `/end` handles all git operations; server has no git dependency |
| 2b.3 | `render_index`, `audit`, `reconcile_projection` tools | `audit` reports zero drift after `render_index`; reconcile surfaces hash mismatches |
| 2b.4 | `regenerate_markdown.py` + one-time normalize commit per repo (human-reviewed first) | sf2/substrate/sf1 each get one "frontmatter v1 normalize" commit |
| 2b.5 | Update `/end` skill | New sessions auto-regenerate cleanly |

### Phase 3 — Memory server

| # | Task | Outcome |
|---|---|---|
| 3.1 | `schema/200_memory.sql`: memories with surrogate `id BIGSERIAL`, partial unique index on `(project_id, name) WHERE active`, `active` column, `supersedes BIGINT REFERENCES memories(id)` (Kimi round-5 #2); `agent-notes-setup` creates a `global` project per workspace for cross-cutting memories (decision 13) | Migration runs; `global` project exists; partial unique index in place |
| 3.2 | `servers/memory.py` thin server; drop `project` default — require explicit project (use `global` for cross-cutting); confirm `list_projects` is in `tools/list` early so agents can self-scope (GLM #3) | Existing 5 tools work; `history` and `add_link`/`remove_link` from core work |
| 3.3 | `[[name]]` parser auto-creates `relates_to` links (skip code spans) | Body with `[[foo]]` produces link row |
| 3.4 | `search_memory` body-elision default; `include_body` opt-in | Token cost measurably lower in real session |
| 3.5 | `import_legacy_memory.py` (account for the sf2 hardcoded default per GLM #12) | Memories migrated; supersedes mirrored to `links` |
| 3.6 | **Reflections spike (moved from Phase 5 per GLM #9):** store one historical reflection as a memory with `memory_type='reflection'` and structured sections + gaps in JSONB. Run `find_reflections`-shaped queries against the memory server. Decide: memories sufficient OR build dedicated server? | Decision artifact in writing; if dedicated server, Phase 5 stays in scope |
| 3.7 | Decide harness reconciliation (§11) | Documented or implemented |
| 3.8 | Tests | green |

### Phase 4 — Cross-kind search server

| # | Task | Outcome |
|---|---|---|
| 4.1 | `servers/search.py` with `search_all_notes` (UNION ALL) | Merged ranked results across bc + memory |
| 4.2 | `trace_graph_all` cross-kind traversal | Acknowledged-slower path; works for small graphs |
| 4.3 | `all_notes_search_v` view (search-only, common columns) | Ad-hoc SQL search is one statement |
| 4.4 | `/start` skill calls `search_all_notes` with current focus | Session orientation surfaces prior art across kinds |
| 4.5 | Tests | green |

### Phase 5 — Reflections (conditional; spike now lives in Phase 3.6)

The spike moved to Phase 3.6 (GLM #9) so any schema strain on memories surfaces while the memory schema is still being built. This phase exists only if 3.6 concluded a dedicated server is needed.

| # | Task | Outcome |
|---|---|---|
| 5.1 | If memories suffice (Phase 3.6 outcome): seed `memory_type='reflection'` in vocab; update `reflect` skill to call `add_memory` with the new type; import historical `reflections/*.md` as memories | `reflect` skill works against memory server; old reflections searchable |
| 5.2 | If memories don't suffice: build dedicated reflections server (schema 300_reflections.sql, server, extract_gaps, mark_gaps_filed) | Dedicated server with the gaps pipeline |
| 5.3 | Either way: add `extract_gaps` tool that parses the "Gaps to flag" section and proposes BC drafts | Agent confirms then calls `file_breadcrumb` |

### Phase 6 — Polish

| # | Task | Outcome |
|---|---|---|
| 6.1 | MCP `resources/` surface for all kinds (`note://kind/workspace/project/identifier`) | Dashboard MCP clients can fetch by URI |
| 6.2 | Optional `agent-notes-omnibus` entry point that mounts all kind registries in one process | Single-process deployment available if needed (Gemini 2) |
| 6.3 | `agent-notes-doctor` script: checks DSN, **embedding-model load (~1.5s on first call, reports failure clearly)**, `breadcrumbs_dir` write access, runs `links_audit` and reports dangling-link counts per kind (Gemini round 2 #2) | Surfaces install-time problems and link-graph hygiene |
| 6.4 | Archive `breadcrumb-mcp/` and `memory-mcp/` with README pointers | Legacy repos redirect cleanly |

## 10. Risks and mitigations

- **Process count + embedding footprint** (Gemini 2 + DeepSeek 2 + DeepSeek round-4 #4): 4 procs × ~200MB baseline + 4 × 270MB embedding model = ~1.9GB resident if all servers run concurrently. Honest tradeoff acknowledgement: dropping the sidecar swapped a network SPOF for ~810MB of duplicated model weights vs the sidecar's single ~270MB load. On a 64GB homelab this is fine; on a 16GB laptop sharing memory with the editor, browsers, and an LLM tool, it's painful. Mitigation: omnibus binary (decision 12, Phase 6.2) collapses to 1 proc with 1 model load, restoring sidecar-grade footprint without the network dependency. **The README should recommend omnibus as the default for <32GB RAM users** (Kimi round-4).
- **Links composite PK is verbose for ad-hoc queries** (GLM #8): the 9-column PK is the right schema choice (see §12 rejection table for D4), but ad-hoc `SELECT * FROM links WHERE id = ?` ergonomics are nice. If implementation surfaces real pain, add `id BIGSERIAL UNIQUE` as an *alternate* key (not replacing the natural PK). Deferred — don't pay until needed.
- **Per-kind isolation of audit/NOTIFY** (DeepSeek round-4 re-litigation of D7): a bug in one kind server's log-writing path floods `change_log` for all kinds and fires NOTIFY for all listeners. Acknowledged-but-accepted: per-kind history tables would limit blast radius at the cost of duplicated trigger code, UNION-based `changes_since`, and per-kind NOTIFY channels. Mitigation if it ever bites: listeners debounce; the change_log table is RANGE-partitionable by `changed_at` (already noted); offending kind can be quiesced via feature flag without restarting other servers. Splitting to per-kind tables is a future migration, not a day-1 cost.
- ~~Sidecar SPOF~~ — withdrawn in v1.3 with the sidecar itself (decisions 2/11 reversed).
- **Single DB blast radius**: a bad migration takes down all kinds. Mitigated by versioned `migrate.py`, never-destructive migrations, staging dry-runs once content matters.
- **`agent_notes.core` API stability**: three servers depend on one library. Mitigated by small public surface (≤15 symbols), documented semver intent, batched breaking changes.
- **Vocabulary invalidation / "ghost status"** (Gemini 3): handled by reference-checked deletes plus `archive_vocabulary` for soft deprecation.
- **Dangling links** (Kimi minor): `links` has no FKs to kind tables for cross-kind flexibility. Policy: filter at query time (`trace_graph` joins back to the kind table and skips missing targets); a periodic `links_audit` script reports orphan counts. Cheaper than per-kind cascade triggers; matches harness file-memory's broken-link semantics.
- **Link traversal performance at scale** (Gemini 4): indexes on `(from_kind, relationship, from_workspace)` and the mirror. `change_log` and `links` designed for future RANGE partitioning by timestamp if volume warrants — not done day one.
- **Human-edits-then-clobber** (Gemini 1): handled by `safe_write` hash check (design 8). `reconcile_projection` surfaces both versions.
- **DB+FS atomicity is eventual** (Kimi minor): explicit in §7. `projection_dirty=true` is the recovery point; `audit` reports it; `render_index` re-attempts.
- **Filesystem write permissions** (Kimi minor): MCP server needs write access to each project's `breadcrumbs_dir`. Documented; `agent-notes-doctor` checks at install time (Phase 6.3).
- **Import re-embedding cost**: one-time ~1 minute in-process. Migration script idempotent.

## 11. Open question — harness file-memory reconciliation

Unchanged from prior plan:

1. **Document the boundary.** Harness = this-instance, memory server = cross-agent. De-facto today.
2. **Bridge tool.** `import_harness_memory` pulls `MEMORY.md` periodically. One-way sync.
3. **Memory server as harness backend.** Configure harness to write through MCP. Requires harness changes; biggest payoff.

Decide between (1) and (2) during Phase 3; defer (3) until friction surfaces. Doesn't block any phase.

## 12. Peer-review revision summary

Changes from v1.0:

| # | Concern | Source | Resolution |
|---|---|---|---|
| A | Phases too big | Kimi | Phase 1 split into 1a/1b; Phase 2 split into 2a/2b |
| B | Cross-kind `trace_graph` performance | Kimi | Split: kind-local `trace_graph` in core; cross-kind `trace_graph_all` in search server (Phase 4) |
| C | `change_log` vs per-kind history confusion | Kimi | `change_log` is sole audit source; status trigger writes to it directly |
| D | `all_notes_v` underspecified | Kimi | Renamed `all_notes_search_v`; explicit "search-only" scope |
| 1 | Human edits silently overwritten | Gemini | `safe_write` hash-check + `reconcile_projection` tool |
| 2 | 5-process overhead | Gemini | `Server` class composable; future `agent-notes-omnibus` opt-in entry point (Phase 6.2) |
| 3 | Vocabulary invalidation | Gemini | Reference-checked deletes + `archive_vocabulary` |
| 4 | Link traversal scale | Gemini | Indexes on `(kind, relationship, workspace)` both directions; partitioning called out as future option |
| 5 | Sidecar blip noise | Gemini | `core/embed.py` retries 3× / ≤500ms before surfacing failure |
| — | Naming | Both | Committed: `agent-notes-mcp` / `agent_notes` |
| — | FS permissions | Kimi | Documented; `agent-notes-doctor` in Phase 6.3 |
| — | DB+FS atomicity | Kimi | Eventual consistency documented in §7 |
| — | Dangling links | Kimi | Filter at query; periodic audit; documented in §10 |

**Round 2 (v1.2):**

| # | Concern | Source | Resolution |
|---|---|---|---|
| K1 | `links` PK omitted project; nullable-FK PK invalid | Kimi | Memories now project-scoped (decision 13); links use natural composite PK with `NOT NULL` project columns (decision 14) |
| K2 | `add_link`/`remove_link` missing from tool surface | Kimi | Promoted to core helpers exposed by every kind server (decision 16); §6 updated |
| K3 | `git mv` inside the MCP server | Kimi | Server updates `file_path` DB column only; `/end` skill performs git ops; new `compute_projection_paths` helper tool; Phase 2b.2 / 2b.2b reflect the split (decision 15) |
| K4 | Memory project scoping was ambiguous | Kimi | Project-scoped, with conventional `global` project per workspace for cross-cutting memories (decision 13) |
| K5.1 | Projection columns are a per-table convention | Kimi | Documented as core convention (decision 17); applies to all projecting kinds |
| K5.2 | `setup` vs `migrate` double-maintenance | Kimi | `setup` is `migrate --all` against empty DB; numbered `schema/*.sql` is sole DDL source (decision 18); Phase 1a.5 reflects this |
| K5.3 | Reflection append-only conflicted with `mark_gaps_filed` mutation | Kimi | Body + section JSONB append-only; metadata mutable (decision 19); §4.4 updated |
| K5.4 | `change_log` event ownership unclear | Kimi | Explicit owner per event type (decision 20); kind tools write `filed`/`updated`/`deleted`; triggers write `status_changed`; `safe_write` writes `projection_written` |
| G1 | Stale `src/mvp_mcp/` directory after rename | Gemini | Renamed to `src/agent_notes/` as the first concrete action |
| G2 | `links_audit` robustness | Gemini | Folded into `agent-notes-doctor` (Phase 6.3) so it's runnable as a health check |
| G3 | Vocabulary import must seed `attributes` (e.g., `is_terminal`) | Gemini | Phase 2a.3 explicitly populates attributes during legacy import so status triggers work correctly |

**Round 3 (v1.3):**

| # | Concern | Source | Resolution |
|---|---|---|---|
| K3.1 | `change_log.project_id` nullable | Kimi | Kept nullable; documented in §4.1 as intentional for workspace-level events |
| K3.2 | NOTIFY flood on bulk import | Kimi | §8 import script disables `change_log_notify` trigger during bulk insert |
| K3.3 | `links` index order wrong for traversal | Kimi | Reordered so node-identifier columns precede `relationship`; PK still enforces uniqueness |
| K3.4 | `file_path` relative vs absolute ambiguous | Kimi | Clarified in §4.2: `file_path` is relative under `projects.breadcrumbs_dir` |
| D2 | Sidecar adds complexity, not removes it | DeepSeek | **Reversed v1.1 decision.** In-process lazy singleton; sidecar dropped. §5 rewritten; decisions 2 and 11 updated |
| D5 | Projection apparatus too heavy | DeepSeek (partial) | Scoped projection columns to opt-in kinds (decision 24); kept hash-check for breadcrumbs (load-bearing for human-edit detection); trimmed surrounding apparatus |
| D6 | Vocabularies-as-data premature | DeepSeek (partial) | Kept per-project vocab (substrate ≠ sf2 today, not speculative) but promoted `is_terminal` / `is_open` / `sort_order` to first-class columns (decision 23) — triggers no longer reach into JSONB |
| D8 | No MCP SDK | DeepSeek | Phase 1a.2 now evaluates the official `mcp` Python SDK and either adopts it or documents why not (decision 21) |
| D9 | async vs sync ambiguity | DeepSeek | Sync everything: stdin loop sync, `psycopg_pool.ConnectionPool` sync (decision 22) |
| D10 | Reflections may not need their own server | DeepSeek (partial) | Phase 5 demoted to spike-first: try storing reflections as memories with `memory_type='reflection'`; build dedicated server only if memories strain (decision 25) |
| D11 | Embedding txn ordering unclear | DeepSeek | Documented call order in decision 26 and Phase 2b.1: embed → BEGIN → insert → COMMIT → safe_write. With sidecar removed, no leak risk |

**DeepSeek items explicitly rejected, with rationale:**

| # | DeepSeek concern | Rejection reason |
|---|---|---|
| D3 | Add per-kind FK triggers on `links` for referential integrity | Per-kind dispatch trigger reintroduces the kind-table coupling we just eliminated; every new kind would require trigger updates. We control all writers; dangling links are bounded by our own bugs, not adversarial input. Audit script (Phase 6.3) is sufficient |
| D4 | Replace 9-column natural composite PK with `BIGSERIAL` surrogate | Surrogate adds 8 bytes/row + a separate unique index (same storage), and forces callers to remember opaque IDs for `remove_link`. Composite PK lets `add_link`/`remove_link` take symmetric natural-key arguments. Style preference, not technical improvement |
| D7 | `change_log` centralizes risk; use derived view / UNION ALL | `UNION ALL` view can't be a NOTIFY trigger source. "Single BIGSERIAL" isn't centralization risk — it's an ID generator. Unified `changes_since` and NOTIFY are real wins. Partitioning called out as future option if volume warrants |

**Round 4 (v1.4):**

| # | Concern | Source | Resolution |
|---|---|---|---|
| K4.1 | Stale sidecar references at lines 10, 63, 381, 396 | Kimi | Cleaned all four (§1, §3 layout, Phase 6.3, §10 risk line) |
| K4.2 | §4.3 memory schema contradicts decision 24 (both said projection columns were present and absent) | Kimi | Decision 24 wins; §4.3 explicitly drops projection columns for memories with a one-line note on how to add them later if needed |
| K4.3 | Vocab schema-evolution convention should be documented | Kimi | Decision 23 amended: "trigger-load-bearing attributes → columns; presentation-only → JSONB"; documented in core README |
| K4.4 | <32GB RAM users should default to omnibus from day 1 | Kimi | Documented in §10 risk register and earmarked for the README's setup section |
| K4.5 | `compute_projection_paths` should return both absolute and repo-relative | Kimi | Tool signature updated to return `{old_absolute, new_absolute, old_repo_relative, new_repo_relative}` |
| D4.1 | Stale sidecar references throughout | DeepSeek | Same fix as K4.1 |
| D4.2 | `agent-notes-reflections` entry point still in pyproject despite decision 25 | DeepSeek | Removed from default entry-point list; added a comment noting it's added only if the Phase 5 spike concludes a dedicated server is needed |
| D4.3 | Legacy `file_path` migration must strip absolute prefixes | DeepSeek | Phase 2a.3 explicitly normalizes legacy values to repo-relative-under-`breadcrumbs_dir` form |
| D4.4 | Risk register understates the ~810MB tradeoff vs the sidecar | DeepSeek | Risk register rewritten to be honest: "swapped a network SPOF for ~810MB of duplicated model weights"; omnibus is the mitigation |

**DeepSeek items re-litigated and held with sharper rationale:**

| # | DeepSeek's renewed argument | My response |
|---|---|---|
| D3 (round 2) | "A single FOREIGN KEY on `breadcrumbs(project_id, identifier)` would prevent bugs, not just contain them. The audit script runs after corruption." | A single FK can't exist on `links` because `from_kind` spans multiple kind tables. Real options are (a) audit script, (b) per-kind validation triggers dispatching on `from_kind` (re-introduces the coupling we eliminated and breaks every time a kind is added), or (c) separate link tables per kind-pair (combinatorial table growth). Partial FKs are not a Postgres feature. "Single FK" isn't on the menu. Audit script holds. |
| D4 (round 2) | "Every downstream table referencing links needs all 9 columns; every join repeats them. Surrogate PKs are standard for a reason." | No table references `links` (it's a pure many-to-many junction). Joins from `links` to kind tables need the natural-key columns regardless of whether `links` has a surrogate — the surrogate adds nothing for joins. The "standard" surrogate-PK convention is for rows that are referenced by other rows; that's not this case. Natural composite PK lets `add_link`/`remove_link` take symmetric arguments without an opaque ID indirection. Holds. |
| D7 (round 2) | "The risk isn't the sequence — it's that a bug in one kind server's log-writing floods the audit table for all kinds, and NOTIFY fires on everything." | Legitimate reframing; partially accepted. Documented in §10 as an acknowledged tradeoff with mitigations (debouncing, partitioning, feature-flag quiesce). Splitting to per-kind tables is a future migration if isolation pain ever materializes; not paying for it day 1. |

**Round 5 (v1.5):**

| # | Concern | Source | Resolution |
|---|---|---|---|
| K5.1 | Trigger description still said it read from `vocabularies.attributes` | Kimi | Corrected to `vocabularies.is_terminal` column (consistent with decision 23) |
| K5.2 | `memories` missing `active` column + partial unique index | Kimi | Added to §4.3: surrogate `id BIGSERIAL`, `active BOOLEAN`, partial unique index on `(project_id, name) WHERE active`; Phase 3.1 reflects this |
| K5.3 | `compute_projection_paths` can't compute repo-relative without `repo_root` | Kimi | Added `repo_root TEXT` to `projects` table; documented path-composition math in §4.2 |
| G2 | Adopt mcp Python SDK | GLM | Reinforces existing decision 21; no plan change needed |
| G3 | `list_projects` discoverable early in memory server tool list | GLM | Phase 3.2 explicitly confirms |
| G4 | change_log bulk-insert performance during import | GLM | Phase 2a.3 now specifies `COPY` for rows and batch INSERT for change_log; trigger already disabled per K3.2 |
| G5 | Test infrastructure planning was missing | GLM | New Phase 1b.6: testcontainers / ephemeral Postgres in CI; convention documented in AGENTS.md |
| G6 | Pool sizing guidance | GLM | Decision 22 amended: `min_size=2, max_size=5` per process; not deferred |
| G7 | Single binary with --kind flag instead of 4 separate entry points | GLM | Adopted: one `agent-notes` binary; per-kind names are thin shims calling `serve(kinds=[...])`; this IS the omnibus mechanism (decision 12) — invoke with multiple kinds for omnibus mode |
| G8 | Surrogate id on links as query-ergonomics aid | GLM | Documented in §10 risks as a deferred option; natural PK stays |
| G9 | Reflections spike at Phase 5 too late | GLM | Spike moved to Phase 3.6 so any memory-schema strain surfaces during memory server build; Phase 5 becomes conditional |
| G10 | Missing README / AGENTS.md / .gitignore | GLM | Phase 1a.1 amended to include all three from day 1 |
| G11 | Legacy `valid_kinds` / JSON-schema enum mismatch in breadcrumb-mcp | GLM | New Phase 0.1: fix in legacy server before migration starts |
| G12 | Coordinate with memory-mcp's superseded plan observations | GLM | New Phase 0.2 audits and feeds Phase 3.2/3.5 |
| K5.4 | `change_log.identifier` semantics for workspace-level events | Kimi | Deferred per Kimi's own note; document when first written |

**Status:** plan is converging. Net change in v1.5 is editorial cleanup + schema correctness (memories `active`, projects `repo_root`) + one architectural simplification (single binary, GLM #7). No further architectural debates open. Ready for Phase 0 dispatch.

---

## 13. Plan 008 P2 decision — fail-safe status lattice

**Decision 57 (2026-06-08):** On genuinely concurrent status ops (same lamport), the fail-safe lattice is `open > claimed > closed > deferred` — open dominates closed. This surfaces unfinished work rather than silently hiding it. Configurable lattices are a P3+ concern; do not build a policy engine in P2.

**Implementation:** The `fold_work_item` function groups ops by lamport. Within a group:
- A single status op is applied directly (sequential, last-write-wins).
- Multiple concurrent status ops are resolved by lattice rank; ties break by lexicographically smaller `op_id` (deterministic because `op_id` is a content hash).

**Merge op:** The `merge` op carries a `merged_state` payload and replaces the entire state. This is the reconciliation primitive for replica logs.

**Rationale:** This matches the Radicle COB / git-bug model: deterministic ordering by `(lamport, op_id)` with a lattice for concurrent conflicts. The lattice direction is intentionally fail-safe (unfinished work is visible) because silently closing work is the anti-pattern this project exists to kill.

## 14. Plan 010 decision — canonical lifecycle convergence

**Decision 58 (2026-06-28):** The regista-branch write path (when
`AGENT_NOTES_REGISTA_WRITES=1`) uses the dossier v4 **canonical lifecycle
workflow** (states: open/in_progress/blocked/deferred/in_review/in_human_review/
done), not the legacy breadcrumb lattice (open/claimed/deferred/closed). The
review gate (`adversarial_review` + `human_gate`) is registered with the
**relaxed** policy (`require_human: false`, homelab default) — an agent can file
→ another-lineage actor reviews → close, with no per-item human bottleneck, while
the mixed-chain integrity guarantee (Invariant G: `done` requires a distinct
cross-lineage pass + accepter) still holds. `claimed` is retired as a lifecycle
state (Plan 010 WI-2): lease is a regista claim (a separate axis), not a state.
Agent `close` maps to `submit_for_review` (→ `in_review`), NOT `done` — the
agent cannot reach `done` unilaterally.

**Projection vocabulary (Decision 59):** the local `work_items.status` column
holds canonical states directly (no display remapping layer). The schema CHECK
constraint and `wi_status` vocabulary accept both canonical and legacy breadcrumb
states during the transition (additive, backward-compatible). The legacy op_log
path (flag off) is unchanged — it still writes breadcrumb states. The migration
(`migrate-to-regista`) converts legacy items to canonical (`closed`→`done`,
`claimed`→`in_progress`, `deferred`→`deferred`).

**Rationale:** "One lifecycle" is the convergence plan's purpose — a display
remapping layer would be exactly the drift the project avoids. The relaxed gate
keeps agent workflows frictionless in a homelab while preserving the provenance
guarantee (a distinct cross-lineage pass is always required for `done`).
Landing behind the existing `AGENT_NOTES_REGISTA_WRITES` flag means the change is
zero-risk for existing deployments (flag off = legacy path unchanged); flip
per-project after migration + `replay()==0`.
