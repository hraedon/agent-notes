# agent-notes-mcp

Unified pgvector-backed MCP server for agent notes — breadcrumbs (issue tracker), memories (cross-session facts), and reflections (session retrospectives) — shared across Claude Code, OpenCode, and Gemini CLI for the sf2 agent constellation.

Consolidates and supersedes the standalone `breadcrumb-mcp` and `memory-mcp` projects. See `plans/001-architecture-and-implementation.md` for the architecture, design decisions, peer-review history, and phased implementation roadmap.

## Status

Phases 0–7a complete. Phase 8a (web frontend, projection removal) complete. **Phase 9a (CLI flattening) in progress** — the CLI is the new primary surface; MCP servers are deprecated.

## Quickstart

```bash
cd /projects/agent-notes-mcp
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
agent-notes breadcrumb update <id> [--status ...] [--body ...] [--json]
agent-notes breadcrumb get <id> [--json]
agent-notes breadcrumb find [--status ...] [--type ...] [--text ...] [--path PATH] [--json]
agent-notes breadcrumb query "<filter>" [--json]

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
| `agent-notes` | Generic | CLI (new primary surface) |
| `agent-notes-breadcrumbs` | breadcrumbs | **Deprecated** (Phase 9a+) |
| `agent-notes-memory` | memory | **Deprecated** (Phase 9a+) |
| `agent-notes-search` | search | **Deprecated** (Phase 9a+) |
| `agent-notes-omnibus` | breadcrumbs + memory + search | **Deprecated** (Phase 9a+) |
| `agent-notes-web` | — | Read-only browser viewer |
| `agent-notes-setup` | — | Alias for `migrate --all` |
| `agent-notes-migrate` | — | Schema migrations from `schema/*.sql` |
| `agent-notes-doctor` | — | Health check: DSN, schema, model, links audit |
| `agent-notes-import-reflections` | — | One-time reflection import |

MCP entry points remain operational during Phase 9a–9c but are scheduled for removal in Phase 9d.

See `AGENTS.md` for build/test/lint commands and contributor conventions.
