# Plan 004 — Flatten to CLI, Move Judgment to Skills, Bridge NOTIFY to agent-wake

Status: Proposed v0.1 (2026-05-24). Architectural pivot away from MCP. Net code removal expected; surface area shifts from per-tool JSON-RPC into argparse + skill prose + a ~100 LOC NOTIFY→HTTP bridge.
Scope: Replaces the MCP servers (`agent-notes-breadcrumbs`, `agent-notes-memory`, `agent-notes-search`, omnibus) with a single thin `agent-notes` CLI over the surviving `core/` modules. Moves the "when to file, what fields matter, how to phrase a memory" judgment into Claude Code / opencode skills. Adds a tiny LISTEN→agent-wake bridge so async wake-on-change is no longer an MCP concern.
Consumers: Claude Code, opencode, web viewer (unaffected), future agent-wake-driven workflows.
Supersedes: Plan 001 §3 servers/ tree, Plan 001 decisions 1/6/10/12/16/21 (the MCP transport / per-kind binary / omnibus framing), Plan 002 decision 33 phrased as "MCP tool" (becomes "CLI subcommand"). Web frontend (Plan 003 Phase 8a) is preserved verbatim.

## 1. Why now

Five months in, the MCP transport is paying ongoing cost without ongoing return:

- **Per-tool JSON schemas are dead weight.** The kind tools (`file_breadcrumb`, `update_breadcrumb`, `add_memory`, `find_breadcrumbs`, `query_breadcrumbs`, `add_link`, `remove_link`, `resolve_project`, `changes_since`, `list_workspaces`, `list_projects`, `list_vocabulary`, `archive_vocabulary`, `trace_graph[_all]`, `search_all_notes`, `search_memory`, `extract_gaps`, `mark_gaps_filed`, `find_reflections`) are 19 hand-maintained schemas defending against argument shapes that argparse + a few lines of skill prose carry just as well in practice. Plan 001 decision 21 already left the door open ("evaluate the SDK") — we now know the answer for our usage is "we don't need the framing at all."
- **`resources/list` was never used.** Live discovery via MCP resources adds a code path that no consumer (Claude Code, opencode, web, scripts) has ever exercised in production. `core/resources.py` (125 LOC) is dead weight.
- **The "MCP as chokepoint for `agent-provenance` attestation" argument weakened.** Provenance attests at the harness hook layer — every tool call (Bash, Edit, MCP, whatever) is observable end-to-end through the same adapter. A CLI invocation is just as attestable as an MCP `tools/call`; the provenance demo argument no longer requires a custom MCP server.
- **Judgment lives in two places today and drifts.** Tool descriptions say one thing, skills (`/end`, `/reflect`) say another, and the user's mental model is the third source. Collapsing judgment into skill prose (a single Markdown file per workflow) makes the contract reviewable in one place.
- **Async surface needed something new anyway.** `/projects/agent-wake` was designed and reviewed across Claude Code and opencode while this repo was elsewhere. A "second MCP for events" was being sketched in a prior session whose notes didn't persist; agent-wake *is* that surface, properly extracted. Bridging NOTIFY → wake costs ~100 LOC and avoids building an event MCP.

## 2. Settled target shape

Four surfaces:

| Surface | Form | Size | Status |
|---|---|---|---|
| **Sync** | `agent-notes` CLI (argparse) over `core/db.py`, `core/embed.py`, `core/links.py`, `core/change_log.py` | ~500–800 LOC | New (Phase 9a) |
| **Workflow** | Skills in `~/.claude/skills/` and opencode equivalent: `file-breadcrumb`, `update-breadcrumb`, `add-memory`, `find-breadcrumb`, `reflect` (exists), `end` (exists), `start` (new) | ~200–400 LOC of Markdown prose total | New (Phase 9b) |
| **Async** | LISTEN-on-Postgres → POST-to-agent-wake-ingest bridge with HMAC | ~100 LOC daemon + tests | New (Phase 9c), gated on agent-wake v0 |
| **Web viewer** | Unchanged (Plan 003 Phase 8a) | n/a | Preserved |

Gemini MCP shim: **deferred (YAGNI).** Plan 001 §1's three-harness goal was never validated for Gemini; if it ever returns, the CLI is sufficient (Gemini's CLI tool integration accepts subprocess shells).

