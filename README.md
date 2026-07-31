# agent-notes

Pgvector-backed memory layer for agent harnesses: breadcrumbs (issue tracker), memories (cross-session facts), and reflections (session retrospectives) — shared across Claude Code, opencode, and any harness that can shell to a CLI.

Consolidates and supersedes the standalone `breadcrumb-mcp` and `memory-mcp` projects. Originally built as an MCP omnibus server; Plan 004 stripped MCP in favor of a CLI + skills + NOTIFY-bridge shape. See `plans/004-flatten-cli-and-async-bridge.md` for the rationale and `plans/001-architecture-and-implementation.md` for the original architecture / peer-review history.

## Status

- **Plan 004** (MCP→CLI flattening): Phases 9a–9d complete. CLI is the primary sync surface; skills installed across Claude Code and opencode; MCP servers removed.
- **Plan 007** (lifecycle enforcement spine): Piece 0 (error contract), Piece 1 (`init` + `orient` + Claude Code `SessionStart` hook), and Piece 2 (opencode plugin with `experimental.chat.system.transform` orientation + `experimental.session.compacting` reconciliation) complete. Both harnesses now enforce the lifecycle spine.
- **Plan 008** (work-log coordination kernel): **P0–P4 complete; Tier A shipped.** The op-CRDT kernel (`op_log`, `work_items` cache, `content_blobs`, verifier), status lattice, merge/reconcile, cross-project derived index (registry, export/ingest, reverse-edge map), cross-project trigger loop, local lease table, and claim/heartbeat/release CLI are all shipped. **Tier A degrade contract is the default safe mode** — `doctor` reports `coordinator-absent / local-lease`; the `breadcrumb → work-item` migration has been executed and verified (cache rebuild from op-log matches). The **regista coordinator integration is an optional, not-yet-attached L3 layer** (Tier B) — the tool works correctly without it; it adds only race-free multi-writer claims at concurrent scale. The `requeue_expired` daemon timer is also Tier B.

## Quickstart

```bash
cd /projects/agent-notes
uv venv && uv pip install -e ".[test]"

# Configure
export AGENT_NOTES_DSN="postgresql://user:pass@host:5432/agent_notes"

# Create schema
agent-notes-setup

# Register your project
agent-notes init .

# File a work item (new kernel model)
agent-notes work-item file --title "Found a bug" --type bug --status open

# Add a memory
agent-notes memory add --name "postgres-tuning" --body "..." --type note
```

### CLI surface

