# AGENTS.md

Conventions and quick reference for agents (and humans) working on agent-notes.

> **Active again (2026-10-28).** agent-notes is the post-AOS coordination
> home: memory tiers, mailbox, and wake (`plans/025-coordination-after-aos.md`).
> Native Postgres is the authority for work items; regista write-through is
> retired (off by default, unsupported going forward).

> **Upstream renamed 2026-05-27:** the coordination spine was previously `substrate`; it is now `regista` (Plan 005 consumer migration here, regista Plan 018 upstream). Older skill text, plans, and reflections that still say "substrate" are intentional historical record.

## Orient

1. **Read first:** `plans/001-architecture-and-implementation.md` — authoritative architecture, design decisions (numbered, peer-reviewed), and phased task tables.
2. **Then:** `plans/003-drop-projection-add-web-frontend.md` for the projection-removal and web frontend changes (Phase 8a — complete).
3. **Then:** `plans/004-flatten-cli-and-async-bridge.md` for the MCP->CLI flattening (Phase 9a–9d — complete).
4. **Then:** `plans/007-lifecycle-enforcement-spine.md` for the lifecycle enforcement and cross-harness instantiation (Piece 0–2 complete).
5. **Then:** `plans/008-work-log-coordination-kernel.md` for the op-CRDT work log, provenance, and cross-project coordination (P0–P4 complete; Tier A shipped — regista coordinator is an optional L3 layer, not yet attached).
6. **Then:** `plans/016-review-gate-cli-and-work-item-split.md` for the `work-item review` CLI surface and the `core/work_item/` subpackage split (`work_item_model.py` is now a thin dispatch facade).
7. **For the next architecture arcs:** `plans/025-coordination-after-aos.md`
   (memory tiers, mailbox, wake; implemented), then
   `plans/026-retire-the-regista-layer.md` (native-only work items and memory,
   an enforced review gate) and
   `plans/027-memory-roadmap-curation-and-recall.md` (measured recall, harvest
   of harness-native memory, and periodic curation). Plans 020 and 022 are
   superseded.

## Build / test / lint

```bash
make dev            # uv pip install -e ".[test]" (plain editable install)

make test           # runs pytest against ephemeral Postgres (testcontainers)
make lint           # ruff
make fmt            # ruff format
```

agent-notes has no sibling-repo dependency: Plan 026 D3 vendored the few
regista pieces it used (principal grammar, error codes) and dropped
`regista-hraedon`, `SUITE.lock` and the `DEV_AGAINST` machinery. A fresh venv
needs only `uv pip install -e ".[test]"`.

Tests for triggers, recursive CTEs, and `change_log` semantics must run against real Postgres — no DB mocks for those. Use `testcontainers[postgres]` (already in `[test]` extras) for ephemeral instances in CI.

## Architecture in one paragraph

One Postgres database (`agent_notes`), one Python package (`agent_notes`), one `Server` base class with composable kind registries. The **CLI (`agent-notes`) is the primary sync surface** (Plan 004 Phase 9a+). The shared `core/` library handles DB connection pooling, embedding, links, change_log, NOTIFY, and search. A read-only web frontend (`agent-notes-web`) provides browser-based browsing of breadcrumbs, memories, and semantic search. **Native Postgres is the authority for work items** (Plan 025 D7): every write goes to the `op_log` and folds into the `work_items` cache. The regista write-through face (Plan 012–017) was retired in Plan 025 D7 and deleted in Plan 026 D1; `AGENT_NOTES_REGISTA_WRITES` (or `regista.writes_enabled`) is now an error on every write. Rows mirrored during the regista era are sealed into the op-log on their next native write (`core/work_item/_seal.py`). No code path writes physical breadcrumb files — the `bc_files` module is a one-time import path (files→DB), not a write path.

**Work-item model layout (Plan 016, Plan 026 D1):** `core/work_item_model.py` is a thin facade — each public `WorkItemModel` method delegates to free functions in the `core/work_item/` subpackage (`_common`, `_native`, `_queries`, `_cross_project`, plus `_independence`, `_seal` and the review modules). With one write path left it holds no dispatch logic; extend the relevant subpackage module.

**Work-log coordination kernel** (Plan 008, P0–P4): the `op_log` is an append-only operation log (op-CRDT model) with content-addressed blobs, a deterministic fold into the `work_items` cache, a fail-safe status lattice, and a standalone verifier CLI. Cross-project coordination (P3) adds `request`/`wait` ops, a derived index for foreign work items, and a reverse-edge map so the `ready` query spans projects. Claims are local leases; `doctor` reports the coordination mode as `local-lease`. The Tier B coordinator stub (`core/coordinator.py`) was deleted in Plan 026 D1.

## Schema conventions

