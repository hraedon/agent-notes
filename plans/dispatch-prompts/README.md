# Dispatch prompts

Self-contained implementation briefs for the remaining `agent-notes-mcp` phases. Each file is a complete prompt — copy-paste into a fresh agent session (GLM, Kimi, Sonnet, etc.). No outside context required beyond the project state at HEAD of `main` and the files referenced.

| Phase | Prompt | Status |
|---|---|---|
| 2a | `phase-2a-breadcrumbs-db.md` | Ready |
| 2b | `phase-2b-breadcrumbs-projection.md` | Ready (the `/end` skill change is a manual step at the end) |
| 3  | `phase-3-memory-and-reflections-spike.md` | Ready |
| 4  | `phase-4-cross-kind-search.md` | Ready |
| 5  | `phase-5-reflections-conditional.md` | Conditional stub — write after Phase 3.6 spike outcome |
| 6  | `phase-6-polish.md` | Ready |

## Common conventions (every prompt assumes)

- Working directory: `/projects/agent-notes-mcp`. Use a worktree (`isolation: worktree` if you're a subagent) — main is protected.
- Read `plans/001-architecture-and-implementation.md` first for the architectural decisions cited by number throughout.
- Read `AGENTS.md` for build/test/lint commands and style.
- Tests: integration against ephemeral Postgres via `testcontainers[postgres]` (already in `[test]` extras); reuse the `ephemeral_db` fixture from `tests/conftest.py`. **No DB mocks for trigger/CTE/LISTEN logic.**
- Style: match existing code in `src/agent_notes/core/` — dataclass models, type hints, sync everywhere, no decorative comments. Cite plan decisions in code only when the WHY would surprise a reader.
- Every commit message: imperative, scoped, plan-decision-aware.
- **Hard requirement** (lesson from Phase 1b): every state-mutating MCP tool must have a test that drives it end-to-end via stdin/stdout against a real `Server` instance AND asserts the corresponding `change_log` row exists. Audit-log compliance (decision 20) is not optional and easy to miss if tests only exercise the helper functions.
