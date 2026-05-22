# Plan 003 — Drop Markdown Projection, Add Web Frontend

Status: Proposed (v0.1, drafted 2026-05-22). Awaiting one round of peer review.
Scope: Architectural pivot. Removes the markdown projection layer wholesale (Plan 001 decisions 4, 8, 15, 17, 20, 24 and supporting apparatus) and replaces the "human reads breadcrumbs in the repo" workflow with a localhost web frontend that reads and writes via the existing model layer.
Consumers: same as Plan 001, plus future browser-based consumers.
Supersedes: the projection-related portions of Plan 001 (§3 `core/projection.py`, decisions 4/8/15/17/20/24, Phase 2b projection wiring, AGENTS.md "End of session" projection clause).

## 1. Why now

Plan 001 framed markdown projection as load-bearing: "DB is the working SoT; markdown is a deterministic projection rebuilt by `/end`." Five hours of production use surfaced that the projection layer is the single largest source of correctness bugs and operational friction:

- **BC-005**: `file_path` parameter advertised but silently ignored (resolved, but the underlying complexity around `_status_to_dir` and slug derivation is still there).
- **BC-006**: unconfigured `breadcrumbs_dir` silently writes to `/tmp/`.
- **`safe_write` hash check + `projection_dirty` + `reconcile_projection`** exist to defend against human edits the MCP would otherwise clobber. That entire machine exists because of one workflow (humans edit on-disk markdown).
- **`/end`-skill git-mv coupling** (decision 15) splits status-transition logic between the server and the skill, which is the kind of seam that breeds drift.

Meanwhile, the user reports being "okay with there not being markdown files if that significantly simplifies things, though I would want to add a web front end for human viewing/editing." That's the trigger.

Note: this plan does NOT touch substrate's hand-curated `/projects/substrate/breadcrumbs/` directory. That's an independent practice; agent-notes-mcp simply stops trying to keep its own DB in sync with files on disk.

## 2. Design decisions

Numbered to extend Plan 001 + Plan 002. Reserves 39–48 for this plan.

39. **The DB is the only source of truth. Period.** Removes the "DB is SoT, markdown is projection" framing from Plan 001 decision 4. There is no projection. There is the DB; there are the MCP tools; there is the web frontend reading from the DB.

40. **All projection apparatus is deleted, not deprecated.** `core/projection.py`, `safe_write`, `compute_projection_paths`, `reconcile_projection`, `render_index`, `projects.breadcrumbs_dir`, `breadcrumbs.projection_sha256`, `breadcrumbs.projection_dirty`. Reason: leaving them as deprecated-but-present is the worst of both worlds — code complexity stays, but no one trusts the path. Either it's load-bearing or it's gone; this plan picks gone.

41. **The web frontend is a separate process with a separate entry point.** New module `agent_notes.web`, new console script `agent-notes-web`. Shares the model layer (`BreadcrumbModel`, `MemoryModel`) and the embedding singleton with the MCP servers but runs independently. Reason: web is HTTP; MCP is stdio; trying to mount one inside the other is a layering inversion.

42. **Read-only first; editing second.** Phase 8a ships a read-only viewer (~300–500 LOC) that demonstrates the workflow. Phase 8b adds editing once the viewer is enough to validate the UX. Reason: the projection layer was over-built because it was designed for hypothetical edit workflows. Don't repeat that.

43. **Localhost-only, no auth, in v1.** Bind to `127.0.0.1`. Single user. If a remote-access need ever materializes, that's a separate plan and a different threat model. Reason: every authentication scheme added at this stage is speculative; localhost is the only environment that exists today.

44. **FastAPI + server-rendered HTML (Jinja2 + HTMX, or plain).** Not React, not a separate SPA. Reason: the data is read-heavy, list-and-detail-shaped, and well-served by server-side rendering. SPA scaffolding costs more than the entire viewer.

45. **Search goes through `search_all_notes` and the existing kind queries.** No new search backend. The web frontend is a presentation layer on top of the model layer; it doesn't get to invent its own SQL.

46. **`/end` skill becomes a no-op for projection.** It still runs `reflect` and commits any other working-directory changes. The MCP server already does not run git (Plan 001 decision 15); removing projection means there's nothing for `/end` to project. Reason: the skill becomes trivial, which is the goal.

47. **The legacy import scripts stay; they're one-time.** `import_legacy_bc.py`, `import_legacy_memory.py`, `import_reflections.py` are historical and useful for any future repo migration. Don't delete them — they're not coupled to the projection apparatus.