- **All kinds are project-scoped** (decision 13). Cross-cutting memories live in a `global` project per workspace, and per-machine memories in a `vm:<id>` project (Plan 025 D3); `projects.kind` (`repo`|`vm`|`global`) keeps those synthetic rows out of every project listing, and only the scope helpers in `core/db.py` return them.
- **Vocabularies live in data, not `CHECK` constraints** (decision 5). Trigger-load-bearing attributes (`is_terminal`, `is_open`, `sort_order`) are first-class columns; presentation-only metadata stays in `attributes JSONB` (decision 23).
- **`change_log` is the sole audit/history source** (decision 7). Per-kind history tables are not used.
- **`links` table accepts dangling rows**; filter at query time. `links_audit` reports orphans (Phase 6.3).
- **DB is the only source of truth** (decision 39, Plan 003). Markdown projection was removed in Phase 8a. No files are written to disk by the servers.

## Canonical v6 actor and producer identity

Every authored write resolves one canonical principal before it reaches
regista. The precedence is `AGENT_NOTES_ACTOR_ID` over `REGISTA_PRINCIPAL_ID`,
with both values using the normal process-env / per-user `suite.env` / system
`suite.env` layering. Principal IDs must use the canonical grammar
(`core/actor.py`, vendored from regista 0.7.2), for
example `agent:worker`, `human:operator`, or `service:hooks`; missing or
invalid identity fails closed. There are no per-command actor or model-lineage
overrides.

**Producer and the native review gate (Plan 026 D2).** Every authored
work-item op carries a `producer` block inside its hashed payload: harness,
model, coarse vendor lineage, and session. The model comes from
`AGENT_NOTES_MODEL` (or the transitional `REGISTA_PRODUCER_MODEL`), otherwise
from the calling session's model, which hooks register in
`agent_sessions.model`. `model_lineages` (data, migration 860) maps models to
vendor lineages.

`work-item review pass|accept`, and any close or update that completes review
work, run `core/work_item/_independence.py::check`. The reviewer must have a
known lineage that no implementer since the last bounce shares; for legacy
items with no recorded model, the reviewer's actor must differ. `human:*`
reviewers skip the lineage check, not the actor check.
`--same-lineage-acknowledged REASON` overrides and records why. `--attempt ID`
applies a validated delegated review (Plan 023/025) as the reviewer, once.

This is cooperative enforcement: identity is self-reported. It prevents
accidental self-approval and records overrides; it is not authentication.

The write path resolves the ambient actor through `core/actor.py::write_actor`
(which refuses the retired regista writes setting, then calls `resolve_actor`).
The CLI renders configuration failures as a named error envelope; nothing is
written. Memories and
reflections use the same canonical actor, but remain non-workflow note entities
and do not participate in the work-item review gate.

**Regista configuration is retired (Plan 026 D1).** `REGISTA_DSN`,
`REGISTA_KEY_PATH` and `REGISTA_REQUIRE_SSL` are no longer read.
`AGENT_NOTES_REGISTA_WRITES=1` (or `regista.writes_enabled` in `config.json`)
makes every write fail with `RETIRED_SETTING`, and `doctor` reports it as the
`retired_settings` check; see `docs/spikes/025-regista-cutover.md`.
`install-harness` removes the regista entries it wrote earlier. The regista
library is no longer a dependency (Plan 026 D3): the principal grammar lives in
`core/actor.py`, the envelope's `ErrorCode` vocabulary in `core/errors.py`
(`tests/test_error_envelope.py` pins the JSON). Action delegation
(`AGENT_NOTES_ACTION_DELEGATION_PATHS`) is deleted and the setting is ignored;
a secret-backend ref as the DSN (`env:`, `file:`, `literal:`, `vault:`,
`azure:`) fails with `RETIRED_SETTING`. The `REGISTA_PRINCIPAL_ID` and
`REGISTA_PRODUCER_*` names stay.

**Checking the wiring: `agent-notes invariants probe`** (WI-071). This is the
read-only probe the suite genesis gate consumes
(`agent_suite.genesis_gate`, required check
`agent_notes.session_identity_resolvable`). It answers one question about *this*
environment — "would a work-item write issued right now carry a resolvable,
valid identity?" — by resolving through `actor.resolve_actor`, the same
entry point every authored write uses, and reporting what that call did. It
writes nothing and opens no connection.

```bash
agent-notes invariants probe          # human-readable
agent-notes invariants probe --json   # the gate contract: one JSON doc, exit 0/1, (exit==0)==ok
```

Failure modes are named in each check's `reason`: `resolved`,
`identity_not_configured`, `identity_invalid`, and `probe_error`. The probe
takes no identity flags — its subject is the ambient session identity, so to
test a different configuration set the suite environment the write path
actually reads.

