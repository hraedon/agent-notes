# AGENTS.md

Conventions and quick reference for agents (and humans) working on agent-notes-mcp.

## Orient

1. **Read first:** `plans/001-architecture-and-implementation.md` — authoritative architecture, design decisions (numbered, peer-reviewed), and phased task tables.
2. **Then:** the current phase's task table in §9 tells you what's in scope.

## Build / test / lint

```bash
uv pip install -e ".[test]"

make test           # runs pytest against ephemeral Postgres (testcontainers)
make lint           # ruff
make fmt            # ruff format
```

Tests for triggers, recursive CTEs, and `change_log` semantics must run against real Postgres — no DB mocks for those. Use `testcontainers[postgres]` (already in `[test]` extras) for ephemeral instances in CI.

## Architecture in one paragraph

One Postgres database (`agent_notes`), one Python package (`agent_notes`), one `Server` base class with composable kind registries. Per-kind MCP servers (`agent-notes-breadcrumbs`, `agent-notes-memory`, `agent-notes-search`) are thin shims over the shared `core/` library that handles JSON-RPC, DB connection pooling, embedding, links, change_log, NOTIFY, MCP resources, and markdown projection. Omnibus mode (multiple kinds in one process) is the same code path invoked with multiple `--kinds`.

## Schema conventions

- **All kinds are project-scoped** (decision 13). Cross-cutting memories live in a `global` project per workspace.
- **Vocabularies live in data, not `CHECK` constraints** (decision 5). Trigger-load-bearing attributes (`is_terminal`, `is_open`, `sort_order`) are first-class columns; presentation-only metadata stays in `attributes JSONB` (decision 23).
- **`change_log` is the sole audit/history source** (decision 7). Per-kind history tables are not used.
- **`links` table accepts dangling rows**; filter at query time. `links_audit` reports orphans (Phase 6.3).
- **DB is the working source of truth; markdown is a deterministic projection** (decision 4) rebuilt by `/end`. Hash-check before overwrite for breadcrumbs (decision 8); memories don't project.

## Embedding

In-process lazy singleton (decision 2). 270MB nomic model loads on first call; thread-safe. `AGENT_NOTES_EMBED_MODEL` / `AGENT_NOTES_EMBED_DIM` env vars override defaults. Always embed *before* opening a DB transaction (decision 26).

## Connection pool

`psycopg_pool.ConnectionPool` (sync), `min_size=2, max_size=5` per process (decision 22).

## When you make a change that surprises a future reader

Add it to the relevant numbered decision in `plans/001-architecture-and-implementation.md`. Don't bury rationale in code comments — the plan is where peers look.

## End of session

`/end` runs `render_index` per touched project, performs `git mv` for any status transitions (using `compute_projection_paths` from the breadcrumbs server), and commits. The MCP server never runs `git` itself (decision 15).
