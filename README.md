# agent-notes-mcp

Unified pgvector-backed MCP server for agent notes — breadcrumbs (issue tracker), memories (cross-session facts), and reflections (session retrospectives) — shared across Claude Code, OpenCode, and Gemini CLI for the sf2 agent constellation.

Consolidates and supersedes the standalone `breadcrumb-mcp` and `memory-mcp` projects. See `plans/001-architecture-and-implementation.md` for the architecture, design decisions, peer-review history, and phased implementation roadmap.

## Status

Phase 0 (legacy prep) complete. Phase 1a (repo skeleton + core protocol layer) in progress.

## Quickstart

```bash
cd /projects/agent-notes-mcp
uv venv && uv pip install -e ".[test]"

# Configure
export AGENT_NOTES_DSN="postgresql://user:pass@host:5432/agent_notes"

# Create schema
agent-notes-setup

# Run a server (per-kind shim — or use `agent-notes serve --kinds ...` for omnibus)
agent-notes-breadcrumbs
```

See `AGENTS.md` for build/test/lint commands and contributor conventions.