48. **Migration is destructive and irreversible.** The `ALTER TABLE` drops `projection_sha256`, `projection_dirty`, `breadcrumbs_dir`. Existing markdown files on disk are not deleted by the migration (that would be reckless); but they're no longer touched by the server. A user who wants to keep them around can; the server forgets they ever existed. Reason: irreversible is fine for a one-person OSS project; the cost of building a "projection compatibility mode" exceeds the cost of committing.

## 3. Schema delta

```sql
-- Decision 40: drop the projection columns.
ALTER TABLE breadcrumbs
    DROP COLUMN projection_sha256,
    DROP COLUMN projection_dirty;

ALTER TABLE projects
    DROP COLUMN breadcrumbs_dir;

-- repo_root stays — Plan 002 decision 33 still uses it for resolve_project.
```

No new tables. The web frontend reads from the existing schema.

## 4. Code delta

Deleted (~600 LOC + supporting tests):
- `src/agent_notes/core/projection.py` (184 LOC)
- Projection-writing branches in `servers/breadcrumbs.py` (`_write_projection`, related helpers)
- `compute_projection_paths` and `reconcile_projection` tool registrations
- Projection-related lines in `scripts/doctor.py` (the `breadcrumbs_dir` check)
- `scripts/regenerate_markdown.py` (whole file)
- Projection-related tests in `tests/test_projection.py`

Added (estimate ~500 LOC for 8a read-only, +200 LOC for 8b editing):
- `src/agent_notes/web/__init__.py`
- `src/agent_notes/web/app.py` — FastAPI app, routes, template wiring
- `src/agent_notes/web/templates/` — Jinja2 templates (index, project, breadcrumb-detail, memory-detail, search)
- `src/agent_notes/web/static/` — minimal CSS, optional HTMX
- New entry point `agent-notes-web = agent_notes.web.app:main` in `pyproject.toml`
- `tests/test_web.py` — route tests using FastAPI's TestClient

Net change: roughly −100 LOC after 8a; roughly +100 LOC after 8b. Removing the projection complexity is its own win independent of the headcount.

## 5. Phased implementation

### Phase 8a — Remove projection, add read-only viewer (one session)