## 3. Design decisions

Numbered to extend Plan 003 (which reserved 39–48). 49–60 reserved for Plan 004.

49. **MCP is removed wholesale, not deprecated in place.** Same logic as Plan 003 decision 40: leaving the JSON-RPC loop as a "legacy entry point" is the worst outcome — complexity stays, no one trusts the path. `core/mcp.py`, `core/server.py`, `core/resources.py`, `servers/breadcrumbs.py`, `servers/memory.py`, `servers/search.py`, all the per-binary console scripts, deleted.

50. **`core/` survives unchanged.** `db.py` (404), `embed.py` (55), `links.py` (269), `change_log.py` (157), `notify.py` (56) keep their interfaces. `servers/breadcrumbs_model.py` (415) migrates verbatim from `servers/` into `core/breadcrumbs_model.py`, and `servers/memory.py`'s model layer is extracted likewise into `core/memory_model.py`. Reason: the model layer is the genuine logic; the server shell was the layer that turned out wrong.

51. **CLI shape: `agent-notes <noun> <verb> [flags]`.** Not `agent-notes <verb> --kind <noun>`. Reads naturally (`agent-notes breadcrumb file …`, `agent-notes memory add …`, `agent-notes search all …`). One subparser per noun; subparser-of-subparser for verb. Path-based project resolution is universal — every command accepts `--path` (default cwd) and resolves via `core.db.resolve_project`. `--workspace`/`--project` remain for explicit override.

52. **CLI outputs are stable, machine-parseable, human-readable.** Default human-friendly tabular; `--json` for skill consumption. Exit codes: 0 success, 2 not-found, 3 not-configured, 4 conflict, 1 anything else. Reason: skills will shell out and parse output; an unstable surface burns trust.

53. **Judgment lives in skill prose.** "When to file a breadcrumb," "what makes a memory worth recording," "how to title a finding" — all moves into the skill Markdown. The CLI is dumb storage/retrieval; it never refuses input on judgment grounds (only on schema grounds: missing required field, unknown project, etc.). Reason: the prior MCP tool descriptions tried to carry both schema and policy; only one of those is enforced by the runtime, and the other was duplicating with skill prose anyway.

54. **Skills are committed to this repo under `skills/` and installed by an `agent-notes install-skills [--target claude|opencode]` subcommand.** Reason: skills are part of the product, not a side artifact. Versioned alongside the CLI they wrap. Install command symlinks or copies into `~/.claude/skills/` / opencode's skill path.

55. **NOTIFY bridge is a separate, optional process.** New module `agent_notes.bridge` with entry point `agent-notes-bridge`. LISTENs on `agent_notes_changes` (already wired in `core/notify.py`), buffers, posts to agent-wake's HTTP ingest with `X-Wake-Signature: HMAC-SHA256(secret, body)`. Configurable via `AGENT_NOTES_BRIDGE_TARGET` (URL) and `AGENT_NOTES_BRIDGE_SECRET`. Reason: not every deployment needs wake-on-change; making it a daemon (systemd unit / `tmux` window) keeps the sync path free of network dependencies.

56. **Bridge does not also publish to substrate.** Substrate's event store is the signed Ed25519 hash-chain authoritative event log for work-item-bound coordination (specs, claims, transitions). Breadcrumb mutations are not work-item events in substrate's sense; the substrate-eventlog MCP exists for read-side queries against substrate, not as a publish bus for unrelated mutations. Open question kept in §9 for confirmation. Reason: shoehorning agent-notes mutations into substrate's signed event scheme would couple two release cadences for no clear consumer; if a consumer later needs durable replay of breadcrumb mutations, a substrate workflow can ingest them via the wake bridge as a downstream hook.

57. **The MCP server keeps running until the skills are proven.** Migration is *additive* — the CLI lands first, skills are written and tested against the live CLI, the MCP server stays operational the entire time. The MCP server is only torn down after the user reports a full session worked through skills only. Reason: this is the user's daily breadcrumb workflow; "land the CLI, delete MCP same session" is the bad path.

58. **Argparse, not Click / Typer.** Stdlib, zero added deps. Reason: a 600-LOC CLI doesn't need a framework; this repo already has FastAPI + Jinja2 + psycopg + pgvector pulling weight.

