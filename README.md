# agent-notes-mcp

Unified pgvector-backed MCP server for agent notes — breadcrumbs (issue tracker), memories (cross-session facts), and reflections (session retrospectives) — shared across Claude Code, OpenCode, and Gemini CLI for the sf2 agent constellation.

Consolidates and supersedes the standalone `breadcrumb-mcp` and `memory-mcp` projects. See `plans/001-architecture-and-implementation.md` for the architecture, design decisions, peer-review history, and phased implementation roadmap.

## Status

Phases 0–5 complete. Phase 6 in progress.

## Quickstart

```bash
cd /projects/agent-notes-mcp
uv venv && uv pip install -e ".[test]"

# Configure
export AGENT_NOTES_DSN="postgresql://user:pass@host:5432/agent_notes"

# Create schema
agent-notes-setup
```

For machines with **<32 GB RAM**, prefer `agent-notes-omnibus` over running per-kind binaries — it loads the embedding model **once** instead of once per process.

## Running

### Per-kind (default for CI / multi-agent setups)

```bash
agent-notes-breadcrumbs   # breadcrumb server
agent-notes-memory        # memory server
agent-notes-search        # search server
```

### Omnibus (single process, one model load)

```bash
agent-notes-omnibus
# or equivalently:
agent-notes serve --kinds breadcrumbs,memory,search
```

## Entry points

| Console script | Kind(s) | Notes |
|---|---|---|
| `agent-notes` | Generic | `serve --kinds X,Y,...` |
| `agent-notes-breadcrumbs` | breadcrumbs | Thin shim |
| `agent-notes-memory` | memory | Thin shim |
| `agent-notes-search` | search | Thin shim |
| `agent-notes-omnibus` | breadcrumbs + memory + search | Single process |
| `agent-notes-setup` | — | Alias for `migrate --all` |
| `agent-notes-migrate` | — | Schema migrations from `schema/*.sql` |
| `agent-notes-doctor` | — | Health check: DSN, schema, model, links audit |

See `AGENTS.md` for build/test/lint commands and contributor conventions.