Two deliberate exemptions: `actor_kind="system"` actors (the migration actor)
carry no model and are never counted by the gate, and memories and reflections
(`core/memory_model.py`) resolve no actor at all, because `derive_authors`
never reads them. Moving notes under the same rule is a deliberate decision,
not a tidy-up — it fails `memory add` for every unconfigured caller.

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
`adversarial-review`, `steward-change`, `start`, `reflect`, `end`. Each is a Markdown
SKILL.md that shells to `agent-notes <noun> <verb> --json` and carries
the per-workflow judgment in prose. For desktop Codex, the repo-local catalog
at `.agents/plugins/marketplace.json` exposes the bounded `plugins/agent-notes`
bundle; see `docs/desktop-codex.md`. Keep bundled skills byte-equal to canonical
sources and use single-line or shell-specific command examples. Install with `agent-notes
install-skills --target {claude,opencode}` (skills only) or
`agent-notes install-harness {claude|opencode|codex|all}` (skills + env wiring
+ opencode plugin + opencode subagent definitions — Plan 017 WI-2.1 and
WI-2.3). Codex installs the canonical skills under `$HOME/.agents/skills` and
records hash-tracked ownership under `$CODEX_HOME`. It also hash-owns narrow
`SessionStart`, `UserPromptSubmit` and `Stop` groups (Plan 025 Phase 4:
orientation, agent messages, turn extension) in
`$CODEX_HOME/hooks.json`; the component plugin bundles the same canonical hook
definitions below `plugins/agent-notes` (with drift checks against the canonical
top-level skills). Hook trust remains an explicit operator action through `/hooks`.
`claude` also installs user-level hooks in `~/.claude/settings.json`, `agy`
merges a named hook into `~/.gemini/config/hooks.json`, and `hermes` adds a
`pre_llm_call` shell hook under `hooks:` in `~/.hermes/config.yaml` (Hermes asks
the operator to approve it; install-harness never writes its allowlist); all of
them run the lean `agent-notes-hook <harness> <event>` entry point. `all` is
Claude + OpenCode, plus Codex, agy and Hermes where their home directory
exists. All are
idempotent; `--dry-run` / `--uninstall` supported. See
Plan 017 §Implementation log for the opencode env-via-config-file
decision (D1).

## Opencode adversarial-review subagents

`install-harness opencode` (and `all`) copies the agent definitions from
`.opencode/agents/` into `~/.config/opencode/agents/` and tracks them
in the harness manifest:

- `glm` / `kimi` coding subagents get `task: adversarial-reviewer-*`
  permission so they can dispatch review subagents.
- `adversarial-reviewer-glm`, `adversarial-reviewer-kimi`,
  `adversarial-reviewer-minimax-m3`, and
  `adversarial-reviewer-nemotron-3-ultra` are read-only
  (`edit: deny`) with limited bash access (`agent-notes*`, `git log*`,
  `git diff*`, `git status*`) so they can read work items, inspect git
  history, and drive `agent-notes work-item review ...` gate transitions
  without mutating code.

This is the bridge that lets an agent working inside agent-notes invoke a
subagent to perform adversarial review. In Claude Code the equivalent is the
`adversarial-review` skill (already installed in `~/.claude/skills/`).

**Opencode plugin** (Plan 007 Piece 2, OpenCode 2): `integrations/opencode/index.js`
uses `ctx.session.hook("context")` to inject `agent-notes orient` output into the
first model request of every session, and `ctx.session.hook("compaction")` to
append the reconciliation checklist before compaction. Plan 025 L1: a
`ctx.session.hook("prompt")` claims the session's mailbox messages once per prompt
(`agent-notes msg inbox --claim --json`) and a second `context` hook injects them as
framed peer content into the request's messages (never the system prompt).
This is the non-optional enforcement layer that complements the opt-in skills.
`agent-notes install-harness opencode` copies the package to
`~/.config/opencode/agent-notes` and registers the V2 `plugins` entry.

## End of session

`/end` runs `reflect` and commits any working-directory changes. There is no projection to rebuild (Plan 003 decision 46). During Phase 9a+, `/end` should use the CLI (`agent-notes`) instead of MCP tools where possible.

**Git boundary (decision 15):** the server never *mutates* git — no `git mv`/commit as a side effect of a DB operation (that seam bred drift; `/end` owns git writes). Reconciliation (below) is the one place the tooling *reads* git, and only ever read-only `git log`, never as a side effect of `file`/`update`/`query` — only when explicitly invoked.

**Breadcrumb ↔ git reconciliation.** The store's recurring drift is *silent resolution*: work lands in a commit ("resolve BC-094") but nobody transitions the DB, so the breadcrumb sits open for weeks. `agent-notes breadcrumb reconcile [--apply]` scans recent `git log` for open breadcrumbs referenced with resolution intent and (with `--apply`) resolves them, stamping the commit into `external_refs` for provenance. It's wired into `/end` step 1b (auto-reconcile before manual close) and is available as an opt-in `orient --reconcile` drift section (off by default to keep `orient` git-free and cheap; enable it once in the SessionStart hook so every session surfaces drift without per-session agent action). Detection is conservative — verb-near-identifier with a negation guard (`not done`, `todo`, `WIP`, `revert` are ignored) — but not infallible; review the dry-run before `--apply`.
