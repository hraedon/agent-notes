# Phase 4 — Cross-kind search server

You are implementing Phase 4 of the agent-notes-mcp project: the cross-kind search MCP server. This is the smallest of the remaining phases — one new server, two new tools, one materialized view, and a `/start` skill update doc.

## Project state at HEAD

Phases 1a, 1b, 2a, 2b, and 3 are complete and merged. Breadcrumbs and memory both have kind tables (`breadcrumbs`, `memories`); reflections may or may not have a dedicated table depending on the Phase 3.6 spike outcome (read `plans/dispatch-prompts/phase-3.6-reflections-spike-outcome.md` to know).

## Read first

1. `plans/001-architecture-and-implementation.md` — §2 decision 10 (cross-kind traversal is `trace_graph_all`, kind-local stays kind-local), §6 Search server tool list, §9 Phase 4 (task table).
2. `AGENTS.md`.
3. `plans/dispatch-prompts/README.md`.
4. `plans/dispatch-prompts/phase-3.6-reflections-spike-outcome.md` — tells you whether reflections are a memory or a separate table.
5. `src/agent_notes/core/links.py` — `trace_graph` is kind-local; you'll write `trace_graph_all` here in the search server.
6. `src/agent_notes/servers/breadcrumbs.py` and `memory.py` for the existing query/find shape (you'll mirror it for cross-kind).

## Scope

### 4.1 — `src/agent_notes/servers/search.py`

Thin server subclassing `Server`. Two new tools:

**`search_all_notes(query, kinds?, workspaces?, projects?, since?, limit=10)`**
- `query` (required): natural-language string.
- `kinds`: optional list filter — default = all known kind tables.
- `workspaces`/`projects`: optional filters by slug.
- `since`: optional ISO timestamp; only return notes whose `updated_at` (or kind-specific equivalent) is more recent.
- `limit`: default 10, max 50.

Algorithm:
- Embed query (query-prefix, in-process).
- Issue a single SQL query against the `all_notes_search_v` view (see 4.3) ranking by `embedding <-> %s` ascending; apply filters.
- Returns: name/title + kind + project_slug + workspace_slug + score + updated_at. **No body** (clients fetch via kind tools). This keeps responses token-cheap.

Implementation note: use `psycopg.sql.Identifier` / `Literal` composition for the filter clauses, never string concatenation.

**`trace_graph_all(from_kind, workspace, project, identifier, direction='dependents', max_depth=3, relationship_kinds=None)`**
- Cross-kind recursive traversal via `LATERAL UNION ALL` against per-kind row sources.
- Returns: list of `{kind, project, identifier, title, status, depth, relationship}` rows.
- Slower than `trace_graph` by design (decision 10). Document the cost at the tool's docstring.

Implementation: use a CTE that starts from the anchor `(from_kind, workspace, project, identifier)`, joins to `links` to find neighbors, then `LEFT JOIN LATERAL (SELECT title, status FROM breadcrumbs WHERE ... UNION ALL SELECT name AS title, NULL AS status FROM memories WHERE ... [UNION ALL ... reflections ...]) target ON true`. Adapt the `UNION ALL` branches to whichever kind tables exist (driven by the Phase 3.6 outcome).

Inherits the 9 core tools.

### 4.2 — Optional `all_notes_search_v` view

Add to `schema/400_search.sql`:

```sql
CREATE OR REPLACE VIEW all_notes_search_v AS
SELECT
    'breadcrumb' AS kind,
    workspace_id,
    project_id,
    identifier,
    title,
    body,
    embedding,
    updated_at
FROM breadcrumbs
UNION ALL
SELECT
    'memory' AS kind,
    workspace_id,
    project_id,
    name AS identifier,
    description AS title,
    body,
    embedding,
    updated_at
FROM memories
WHERE active = true
-- [UNION ALL ... reflections ... if separate table per Phase 3.6 outcome]
;
```

Per plan §6 / Kimi D: this is **search-only** — common columns only (kind, workspace, project, identifier, title, body, embedding, updated_at). Kind-specific fields (severity, diagnostic_keys, supersedes, replaced_by) are NOT exposed; agents fetch those via kind tools. Document at the top of the SQL file.

Note: the view is non-materialized (just `VIEW`, not `MATERIALIZED VIEW`) — pgvector index on the underlying tables is what makes the search fast; materializing the view would double the storage and need refresh.

If you find the view performs poorly under realistic load, document the finding and propose materialization as a follow-up — don't materialize speculatively.

### 4.3 — `/start` skill update doc

Write `plans/dispatch-prompts/start-skill-update.md` describing the change to `~/.claude/skills/start/SKILL.md` so the operator can apply it:
- After the existing "read worklog" step, the skill should call `search_all_notes` with the current session's focus (passed in via skill args or derived from `git diff --stat`) and surface the top 5 cross-kind matches. This is the "session continuity" use case (decision per plan §1).

You do NOT modify the skill yourself.

### 4.4 — Tests

- `search_all_notes` test: seed 3 BCs + 3 memories (with different embeddings); search with a query that matches a memory more than any BC; assert memory ranks higher.
- `trace_graph_all` test: create a chain BC-1 → memory-A → BC-2 via `add_link`; traverse from BC-1 in `dependents` direction; assert all three nodes returned with correct depths.
- Filter tests: by kinds, by workspaces, by projects, by since.
- `since` test: insert two memories at different times; query with `since` between them; assert only the newer is returned.

Stdin-driven end-to-end tests for both tools (the hard requirement applies even though these are read-only — drive them via the Server, not via direct function call, so the protocol layer is exercised).

## Validation

1. All previous tests still pass.
2. `ruff check` clean.
3. New Phase 4 tests pass (~10 new).
4. `agent-notes-migrate --all` runs all schema files including the new `400_search.sql`.
5. `agent-notes-search` binary's `tools/list` shows 2 new tools + 9 inherited.

## Out of scope

- Reflections-specific work (Phase 5, if needed).
- Resources surface (Phase 6.1).
- Omnibus binary configuration (Phase 6.2).

## Report at end

- Files added/modified.
- Test counts before/after.
- View performance notes: did `search_all_notes` queries feel snappy under your test load? Any indexes you'd recommend for production?
- The exact view definition (so reviewers can sanity-check the UNION).