59. **Tests: keep model-layer tests; rewrite server tests as CLI subprocess tests.** `subprocess.run([...], capture_output=True)` against an ephemeral testcontainers Postgres, parse `--json`, assert. Reason: testing the CLI is testing the actual user surface; testing the dispatch table is theater.

60. **The agent-provenance reframing is recorded here, not in agent-provenance's plans.** The first observed wake source attested through provenance's harness adapter becomes the NOTIFY→wake bridge once both ship. Documenting in this plan so future-me doesn't lose the reframing; agent-provenance's docs need an addendum but not a co-authored plan.

## 4. Code delta

**Deleted (~1,500 LOC + supporting tests):**
- `src/agent_notes/core/mcp.py` (112)
- `src/agent_notes/core/server.py` (535)
- `src/agent_notes/core/resources.py` (125)
- `src/agent_notes/servers/breadcrumbs.py` (551 — model preserved by move)
- `src/agent_notes/servers/memory.py` (874 — model portion preserved by move)
- `src/agent_notes/servers/search.py` (420)
- `src/agent_notes/servers/__init__.py`
- Console scripts: `agent-notes-breadcrumbs`, `agent-notes-memory`, `agent-notes-search`, `agent-notes-omnibus`, plus the `serve` / `main_breadcrumbs` / `main_memory` / `main_search` / `main_omnibus` functions in `cli.py`.
- All MCP-transport tests (JSON-RPC framing, tool dispatch, resources/list).

**Moved (verbatim, ~700 LOC):**
- `servers/breadcrumbs_model.py` → `core/breadcrumbs_model.py` (415)
- Model portion of `servers/memory.py` → `core/memory_model.py` (~250)
- Model portion of `servers/search.py` → `core/search.py` (~150)

**Added (~600 LOC CLI + ~100 LOC bridge + ~300 LOC skills):**
- New `src/agent_notes/cli.py` (rewritten; replaces current 116-LOC shim).
- New `src/agent_notes/bridge.py` (NOTIFY → HTTP).
- Console scripts: `agent-notes` (rewritten), `agent-notes-bridge` (new), `agent-notes-web` (unchanged), `agent-notes-migrate` (unchanged), `agent-notes-doctor` (unchanged but loses MCP-specific checks), `agent-notes-setup` (unchanged), `agent-notes-import-reflections` (unchanged).
- New `skills/` directory with Markdown skills.

**Net change:** roughly −1,500 + 700 + 1,000 = +200 LOC, but the +200 is in CLI + bridge + Markdown — surfaces the user actually reads. The −1,500 is dispatch / schema / transport code that nobody reads.

## 5. CLI surface (Phase 9a)

```
agent-notes init [path]
agent-notes resolve [--path PATH] [--json]
agent-notes doctor [--json]

agent-notes breadcrumb file --title T --body B [--type ...] [--status ...] [--path PATH] [--json]
agent-notes breadcrumb update <id> [--status ...] [--body ...] [--json]
agent-notes breadcrumb get <id> [--json]
agent-notes breadcrumb find [--status ...] [--type ...] [--text ...] [--path PATH] [--json]
agent-notes breadcrumb query "<sql-ish filter>" [--json]

agent-notes memory add --name N --body B [--type ...] [--path PATH] [--json]
agent-notes memory get <name> [--json]
agent-notes memory list [--path PATH] [--json]
agent-notes memory search "<query>" [--path PATH] [--json]
agent-notes memory delete <name>

agent-notes link add --from <kind:id> --to <kind:id> --type <type>
agent-notes link remove --from <kind:id> --to <kind:id> --type <type>
agent-notes link trace <kind:id> [--all] [--depth N] [--json]

agent-notes search all "<query>" [--path PATH] [--json]

agent-notes vocabulary list [--kind ...] [--path PATH] [--json]
agent-notes vocabulary archive <kind> <value>

agent-notes changes since <timestamp-or-id> [--json]

agent-notes install-skills [--target claude|opencode] [--dry-run]
```

`reflection` operations stay under `memory` (Plan 001 decision 25 preserved).

## 6. Skills surface (Phase 9b)

In `skills/` (this repo) and installable into `~/.claude/skills/`:

