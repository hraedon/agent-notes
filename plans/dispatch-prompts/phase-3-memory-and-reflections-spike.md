# Phase 3 — Memory server + Reflections spike

You are implementing Phase 3 of the agent-notes-mcp project: the memory kind schema, the memory MCP server, the one-shot import from `/projects/memory-mcp/`, and the reflections-as-memories spike (moved up from Phase 5 per GLM #9 so any memory-schema strain surfaces during memory server build, not after).

## Project state at HEAD

Phase 1a, 1b, 2a, and 2b are complete and merged. The core library is solid; the breadcrumbs server exists with full DB+projection support. You add memory as a parallel kind.

## Read first

1. `plans/001-architecture-and-implementation.md` — §2 (all 26 decisions, especially 13, 17, 19, 23, 25), §4.3 (memory schema — note `active` column, partial unique index, `supersedes BIGINT`), §6 Memory server tool list, §9 Phase 3 (task table), §11 (harness reconciliation open question).
2. `AGENTS.md`.
3. `plans/dispatch-prompts/README.md` for the hard test requirement.
4. `/projects/memory-mcp/src/memory_mcp/server.py` — the legacy server's 5 tool implementations. Same external shape EXCEPT decisions 13 (project-scoped, no default), 16 (history is a core helper, not memory-specific), 17 (no projection columns), 19 (no reflections-specific fields — see Phase 3.6).
5. `/projects/memory-mcp/plans/001-generalization-and-link-graph.md` (superseded but its observations are load-bearing — already captured in plan Phase 0.2).

## Scope

### 3.1 — `schema/200_memory.sql`

Per plan §4.3:

- `memories` table with surrogate `id BIGSERIAL PRIMARY KEY`.
- `(workspace_id, project_id)` redundantly populated by trigger from `projects.workspace_id` (same pattern as breadcrumbs in Phase 2a) so composite FKs into `vocabularies (workspace_id, kind_namespace, name)` enforce per-workspace vocab.
- Columns: `project_id INTEGER NOT NULL REFERENCES projects(id)`, `workspace_id INTEGER NOT NULL` (trigger-populated), `name TEXT NOT NULL`, `memory_type TEXT NOT NULL` (FK to vocabularies), `description TEXT NOT NULL`, `body TEXT NOT NULL`, `tags TEXT[] NOT NULL DEFAULT '{}'`, `active BOOLEAN NOT NULL DEFAULT TRUE`, `supersedes BIGINT REFERENCES memories(id)`, `embedding vector(768)`, `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`, `updated_at TIMESTAMPTZ NOT NULL DEFAULT now()`.
- **Partial unique index on active rows only** (Kimi round-5 #2):
  ```sql
  CREATE UNIQUE INDEX memories_name_active_unique
      ON memories (project_id, name) WHERE active = true;
  ```
- HNSW index on `embedding`.
- GIN index on `tags`.
- **No projection_sha256 / projection_dirty columns** (decision 24; Kimi round-5 #2).
- Insert/update/delete write `change_log` rows via application code (no trigger needed — Phase 1b's `change_log.write_change` handles this from the server).
- `agent-notes-setup` already creates a `global` project per workspace (Phase 1a). Verify this; cross-cutting memories scope to `project=global`.

### 3.2 — `src/agent_notes/servers/memory.py`

Thin server subclassing `Server`. Tools (5 + inherits 9 core tools):

1. **`add_memory`** — required: `project` (no default! decision 13 / GLM #12 — error helpfully if missing), `name`, `memory_type`, `description`, `body`. Optional: `tags`, `supersedes_name` (a previous active memory in the same project to deactivate). Algorithm:
   - Embed text (description + body) outside transaction.
   - BEGIN.
   - If `supersedes_name` provided OR a memory with same `(project_id, name)` already active exists: UPDATE old row `SET active=false`, then INSERT new row with `supersedes=<old.id>`.
   - Else: simple INSERT.
   - Write `change_log` event=`filed` (or `superseded` if applicable).
   - COMMIT.
   - Auto-parse `[[name]]` references from body (skip fenced code spans — use a simple regex with a code-block filter): for each, call `links.add_link(from_kind='memory', from_workspace, from_project, from_identifier=<this name>, to_kind='memory', to_workspace, to_project, to_identifier=<linked name>, relationship='relates_to')`. The links.add_link from Phase 1b already writes its own `change_log` row in the same transaction; this happens outside the memory INSERT's transaction so failure of the link insert doesn't roll back the memory. Document this protocol in a comment.

2. **`search_memory(query, project?, memory_type?, tags?, limit=5, include_body=false)`** — semantic search. **Body elided by default** (decision per memory-mcp plan §2.5; GLM-compatible). Returns name + description + score (+ memory_type + project). If `include_body=true`, include body. Filter by project/memory_type/tags. The legacy server includes body always; the new contract saves tokens at the cost of one follow-up `get_memory` call when an agent wants the body.

3. **`list_memories(project?, memory_type?, tags?, active_only=true, limit=50)`** — structured list, no semantic ranking. Returns name + description + memory_type + project + tags. `active_only=true` uses the partial unique index for cheap scans.

4. **`get_memory(project, name)`** — fetch full body. If memory has `supersedes`, include note about it.

5. **`delete_memory(project, name)`** — soft delete (sets `active=false`); writes `change_log` event=`deleted`. Does NOT remove from `links` table (dangling-link policy per decision in plan §10).

Inherited core tools: `add_link`/`remove_link`, `history`, `changes_since`, `list_workspaces`, `list_projects`, `list_vocabulary`, `archive_vocabulary`. Per GLM #3, verify that `list_projects` is in `tools/list` early so agents calling `add_memory` can discover available projects.

`trace_graph` (kind-local) wraps `links.trace_graph(target_table='memories')`. Same pattern as breadcrumbs in Phase 2a — the `target_table` kwarg should already be in core/links.py from Phase 2a; verify and use.

### 3.3 — Bidirectional `[[name]]` link parsing

Implement in `src/agent_notes/servers/memory.py` (or a sibling helper). Function: `parse_link_refs(body: str) -> list[str]`. Strips fenced code blocks (` ``` `) and inline code (single backticks), then matches `\[\[([a-z0-9-]+)\]\]`. Returns the slugs. Used inside `add_memory` to auto-create `relates_to` links.

Test cases: body with code-block-wrapped `[[foo]]` → empty; body with both real and code `[[bar]]`+`` `[[baz]]` `` → only `bar`.

### 3.4 — `scripts/import_legacy_memory.py`

Same pattern as Phase 2a's `import_legacy_bc.py`:

1. Read legacy `/projects/memory-mcp/`'s DSN.
2. For each row, determine destination project: if the legacy `project` field is set, use it; if it's `sf2`, map to `sf2` project (which was hardcoded as default per memory-mcp plan §2.1). For anything else, fall back to `global` project in `default` workspace.
3. Seed `memory_types` vocabulary with the four standard types (`user`, `feedback`, `project`, `reference`) and any extras observed.
4. Disable NOTIFY trigger during bulk insert (Kimi round-3 #2). Use `COPY` for rows.
5. Replay `supersedes` chains: legacy memory-mcp records `supersedes` as a name reference; resolve to id and populate `memories.supersedes` correctly. Insert link rows mirroring the supersedes chain into `links` (kind=memory, relationship='supersedes') — Phase 1b's design 16.
6. Re-embed in-process.
7. Run the `[[name]]` parser over every body and create the implicit `relates_to` links.
8. Idempotent.

### 3.5 — Phase 3.6: Reflections-as-memories spike (decision 25)

**Goal:** answer the question — "do reflections fit cleanly as memories with `memory_type='reflection'`, or do we need a dedicated server?"

Steps:
1. Add `reflection` to the `memory_types` vocabulary (in `default` workspace).
2. Pick 2–3 historical reflections from `/projects/software-factory-2/reflections/` and `/projects/breadcrumb-mcp/reflections/`. For each:
   - Parse the file (frontmatter + sections like "Work summary", "On the project", "On the work done", "What remains", "Gaps to flag").
   - Call `add_memory` with `name=<slug like 2026-05-20-glm-5-1>`, `memory_type=reflection`, `description=<one-line from work summary>`, `body=<original markdown>`, `tags=[<project>, <model>]`.
   - **Structured sections + gaps:** since memories don't have JSONB extras column (decision 24's prose says metadata can go in JSONB; verify schema reality — if not present, this is the spike's first finding), either propose adding `metadata JSONB` to `memories` OR conclude that the lack of structured fields IS the schema strain.
3. Run typical reflection-shaped queries against the memory server: `search_memory("substrate constructor")`; `list_memories(memory_type='reflection', tags=['sf2'])`; `get_memory(project=..., name=...)`.
4. Try to implement `extract_gaps` as a separate tool that fetches a reflection (`get_memory`), parses the "Gaps to flag" section, and proposes BC drafts. Does it feel forced?
5. Write a 1-page report `plans/dispatch-prompts/phase-3.6-reflections-spike-outcome.md` with the conclusion:
   - **If memories suffice:** what minor additions are needed (e.g., `metadata JSONB` column, or `tags` convention); update plan §4.4 to say "reflections are memories"; Phase 5 becomes a 1-task phase to import historical reflections + update the `reflect` skill.
   - **If memories don't suffice:** describe the specific schema strain (e.g., needing typed sections, gaps_filed_as array, replaced_by link); recommend building the dedicated server per plan v1.2's Phase 5; Phase 5 stays full-scope.

### 3.6 — Harness reconciliation decision (§11)

Pick one of options 1/2/3 from plan §11. Document in `plans/dispatch-prompts/phase-3-harness-reconciliation.md`. Recommended: option 1 (document the boundary) for now — write a short note in `AGENTS.md` explaining harness memory is per-instance, memory-mcp is cross-agent. Option 2 (bridge tool) can be a future task.

### 3.7 — Tests

Same hard requirement: every state-mutating tool gets a stdin-driven test asserting `change_log`.

Additional:
- `add_memory` with no `project` arg returns a helpful error listing available projects (GLM #3 — discoverability).
- `add_memory` with `[[name]]` in body creates a `relates_to` link row.
- `add_memory` with `[[name]]` inside a fenced code block does NOT create a link.
- `add_memory` that supersedes an existing active memory: old row's `active=false`, new row's `supersedes=<old.id>`, partial unique index doesn't fire (both can coexist; only one active).
- `search_memory(include_body=false)` returns rows without body fields; `include_body=true` includes them.
- `list_memories(active_only=true)` excludes superseded rows.
- Import script test against a tiny seed DSN.

## Validation

1. All previous tests still pass.
2. `ruff check` clean.
3. New Phase 3 tests pass (~25 new).
4. `agent-notes-migrate --all` runs `000_core.sql` + `100_breadcrumbs.sql` + `200_memory.sql` cleanly.
5. `agent-notes-memory` binary's `tools/list` includes 5 memory tools + 9 inherited.
6. The 3.6 spike report exists at `plans/dispatch-prompts/phase-3.6-reflections-spike-outcome.md` with a clear conclusion.

## Out of scope

- Cross-kind search (Phase 4).
- Dedicated reflections server (Phase 5, only if 3.6 spike says it's needed).
- Resources surface for memory (Phase 6.1).

## Report at end

- Files added/modified.
- Test counts before/after.
- 3.6 spike outcome (1-line summary in your report, full report in the linked file).
- Honest assessment: any rough edges in the memory server that should be cleaned up before Phase 4 builds search on top?
