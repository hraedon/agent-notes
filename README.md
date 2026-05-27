# agent-notes

Pgvector-backed memory layer for agent harnesses: breadcrumbs (issue tracker), memories (cross-session facts), and reflections (session retrospectives) — shared across Claude Code, opencode, and any harness that can shell to a CLI.

Consolidates and supersedes the standalone `breadcrumb-mcp` and `memory-mcp` projects. Originally built as an MCP omnibus server; Plan 004 stripped MCP in favor of a CLI + skills + NOTIFY-bridge shape. See `plans/004-flatten-cli-and-async-bridge.md` for the rationale and `plans/001-architecture-and-implementation.md` for the original architecture / peer-review history.

## Status

CLI flattening (Plan 004 Phase 9a) complete and shipping. Skills (Phase 9b) installed across Claude Code and opencode. NOTIFY→agent-wake bridge (Phase 9c) implemented; live integration gated on agent-wake v0 tag. MCP servers removed (Phase 9d).

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

# File a breadcrumb
agent-notes breadcrumb file --title "Found a bug" --kind bug --status new

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
agent-notes breadcrumb query "<filter>" [--json]
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

agent-notes changes since <timestamp-or-id> [--json]

agent-notes install-skills [--target claude|opencode] [--dry-run]
```

### Skills (Claude Code / opencode)

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

Both targets read the same `skills/<name>/SKILL.md` source. The
opencode target strips the `name:` line from YAML frontmatter
(opencode derives the name from the filename). Re-running is
idempotent — a second invocation with no source changes reports
`unchanged` for every skill.

### NOTIFY → agent-wake bridge (optional)

`agent-notes-bridge` is a small daemon (Plan 004 §7, Phase 9c) that LISTENs
on the Postgres `agent_notes_changes` channel and POSTs each change to an
[agent-wake](https://github.com/) HTTP ingest endpoint with HMAC-signed
requests (`X-AgentWake-Source` / `X-AgentWake-Signature`). It lets external
agents wake on breadcrumb / memory changes without polling.

Run it (one process per deployment):

```bash
export AGENT_NOTES_DSN=postgresql://...
export AGENT_NOTES_BRIDGE_TARGET=http://127.0.0.1:8788/   # agent-wake ingest
export AGENT_NOTES_BRIDGE_SECRET=...                       # shared HMAC secret
agent-notes-bridge
```

Optional env vars: `AGENT_NOTES_BRIDGE_SOURCE` (default `agent-notes`),
`AGENT_NOTES_BRIDGE_BATCH_MS` (default 100ms), `AGENT_NOTES_BRIDGE_BATCH_N`
(default 50). Each buffered change becomes one POST to the target (agent-wake's
v0 wire schema is one-event-per-POST). On 3 failed delivery attempts
(100ms / 1s / 10s backoff), the bridge drops the event and continues — the
`change_log` row remains the durable record.

A sample systemd unit lives at `deploy/agent-notes-bridge.service`. The
bridge does **not** publish to substrate (Plan 004 decision 56); subscribe
to the wake target if a downstream substrate consumer needs the stream.

### Web viewer (read-only)

```bash
agent-notes-web
# Opens on http://127.0.0.1:8765
# Port configurable via AGENT_NOTES_WEB_PORT env var
```

Browse breadcrumbs, memories, and run semantic search from a browser. No auth; localhost-only.

## Entry points

| Console script | Kind(s) | Status |
|---|---|---|
| `agent-notes` | All | CLI — primary sync surface |
| `agent-notes-bridge` | — | NOTIFY → agent-wake forwarder (optional) |
| `agent-notes-web` | — | Read-only browser viewer |
| `agent-notes-setup` | — | Alias for `migrate --all` |
| `agent-notes-migrate` | — | Schema migrations from `schema/*.sql` |
| `agent-notes-doctor` | — | Health check: DSN, schema, model, links audit |
| `agent-notes-import-reflections` | — | One-time reflection import |

See `AGENTS.md` for build/test/lint commands and contributor conventions.