| # | Task | Outcome |
|---|---|---|
| 8a.1 | Confirm Plan 002 Phase 7a status. If 7a's BC-006 fix has landed, note that its `PROJECT_NOT_CONFIGURED` error path will be deleted in 8a.4 — fine, but mark it in the commit message | Ordering question closed |
| 8a.2 | Schema migration (`schema/500_drop_projection.sql`): drops three columns. Idempotent (`IF EXISTS`). Document as irreversible | `agent-notes-migrate` applies cleanly to dev DB |
| 8a.3 | Delete `core/projection.py`, projection branches in `servers/breadcrumbs.py`, `scripts/regenerate_markdown.py`, projection tests | `make test` green |
| 8a.4 | Remove `breadcrumbs_dir` check from `doctor.py`; remove `compute_projection_paths` / `reconcile_projection` from MCP tool surface | Tool surface shrinks; doctor simpler |
| 8a.5 | New `agent_notes.web` module: FastAPI app with routes for `/`, `/projects/<slug>`, `/breadcrumbs/<id>`, `/memories/<name>`, `/search` | Read-only browse works |
| 8a.6 | Templates for the five routes; minimal CSS | Pages render readably |
| 8a.7 | `agent-notes-web` entry point in `pyproject.toml`; bind `127.0.0.1` only; default port 8765 (configurable via `AGENT_NOTES_WEB_PORT`) | One command starts the viewer |
| 8a.8 | `tests/test_web.py` covering each route's 200/404/empty-state paths via FastAPI TestClient | `make test` green |
| 8a.9 | Update README and AGENTS.md: remove projection vocabulary; add web-frontend section | Documentation matches reality |
| 8a.10 | File a breadcrumb noting on-disk markdown files in existing repos are no longer touched, with a one-paragraph recommendation (delete them, archive them, or leave them — user's call) | Operators know what to do |

### Phase 8b — Editing through the web (one session, when triggered)

Trigger: Phase 8a has shipped, the user has used the viewer for at least a week, and editing in the agent (via MCP tools) feels insufficient.

| # | Task | Outcome |
|---|---|---|
| 8b.1 | POST routes for `file_breadcrumb`, `update_breadcrumb`, `add_memory`, `mark_stale` (if Plan 002 7c shipped), `add_link`, `remove_link` | All write operations available in the browser |
| 8b.2 | Forms wired via HTMX (or plain form POSTs); validation errors render inline | Web edit UX matches MCP tool behavior |
| 8b.3 | CSRF token on all POST routes (defense in depth even on localhost) | Standard hygiene |
| 8b.4 | Tests | `make test` green |

### Phase 8c — Backups and export (deferred, ships when triggered)

Trigger: the user wants to share a snapshot with another machine or a collaborator.

| # | Task | Outcome |
|---|---|---|
| 8c.1 | `agent-notes export` CLI that dumps the DB to a portable JSON or `pg_dump` archive | One-command snapshot |
| 8c.2 | `agent-notes import` for the inverse | Round-trip works |

This phase is the honest replacement for "markdown is committed to git so it's backed up." Doesn't need to ship in 8a/8b.

## 6. Considered and rejected

| # | Proposal | Rejection rationale |
|---|---|---|
| Q1 | Keep projection as opt-in per project (decision 24's existing "opt-in per kind" extended one level deeper) | Two code paths for the same data is the worst outcome. Either commit to projection or remove it; "opt-in" preserves the BC-006-class bugs for the projects that opt in. |
| Q2 | Write projection on demand via a tool (`emit_markdown(project)`) | Same complexity, just lazy. The `safe_write` hash apparatus is needed the moment someone edits the resulting file. If we want one-shot dumps, that's `agent-notes export` (Phase 8c), not a projection. |
| Q3 | Make the web frontend writable in 8a (no 8b split) | Doubles 8a's scope and locks in UX decisions before the viewer has been used. Read-only first is cheap to revisit. |
| Q4 | React / SPA / Next.js | Decision 44. Wrong tool for read-heavy list/detail browsing of a small dataset by one user. |
| Q5 | Add auth in 8a | Decision 43. Speculative threat model; one-user localhost has none. |
| Q6 | Build a TUI instead of a web frontend | Considered. A TUI is closer to the existing CLI ethos, but the user named "web frontend" explicitly. The cost is similar; the discoverability is much better for a web frontend (URL → share with collaborator later). |
| Q7 | Ship the web frontend without removing projection | The whole point of the plan is to remove the complexity. Shipping both at once means the projection bugs keep biting while the web frontend ramps up; sequence the cleanup first. |

## 7. Risks and mitigations

- **The web frontend turns into a tar pit.** Mitigation: 8a is read-only and capped at ~500 LOC. If it grows past that during implementation, stop and ask why. The viewer should be boring.
- **A consumer outside this repo depends on `breadcrumbs/*.md` files** (the user reads them, an external script greps them, etc.). Mitigation: 8a.10 surfaces this explicitly; existing on-disk files are not deleted by the migration; substrate's hand-curated dir is independent and unaffected.
- **`/end` skill breaks.** Mitigation: decision 46. Skill simplifies — there's literally less to do. Touch the skill in 8a as part of documentation.
- **Embedding model loads in two processes** (MCP server + web). Mitigation: web doesn't need the model for read-only browsing (no semantic search yet); only the search route triggers loading, and only if the user opts into semantic search vs structured filters. If both are running and both need it, that's ~540MB resident; the omnibus mode's tradeoff (Plan 001 §10) already covers this thinking.
- **Port collision (8765 in use).** Mitigation: `AGENT_NOTES_WEB_PORT` env var; doctor surfaces the current binding.
- **CSRF on localhost feels paranoid** but defends against drive-by web attacks (a malicious webpage POSTing to localhost). Cheap to add in 8b; don't skip.
- **Schema migration is irreversible** (decision 48). Mitigation: take a `pg_dump` before running it. Document this in the migration file's header comment.

## 8. Open questions

1. **Ordering with Plan 002 Phase 7a.** Default: 7a ships first because it unblocks BC-006/007 today; 8a deletes the now-vestigial BC-006 path. Inverted ordering (8a first) skips the BC-006 fix entirely but blocks 7a's path-based resolution improvements behind a bigger change. Recommend default ordering.
2. **HTMX vs no JS.** Read-only 8a doesn't need either; pure server-rendered HTML works. Decide during 8b when forms enter the picture.
3. **Should the web frontend show `change_log` / history?** Probably yes (it's already there in the data), but defer to 8b.

## 9. Status

Proposed v0.1; ready for one round of peer review (Plan 002 decision 38). New evidence (not restated proposals) welcome.
