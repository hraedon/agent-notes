# Phase 6 — Polish: resources surface, omnibus mode, doctor, archive legacy

You are implementing Phase 6 of the agent-notes-mcp project. This is the final phase — four small, parallel tasks. Can be a single dispatch (one agent does all four) or split across two (6.1+6.2 together, 6.3+6.4 together).

## Project state at HEAD

All earlier phases complete. Kind servers (breadcrumbs, memory, optionally reflections), search server, all schemas, imports, projections — all merged. Tests in the high hundreds, all green.

## Read first

1. `plans/001-architecture-and-implementation.md` — §2 (decisions 12, 16), §6 Resources surface, §9 Phase 6.
2. `AGENTS.md`.
3. `plans/dispatch-prompts/README.md`.

## Scope

### 6.1 — MCP resources surface for all kinds

The `core/resources.py` scaffolding (Phase 1b) is in place. Wire actual resource handlers per kind:

- `note://breadcrumb/<workspace>/<project>/<identifier>` — returns the BC's full body + frontmatter as a resource.
- `note://memory/<workspace>/<project>/<name>` — returns memory body.
- `note://reflection/<workspace>/<project>/<slug>` — if reflections kind exists.
- Collection URIs: `note://breadcrumb/<workspace>/<project>/` returns a list of resource refs.

For each kind server, register the handler via the `Server` base class's `register_resource_handler`. Implement `resources/list` (per-kind) and `resources/read` (URI-driven).

Tests: a smoke test for each kind that requests the collection URI, asserts a list of references, then reads one and asserts the content matches what `get_breadcrumb`/`get_memory` returns.

### 6.2 — Omnibus binary

Per decisions 12 and 7 / GLM #7, the existing `agent-notes` binary already accepts `--kinds X,Y,...`. Phase 6.2's task is to make this production-ready:

1. Verify `agent-notes serve --kinds breadcrumbs,memory,search` mounts all three kind registries into one process and responds correctly to `tools/list` (returning the union of all kinds' tools, deduped) and `tools/call` (routing by tool name).
2. Add `agent-notes-omnibus` as a convenience entry point in `pyproject.toml` that runs `serve(kinds=['breadcrumbs', 'memory', 'search'])` (and `reflections` if it exists).
3. Document in `README.md`: "For machines with <32GB RAM, prefer `agent-notes-omnibus` over running per-kind binaries — it loads the embedding model once instead of once per process." (Kimi round-4 #4.)
4. Update each kind's harness-config example in `README.md` to show both per-kind and omnibus variants.

Tests: a test that constructs an omnibus Server, mounts two kind registries, and verifies tools from both kinds appear in `tools/list` and dispatch correctly.

### 6.3 — `agent-notes-doctor` script

`src/agent_notes/scripts/doctor.py` + entry point `agent-notes-doctor`. Health checks:

1. **DSN reachable** — try to connect via `core/db._conn()`; report success/failure with the error message.
2. **Schema migration up to date** — list applied schema files vs files in `schema/`; report drift.
3. **Embedding model loads** — `core/embed.embed("hello", "query")`; report load time and resulting vector dim; compare to `AGENT_NOTES_EMBED_DIM`.
4. **Per-project `breadcrumbs_dir` write access** — for each project, check the path exists and is writable; report.
5. **`links_audit`** — count dangling links per kind (links whose `from_*` or `to_*` no longer correspond to any kind-table row). Report per-kind counts.
6. **Vocabulary integrity** — for each kind, verify every distinct `kind`/`status`/`severity` value in the kind table has a corresponding `vocabularies` row (catches a vocab archive-then-orphan situation).

Output: human-readable table-style summary; exit code 0 if all healthy, 1 if any check failed. Useful for CI and pre-deploy.

Tests: a smoke test that runs the doctor against the testcontainers DB and asserts all checks pass on a clean install.

### 6.4 — Archive legacy repos

**The operator does this manually.** Your task: write `plans/dispatch-prompts/legacy-archive-checklist.md` describing the steps:

1. Verify one week of clean operation against the new server (no `audit` drift, no missing import data, all harness clients work).
2. Update `/projects/breadcrumb-mcp/README.md` to redirect: "Superseded by `/projects/agent-notes-mcp/`. Historical reference; do not extend."
3. Same for `/projects/memory-mcp/README.md`.
4. Move each repo to an `archive/` directory or add `archived: true` markers — operator's preference.
5. Update any harness configs that still reference the old binaries.
6. Commit each repo with a final "archived: superseded by agent-notes-mcp" commit.

You do not touch the legacy repos yourself.

## Validation

1. All prior tests still pass.
2. `ruff check` clean.
3. New Phase 6 tests pass (~10 new).
4. `agent-notes-doctor` reports green against a clean install.
5. `agent-notes serve --kinds breadcrumbs,memory,search` starts and serves all three kinds' tools in one process.
6. Legacy archive checklist is clear and actionable.

## Out of scope

- Anything not in 6.1–6.4. If you find a Phase X.Y task that wasn't done, file it as a new dispatch prompt rather than tacking it onto this phase.

## Report at end

- Files added/modified.
- Test counts before/after.
- Final memory-footprint measurement: `ps aux` or similar on a running `agent-notes serve --kinds breadcrumbs,memory,search` vs three separate binaries. Did the omnibus actually save the model-load overhead?
- Any remaining loose ends across all phases that should be ticketed before declaring "done".