| Skill | Purpose | Calls |
|---|---|---|
| `file-breadcrumb` | "I found a problem worth tracking" workflow. Prompt drives title/body/type judgment. | `agent-notes breadcrumb file --json` |
| `update-breadcrumb` | Status transitions, body appends, marking resolved. | `agent-notes breadcrumb update --json` |
| `find-breadcrumb` | Search-before-file dedup. Prompt instructs to search project + global before filing. | `agent-notes breadcrumb find --json`, `agent-notes search all --json` |
| `add-memory` | Cross-session fact recording. Prompt drives naming convention and de-duplication. | `agent-notes memory add --json`, `agent-notes memory search --json` |
| `start` | Session-start orientation. Prints recent breadcrumbs, active memories, last reflection. | `agent-notes breadcrumb find --status open`, `agent-notes memory list`, `agent-notes changes since` |
| `reflect` (existing, edited) | Now shells to CLI instead of MCP; otherwise unchanged. | `agent-notes memory add --type reflection`, file write to `reflections/` |
| `end` (existing, edited) | Same — no projection (Plan 003), just reflect + commit. | `agent-notes` (no MCP) |

## 7. Bridge surface (Phase 9c, gated on agent-wake v0)

`agent-notes-bridge` daemon:
- LISTEN on `agent_notes_changes` (existing channel from `core/notify.py`).
- Buffer payloads up to 100ms or 50 events, whichever first.
- POST batch to `AGENT_NOTES_BRIDGE_TARGET` (e.g. `http://localhost:7777/wake`) with body `{"events":[…]}` and header `X-Wake-Signature: sha256=<hex>` computed with `AGENT_NOTES_BRIDGE_SECRET`.
- Retry with exponential backoff (3 attempts, 100ms/1s/10s); after that, log and drop. The change_log row is the durable record — replay is a future concern (see §9 open Q3).
- One process, one DB connection (LISTEN-only), one HTTP client. ~100 LOC.

## 8. Phased implementation

Ordering is chosen so the user's existing MCP-based workflow keeps working through 9a + 9b. The MCP server is only deleted in 9d, after skills are validated.

### Phase 9a — CLI lands, MCP server keeps running (one session)

| # | Task | Outcome |
|---|---|---|
| 9a.1 | Move `servers/breadcrumbs_model.py` → `core/breadcrumbs_model.py`. Extract model from `servers/memory.py` → `core/memory_model.py`. Extract model from `servers/search.py` → `core/search.py`. Update imports in existing MCP servers and web. | Models live in `core/`; MCP servers still work via re-export shims. |
| 9a.2 | Rewrite `src/agent_notes/cli.py` with the full noun/verb argparse tree (decision 51). All commands routed through the moved model layer. `--json` and `--path` everywhere. Stable exit codes (decision 52). | Every operation runnable from a shell with no MCP harness. |
| 9a.3 | Add `agent-notes-doctor` checks for new state: skills installed? Bridge reachable (if configured)? Drop MCP-specific checks. | Doctor mirrors the new shape. |
| 9a.4 | Rewrite server tests as CLI subprocess tests (decision 59). Keep model-layer unit tests. | `make test` green. |
| 9a.5 | Update README + AGENTS.md to document CLI surface; mark MCP entry points as "deprecated, removal in Phase 9d." Keep them runnable. | Documentation matches reality. |
| 9a.6 | Run a real session against the CLI (file 3–5 breadcrumbs by hand) to validate the surface before writing skills. | CLI proven on real use. |

**Exit criterion for 9a:** `agent-notes-omnibus` and `agent-notes` CLI both work against the same DB; the user has used the CLI for one session.

### Phase 9b — Skills land, MCP server still runs (one session)

