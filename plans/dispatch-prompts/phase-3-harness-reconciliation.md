# Phase 3.7 — Harness reconciliation decision (§11)

**Date:** 2026-05-20
**Author:** GLM-5.1 (Phase 3 implementation)
**Decision:** Option 1 — document the boundary. Option 2 (bridge tool) deferred.

## Context

Plan §11 identifies an open question about the relationship between harness file-memory (per-instance, stored in `MEMORY.md` by Claude Code / OpenCode / Gemini CLI) and the memory MCP server (cross-agent, stored in Postgres).

Three options were on the table:

1. **Document the boundary.** Harness = this-instance, memory server = cross-agent. De-facto today.
2. **Bridge tool.** `import_harness_memory` pulls `MEMORY.md` periodically. One-way sync.
3. **Memory server as harness backend.** Configure harness to write through MCP. Requires harness changes.

## Decision: Option 1 — document the boundary

### Rationale

- The memory server is now operational and provides cross-agent persistence. Any agent connected to it can store and retrieve memories that survive across sessions and are shared with other agents.
- Harness file-memory (`MEMORY.md`) serves a different purpose: per-instance context that the harness injects into the system prompt on the next invocation of the same agent configuration. It's not shared across agents, not queryable, and not versioned.
- These are complementary, not competing. A bridge tool (option 2) adds complexity for marginal gain — agents that need cross-agent memory should use the memory server directly. The harness file stays for its original purpose (per-instance context injection).
- Option 3 requires upstream changes to Claude Code / OpenCode / Gemini CLI that are outside our control.

### Boundary definition

| Aspect | Harness file-memory | Memory MCP server |
|---|---|---|
| Scope | Single agent instance | All agents across workspace |
| Storage | `MEMORY.md` on disk | Postgres `agent_notes.memories` |
| Persistence | Until agent clears/overwrites | Indefinite (until soft-deleted) |
| Query | None (plain text injection) | Semantic search, type filter, trace graph |
| Sharing | Not shared | Shared across all connected agents |
| Editable by | The harness itself | Any agent via MCP tools |

### What goes where

- **Use harness `MEMORY.md`** for: agent-specific behavioral preferences, per-session context the harness injects, scratch notes that don't need to outlive the instance.
- **Use the memory MCP server** for: project knowledge, design decisions, cross-session learnings, feedback that should inform future agents, reflections, structured notes with wikilinks.

### Action items

1. Add a brief paragraph to `AGENTS.md` documenting this boundary (done when Phase 3 merges).
2. Defer option 2 (bridge tool) — if an agent wants harness content in the memory server, it can call `add_memory` directly during its session. No automated sync needed yet.
3. Defer option 3 until the harness projects express interest.

### Future trigger

Revisit if: (a) agents regularly duplicate content between harness and memory server, or (b) a harness project requests MCP as a storage backend.
