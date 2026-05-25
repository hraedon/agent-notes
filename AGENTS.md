# AGENTS.md

Conventions and quick reference for agents (and humans) working on agent-notes-mcp.

## Orient

1. **Read first:** `plans/001-architecture-and-implementation.md` — authoritative architecture, design decisions (numbered, peer-reviewed), and phased task tables.
2. **Then:** `plans/003-drop-projection-add-web-frontend.md` for the projection-removal and web frontend changes (Phase 8a).
3. **Then:** `plans/004-flatten-cli-and-async-bridge.md` for the MCP->CLI flattening (Phase 9a–the current phase).
4. **Then:** the current phase's task table tells you what's in scope.

## Build / test / lint

```bash
uv pip install -e ".[test]"

make test           # runs pytest against ephemeral Postgres (testcontainers)
make lint           # ruff
make fmt            # ruff format
```

Tests for triggers, recursive CTEs, and `change_log` semantics must run against real Postgres — no DB mocks for those. Use `testcontainers[postgres]` (already in `[test]` extras) for ephemeral instances in CI.

## Architecture in one paragraph

One Postgres database (`agent_notes`), one Python package (`agent_notes`), one `Server` base class with composable kind registries. The **CLI (`agent-notes`) is the primary sync surface** (Plan 004 Phase 9a+); legacy per-kind MCP servers (`agent-notes-breadcrumbs`, `agent-notes-memory`, `agent-notes-search` / `agent-notes-omnibus`) are deprecated but operational during the transition. The shared `core/` library handles DB connection pooling, embedding, links, change_log, NOTIFY, and search. A read-only web frontend (`agent-notes-web`) provides browser-based browsing of breadcrumbs, memories, and semantic search.

## Schema conventions

- **All kinds are project-scoped** (decision 13). Cross-cutting memories live in a `global` project per workspace.
- **Vocabularies live in data, not `CHECK` constraints** (decision 5). Trigger-load-bearing attributes (`is_terminal`, `is_open`, `sort_order`) are first-class columns; presentation-only metadata stays in `attributes JSONB` (decision 23).
- **`change_log` is the sole audit/history source** (decision 7). Per-kind history tables are not used.
- **`links` table accepts dangling rows**; filter at query time. `links_audit` reports orphans (Phase 6.3).
- **DB is the only source of truth** (decision 39, Plan 003). Markdown projection was removed in Phase 8a. No files are written to disk by the servers.

## Embedding

In-process lazy singleton (decision 2). 270MB nomic model loads on first call; thread-safe. `AGENT_NOTES_EMBED_MODEL` / `AGENT_NOTES_EMBED_DIM` env vars override defaults. Always embed *before* opening a DB transaction (decision 26).

## Connection pool

`psycopg_pool.ConnectionPool` (sync), `min_size=2, max_size=5` per process (decision 22).

## Web frontend

`agent-notes-web` serves a read-only HTML viewer on `127.0.0.1:8765` (configurable via `AGENT_NOTES_WEB_PORT`). FastAPI + Jinja2 templates. No auth (decision 43). Routes: `/`, `/workspaces/{slug}`, `/workspaces/{slug}/{project}`, `/workspaces/{slug}/{project}/breadcrumbs/{id}`, `/workspaces/{slug}/{project}/memories/{name}`, `/search?q=...`.

## When you make a change that surprises a future reader

Add it to the relevant numbered decision in `plans/001-architecture-and-implementation.md` (and now `plans/004-flatten-cli-and-async-bridge.md`). Don't bury rationale in code comments — the plan is where peers look.

## Skills

Agent-facing workflows ship as skills under `skills/`:
`file-breadcrumb`, `update-breadcrumb`, `find-breadcrumb`, `add-memory`,
`start`, `reflect`, `end`. Each is a Markdown SKILL.md that shells to
`agent-notes <noun> <verb> --json` and carries the per-workflow
judgment in prose. Install with `agent-notes install-skills --target
claude` (idempotent). opencode target is deferred — see Plan 004
question Q4 and `skills/opencode/README.md`.

## End of session

`/end` runs `reflect` and commits any working-directory changes. There is no projection to rebuild (Plan 003 decision 46). The server never runs `git` itself (decision 15). During Phase 9a+, `/end` should use the CLI (`agent-notes`) instead of MCP tools where possible.