| # | Task | Outcome |
|---|---|---|
| 9b.1 | Author `skills/file-breadcrumb.md`, `skills/update-breadcrumb.md`, `skills/find-breadcrumb.md`, `skills/add-memory.md`, `skills/start.md`. Each shells to `agent-notes … --json` and reasons over output. Prose carries the "when to file" judgment formerly in MCP tool descriptions (decision 53). | Skills authored and committed. |
| 9b.2 | Edit existing `~/.claude/skills/reflect/` and `~/.claude/skills/end/` to use CLI instead of MCP. | Existing skills don't break. |
| 9b.3 | Implement `agent-notes install-skills [--target claude|opencode]` (decision 54). Symlinks or copies into the target's skills directory. Idempotent. | One-command install. |
| 9b.4 | opencode equivalents of each skill (different frontmatter / invocation but same prose). Live in `skills/opencode/` mirror tree. | Cross-harness parity. |
| 9b.5 | Run one full session using only skills (the user does not call `agent-notes` directly, and the Claude Code MCP server is removed from `.mcp.json` for this session only as a smoke test). Capture friction in a reflection. | Skills proven sufficient. |

**Exit criterion for 9b:** The user has completed at least one session — including filing 2+ breadcrumbs, updating one, adding one memory, and running `/end` — using only skills. Friction logged.

### Phase 9c — NOTIFY→wake bridge (one session, gated on agent-wake v0 ingest)

**Hard dependency:** `/projects/agent-wake` v0 has a working HTTP ingest with HMAC gating (per `design/v0-implementation-plan.md` §2). If agent-wake v0 has not shipped, Phase 9c blocks and Phase 9d can still proceed — bridge is genuinely optional.

| # | Task | Outcome |
|---|---|---|
| 9c.1 | `src/agent_notes/bridge.py`: LISTEN, batch, HMAC-sign, POST, retry/backoff. ~100 LOC. | Daemon runnable. |
| 9c.2 | `agent-notes-bridge` console script. Env vars: `AGENT_NOTES_DSN`, `AGENT_NOTES_BRIDGE_TARGET`, `AGENT_NOTES_BRIDGE_SECRET`. | One command starts the bridge. |
| 9c.3 | Integration test: file a breadcrumb via CLI → assert HTTP POST hits a fake server with valid signature. | `make test` green. |
| 9c.4 | Doctor check: `AGENT_NOTES_BRIDGE_TARGET` set + 200 response on health probe. | Misconfiguration surfaces. |
| 9c.5 | README section + systemd unit example in `deploy/`. | Operators can run it. |
| 9c.6 | Note in `agent-provenance` docs: the bridge is the first observed wake source attested through provenance's harness adapter (decision 60). | Reframing recorded cross-repo. |

**Exit criterion for 9c:** A breadcrumb filed locally produces an observable wake event in agent-wake within 1 second.

### Phase 9d — Delete MCP (one session, after 9b proven)

Triggers: 9b exit criterion met *and* the user has used skills exclusively for ≥3 sessions without falling back to direct MCP calls.

| # | Task | Outcome |
|---|---|---|
| 9d.1 | Delete `core/mcp.py`, `core/server.py`, `core/resources.py`, `servers/breadcrumbs.py`, `servers/memory.py`, `servers/search.py`, `servers/__init__.py`. Delete re-export shims from 9a.1 if any. | ~1,500 LOC gone. |
| 9d.2 | Remove `agent-notes-breadcrumbs`, `agent-notes-memory`, `agent-notes-search`, `agent-notes-omnibus` console scripts from `pyproject.toml`. Remove `serve`, `main_breadcrumbs`, `main_memory`, `main_search`, `main_omnibus` from CLI. | Surface contracts. |
| 9d.3 | Delete MCP server entries from Claude Code / opencode harness configs. | No dead config. |
| 9d.4 | Delete MCP-specific tests; confirm CLI test suite covers the same behaviors. | `make test` green. |
| 9d.5 | Update README, AGENTS.md, decision log: mark Plan 001 decisions 1/6/10/12/16/21 as superseded by Plan 004. | Decision history coherent. |

**Exit criterion for 9d:** Repo no longer ships an MCP server; all workflows continue to function.

## 9. Open questions