```
agent-notes init [path]
agent-notes resolve [--path PATH] [--json]
agent-notes doctor [--json]

agent-notes breadcrumb file --title T --body B [--type ...] [--status ...] [--path PATH] [--json]
agent-notes breadcrumb update <id> [--status ...] [--body ...] [--append-body ...] [--json]
agent-notes breadcrumb get <id> [--json]
agent-notes breadcrumb find [--status ...] [--type ...] [--text ...] [--path PATH] [--json]
agent-notes breadcrumb delete <id> [--path PATH] [--json]

agent-notes memory add --name N --body B [--type ...] [--path PATH] [--json]
agent-notes memory get <name> [--json]
agent-notes memory list [--path PATH] [--json]
agent-notes memory search "<query>" [--path PATH] [--json]
agent-notes memory update <name> [--body B] [--attributes JSON] [--path PATH] [--json]
agent-notes memory delete <name>

agent-notes export [--path PATH] [--workspace W] [--project P] > backup.json
agent-notes import backup.json

agent-notes link add --from <kind:id> --to <kind:id> --type <type>
agent-notes link remove --from <kind:id> --to <kind:id> --type <type>
agent-notes link trace <kind:id> [--all] [--depth N] [--json]

agent-notes search all "<query>" [--path PATH] [--json]
agent-notes search breadcrumb "<query>" [--path PATH] [--json]
agent-notes search memory "<query>" [--path PATH] [--json]

agent-notes vocabulary list [--kind ...] [--path PATH] [--json]
agent-notes vocabulary add --workspace <slug> <kind> <name> [--sort-order N] [--terminal] [--closed] [--json]
agent-notes vocabulary archive <kind> <value>

agent-notes work-item file --title T [--body B] [--type ...] [--status ...] [--path PATH] [--json]
agent-notes work-item update <id> [--status ...] [--body ...] [--append-body ...] [--json]
agent-notes work-item get <id> [--with-body] [--json]
agent-notes work-item find [--status ...] [--type ...] [--text ...] [--path PATH] [--json]
agent-notes work-item ready [--path PATH] [--json]
agent-notes work-item claimable [--path PATH] [--json]
agent-notes work-item claim <id> [--actor-id ...] [--ttl 300] [--json]
agent-notes work-item release <id> [--actor-id ...] [--json]
agent-notes work-item heartbeat <id> [--actor-id ...] [--ttl 300] [--json]
agent-notes work-item requeue-expired [--json]
agent-notes work-item close <id> [--path PATH] [--json]
agent-notes work-item attest-gate <id> --reason <txt> [--path PATH] [--json]
agent-notes work-item review list [--path PATH] [--json]
agent-notes work-item review pass    <id> --note <txt> [--actor-id ...] [--model-lineage ...] [--same-lineage-acknowledged] [--json]
agent-notes work-item review accept  <id> --note <txt> [--actor-id ...] [--model-lineage ...] [--json]
agent-notes work-item review reject  <id> --note <txt> [--actor-id ...] [--model-lineage ...] [--json]
agent-notes work-item review request-changes <id> --note <txt> [--actor-id ...] [--json]


agent-notes changes since <timestamp-or-id> [--json]

agent-notes install-skills [--target claude|opencode] [--dry-run]
```

Suite harness installation accepts `claude`, `opencode`, `codex`, `hermes`, or
`all`. `agent-notes install-harness codex` installs the canonical skills under
`$HOME/.agents/skills` and records hash-tracked ownership under `$CODEX_HOME`
(default `~/.codex`). The repository is also a component-owned Codex plugin,
with `plugins/agent-notes/.codex-plugin/plugin.json` containing a small,
drift-checked bundle for suite marketplace composition. Keeping the plugin
below `plugins/agent-notes` prevents Codex from packaging the checkout's
multi-gigabyte `.venv`. Both paths install the same narrow lifecycle
adapter: `SessionStart` injects bounded metadata-only orientation and `Stop`
performs best-effort outbox reconciliation without requesting another turn.
The direct fallback hash-owns only its groups in `$CODEX_HOME/hooks.json` and
preserves Cairn/user hooks. Review and trust command hooks explicitly with
Codex `/hooks`; doctor reports that trust as unverified because Codex exposes no
machine-readable trust probe. Live skill conformance remains open in Plan 019,
so stable `all` remains Claude and OpenCode until the full adapter set is
promoted atomically. Component-private Hermes remains explicitly selectable and
is never implicit.

### Skills (Claude Code / OpenCode / Codex)

This repo ships skill prose under `skills/` that turns the CLI into
a discoverable agent surface. Each skill carries the *judgment* —
when to file a breadcrumb, what fields matter, how to phrase a memory
— alongside a thin shell to the CLI. Per Plan 004 decision 53, the
CLI is dumb storage; the policy lives in the skill Markdown.

| Skill | Purpose |
|---|---|
| `file-breadcrumb` | "I found a problem worth tracking" workflow. |
| `update-breadcrumb` | Status transitions, body appends, resolving. |
| `find-breadcrumb` | Search-before-file dedup helper. |
| `add-memory` | Cross-session fact recording with naming/dedup. |
| `start` | Session-start orientation. |
| `reflect` | Write a session reflection (now via CLI, not MCP). |
| `end` | Wrap-up: reconcile breadcrumbs, reflect, commit. |

Install with:

```bash
agent-notes install-skills --target claude       # → ~/.claude/skills/<name>/SKILL.md
agent-notes install-skills --target opencode     # → ~/.config/opencode/command/<name>.md
# Add --dry-run to preview without writing.
```

### Opencode plugin (lifecycle enforcement)

For **automatic** session-start orientation and compaction reconciliation,
use the opencode plugin (Plan 007 Piece 2). Unlike skills (which the
model chooses to invoke), the plugin injects prompts into the system
prompt and compaction context on every session:

```bash
# Add to ~/.config/opencode/opencode.json:
#   "plugin": ["/projects/agent-notes/integrations/opencode/index.js"]
```

See `integrations/opencode/README.md` for full setup.

Both targets read the same `skills/<name>/SKILL.md` source. The
opencode target strips the `name:` line from YAML frontmatter
(opencode derives the name from the filename). Re-running is
idempotent — a second invocation with no source changes reports
`unchanged` for every skill.

### NOTIFY → agent-wake bridge (optional)

`agent-notes-bridge` is a small daemon (Plan 004 §7, Phase 9c) that LISTENs
on the Postgres `agent_notes_changes` channel and POSTs each change to an
[agent-wake](/projects/agent-wake) HTTP ingest endpoint with HMAC-signed
requests (`X-AgentWake-Source` / `X-AgentWake-Signature`). It lets external
agents wake on breadcrumb / memory changes without polling.

**Setup (two sides):**

1. **agent-notes side** — set env vars and run the bridge:

```bash
export AGENT_NOTES_DSN=postgresql://...
export AGENT_NOTES_BRIDGE_TARGET=http://127.0.0.1:8788/
export AGENT_NOTES_BRIDGE_SECRET=$(python3 /projects/agent-wake/tools/generate-secret.py)
agent-notes-bridge
```

2. **agent-wake side** — add `agent-notes` as a source in
   `~/.config/agent-wake/config.json`:

```json
{
  "version": 1,
  "listen": {"host": "127.0.0.1", "port": 8788},
  "sources": {
    "agent-notes": {
      "secret_env": "AGENT_NOTES_BRIDGE_SECRET"
    }
  },
  "routing": {
    "agent-notes": {"adapter": "claude"}
  }
}
```

Then restart `agent-waked`. The bridge sends `wake: false` (silent inject),
but Claude Code's channel plugin always wakes the agent in v0 — this is a
known agent-wake limitation, not a bridge bug.

Optional env vars: `AGENT_NOTES_BRIDGE_SOURCE` (default `agent-notes`),
`AGENT_NOTES_BRIDGE_BATCH_MS` (default 100ms), `AGENT_NOTES_BRIDGE_BATCH_N`
(default 50). Each buffered change becomes one POST to the target. On 3
failed delivery attempts (100ms / 1s / 10s backoff), the bridge drops the
event and continues — the `change_log` row remains the durable record.

A sample systemd unit lives at `deploy/agent-notes-bridge.service`. The
bridge does **not** publish to regista (Plan 004 decision 56); subscribe
to the wake target if a downstream regista consumer needs the stream.

### Cross-project trigger loop (optional, Plan 008 P3)

`agent-notes-trigger-loop` listens on the Postgres `agent_notes_op_log_events`
NOTIFY channel and routes cross-project wake events to the appropriate
project's wake channel:

- `request.created` → target project's wake channel
- `link.added` (cross-project) → target project's wake channel (as `dependency.blocked`)
- `item.closed` → dependent projects' wake channels (as `dependency.resolved`)

It reuses the same env vars as the bridge (`AGENT_NOTES_BRIDGE_TARGET`,
`AGENT_NOTES_BRIDGE_SECRET`, `AGENT_NOTES_BRIDGE_SOURCE`, `AGENT_NOTES_BRIDGE_BATCH_MS`,
`AGENT_NOTES_BRIDGE_BATCH_N`). The target project is resolved via the registry
(`projects.wake_channel`); if unset, the default bridge target is used.