1. **Substrate-eventlog overlap.** Provisional answer: bridge does *not* publish to substrate (decision 56). Substrate's event store is a signed work-item-bound coordination log with Ed25519 hash chains; agent-notes mutations don't map. The `mcp__substrate-eventlog__*` tools that exist today are read-side queries against substrate, not a publish bus. **Unresolved:** confirm with the operator that no current SF2 workflow expects breadcrumb mutations to appear in substrate's event log. If one does, add a substrate downstream hook in 9c.7 that consumes from the wake target and translates into a substrate workflow event — *not* a parallel publish from the bridge itself.
2. **Data volume verification.** Prior session reported ~254 BCs / 102 memories / 4 links / one dominant project. Could not run `psql` from this read-only survey to reconfirm. **Action:** in 9a.1, run `agent-notes doctor` (or `SELECT count(*) FROM breadcrumbs, memories, links;`) to reconfirm before deleting code. If volume has crossed ~10k rows, the "link-graph traversal needs MCP" argument might reappear; the response is still "CLI with `--json` is sufficient" but worth confirming.
3. **Durable replay for the bridge.** If a bridge crash drops events between LISTEN and POST, the change_log row is preserved but the wake never fires. Acceptable for v1. If a consumer later needs guaranteed delivery, the bridge gains a "high-water mark" cursor stored in a `bridge_state` table and replays from `change_log` on startup. Defer until a consumer demands it.
4. **Skills install path on opencode.** opencode's skill loading conventions are less settled than Claude Code's. Worth confirming the target directory before 9b.4.
5. **Gemini.** Plan 001 framed three harnesses (Claude Code, opencode, Gemini). Gemini was never validated. Plan 004 codifies "Gemini deferred (YAGNI)." If returned to, the answer is "Gemini calls the CLI as a subprocess tool," not "build a Gemini-specific shim."

## 10. Risks and mitigations

- **Skills can't replicate MCP's structured argument validation.** Mitigation: argparse + `--json` + exit codes (decisions 52, 58). Real failures surface to the model as parseable error JSON, not silent corruption.
- **The user's daily workflow breaks during migration.** Mitigation: phases 9a / 9b / 9d explicitly keep the MCP server running until skills are proven (decision 57). MCP deletion is gated on ≥3 successful skill-only sessions.
- **agent-wake v0 slips, 9c blocks indefinitely.** Mitigation: 9a + 9b + 9d are independent of agent-wake. The bridge is optional; the CLI flatten is the load-bearing win. If agent-wake never ships, the bridge stays unbuilt and that's fine.
- **Skill prose drift from CLI behavior.** Mitigation: every skill ends with an "if this command fails, the contract has drifted — file a breadcrumb under this project" footer. Cheap, makes drift detectable.
- **Lost MCP `resources/list` discovery.** Accepted as lost. No consumer used it.
- **Lost per-tool JSON schema validation.** Mitigation: argparse + skill prose. The contract is the union of CLI `--help` output (machine-readable) and skill Markdown (human-readable). Both are reviewable.
- **opencode skill format diverges from Claude Code's.** Mitigation: parallel `skills/claude/` and `skills/opencode/` trees with shared "core prose" includes if needed. Don't try to unify until both formats are stable.

## 11. Considered and rejected

| # | Proposal | Rejection rationale |
|---|---|---|
| Q1 | Keep MCP, add CLI alongside | Two surfaces, two doc paths, two test paths. The whole point is to flatten. |
| Q2 | Use Click or Typer | Decision 58. 600 LOC of argparse is fine; deps multiply for no win. |
| Q3 | Make the bridge publish to substrate instead of (or in addition to) agent-wake | Decision 56. Substrate is for signed work-item events; breadcrumb mutations are not work-item events. If a downstream substrate consumer needs them, route via wake hook, don't fan out. |
| Q4 | Build an event-MCP server (the prior-session sketch) | Replaced by agent-wake. That's exactly what agent-wake is. |
| Q5 | Build a Gemini MCP shim now | YAGNI. Gemini calls CLI subprocesses fine; build the shim if and when Gemini becomes a real consumer. |
| Q6 | Land the CLI and delete MCP in one session | Decision 57. The user's daily workflow is breadcrumbs; gating MCP deletion on real skill use is cheap insurance. |
| Q7 | Make skills generate-only (judgment) and have an LLM still pick tool args via MCP | Same complexity in two places. Decision 53: judgment in prose, schema in argparse. |
| Q8 | Web frontend swallows the CLI (POST routes call CLI internally) | The web reads from the model layer directly (Plan 003). Adding CLI as middleware is an inversion. |

## 12. Status

Proposed v0.1; ready for one round of peer review (Plan 002 decision 38). New evidence welcome; restated proposals not.