```bash
export AGENT_NOTES_DSN=postgresql://...
export AGENT_NOTES_BRIDGE_TARGET=http://127.0.0.1:8788/
export AGENT_NOTES_BRIDGE_SECRET=$(python3 /projects/agent-wake/tools/generate-secret.py)
agent-notes-trigger-loop
```

The trigger loop is **best-effort** (Invariant W). A lost wake is recovered by
the level-tail (`events --since`) on the next SessionStart.

### Web viewer (read-only)

```bash
agent-notes-web
# Opens on http://127.0.0.1:8765
# Port configurable via AGENT_NOTES_WEB_PORT env var
```

Browse breadcrumbs, memories, and run semantic search from a browser. Localhost-only by default.

Optional bearer-token auth is available via the `AGENT_NOTES_WEB_TOKEN` env var:

```bash
export AGENT_NOTES_WEB_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe())")
agent-notes-web
```

When `AGENT_NOTES_WEB_TOKEN` is set, every request must include
`Authorization: Bearer <token>`. If the env var is unset, the viewer remains
open (backward compatible).

### Migration & Rollback

The v1.0.0 migration (`schema/800_drop_breadcrumbs.sql`) drops the legacy
`breadcrumbs` table. **Before upgrading**, back up your database:

```bash
pg_dump -h host -U user agent_notes > backup_pre_1.0.0.sql
```

To roll back to the pre-1.0.0 version:

1. Stop all agent-notes processes (web, bridge, trigger-loop).
2. Restore from backup: `psql -h host -U user agent_notes < backup_pre_1.0.0.sql`
3. Check out the pre-kernel commit: `git checkout b366c5f` (the last commit before the Plan 008 kernel work).
4. Re-run migrations: `agent-notes-setup`

The `op_log` table is never dropped by any migration. `800_drop_breadcrumbs.sql`
drops the now-unused legacy `breadcrumbs` table that the `op_log` has already
superseded. All other schema files use `IF NOT EXISTS` and are safe to re-run.

### Breadcrumb → Work-item alias

The `breadcrumb` CLI subcommand is a **partial alias** for `work-item` — the
shared verbs (`file`, `update`, `get`, `find`, `delete`) delegate to the same
`WorkItemModel` backend. The `breadcrumb` name is kept for backward
compatibility with existing skills and agent muscle memory. New code should
use `work-item`.

Three `breadcrumb`-only verbs have **no** `work-item` equivalent:

- `breadcrumb sync --from-files ...` — syncs breadcrumbs from a files directory
- `breadcrumb export-index` — writes a plain-text fallback index
- `breadcrumb reconcile [--apply]` — scans git for silent-resolution drift

These are legacy/transition helpers. The `reconcile` functionality is also
available via `agent-notes orient --reconcile`.

The vocabulary namespaces (`bc_kind` → `wi_kind`, `bc_status` → `wi_status`,
`bc_severity` → `wi_severity`) are similarly aliased. Pre-existing `bc_*`
vocabulary entries continue to work; new entries should use `wi_*`.

## Entry points

| Console script | Kind(s) | Status |
|---|---|---|
| `agent-notes` | All | CLI — primary sync surface |
| `agent-notes-bridge` | — | NOTIFY → agent-wake forwarder (optional) |
| `agent-notes-trigger-loop` | — | Cross-project wake routing (optional, P3) |
| `agent-notes-web` | — | Read-only browser viewer |
| `agent-notes-setup` | — | Alias for `migrate --all` |
| `agent-notes-migrate` | — | Schema migrations. The DDL ships inside the wheel (`agent_notes/schema/`) and is resolved with `importlib.resources`, so `--all` works on an artifact-only host with no checkout; `--list [--json]` reports the migrations found and where they resolved from, without needing a database |
| `agent-notes-doctor` | — | Health check: DSN, schema, model, links audit |
| `agent-notes-import-reflections` | — | One-time reflection import |

See `AGENTS.md` for build/test/lint commands and contributor conventions.
