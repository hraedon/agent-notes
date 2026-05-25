---
name: start
description: Orient at the beginning of a working session — surface open breadcrumbs, active memories, and the last reflection so the agent picks up with context. Invoke at the start of a session, when the user says "/start", "where were we", or after a long gap.
---

# /start — Session-Start Orientation

Sessions are short and agents change. Without orientation, work
starts by re-reading the README and rediscovering open issues that
were already known. This skill collapses that lookup into one place.

Run this when:

- A new session begins on a project with breadcrumbs / memories.
- The user asks "where did we leave off?" or "what's outstanding?"
- You're picking up work from a different model or session.

If you need to know which workspaces exist (e.g. to broaden a search
beyond the current path), use:

```
agent-notes workspace list --json
```

It returns `id`, `slug`, `name`, and `project_count` for every
workspace. Don't go fishing through `vocabulary list` for this.

## Three quick lookups

Run all three (parallel-safe), then synthesize a compact briefing.

### 1. Recent open breadcrumbs

```
agent-notes breadcrumb find \
  --path <repo-path> \
  --status open \
  --limit 10 \
  --json
```

Also pull `--status new` and `--status in-progress` if the project
uses those. Combine and sort by severity then recency. Show the top
5–8 in a list:

```
- BC-CLI-007 (bug / high) — Memory CLI swaps workspace/project args
- BC-CLI-002 (todo / medium) — install-skills lacks frontmatter validation
```

### 2. Active memories

```
agent-notes memory list --path <repo-path> --json
```

You don't need to dump every memory — that's noisy. Highlight:

- The `project-*` memory for this repo (it's the "what is this project
  doing right now" anchor).
- Any `feedback-*` memories (they shape how you respond).
- Any memory whose body references the area the user is about to work
  on. If you don't know the area yet, list names only.

### 3. The last reflection

```
agent-notes memory list \
  --path <repo-path> \
  --type reflection \
  --limit 3 \
  --json
```

Take the most recent reflection's name, then:

```
agent-notes memory get <name> --path <repo-path> --json
```

Surface the "What remains" and "Gaps to flag" sections in particular —
those are the explicit handoff points from the prior session.

## Briefing shape

Aim for ~10 lines, not a full dossier. Sample:

```
## Session start — agent-notes-mcp

**Open breadcrumbs (3):**
- BC-CLI-007 (bug / high) — workspace/project arg swap in get_memory
- BC-CLI-002 (todo / medium) — install-skills frontmatter validation
- BC-CLI-003 (rfc / low)    — expose LISTEN helpers via CLI

**Where we left off (reflection 2026-05-24-kimi-k2-6):**
- Phase 9a landed; 204 tests pass.
- Next: Phase 9b skills (file-breadcrumb, add-memory, start).
- Gaps: --kind vs --type inconsistency, link trace --all CLI test.

**Active anchor memory:**
- project-agent-notes-refactor — MCP→CLI+skills pivot via Plan 004.
```

Don't editorialize past the facts — the user/next-agent decides what
to do; you're just laying out the state.

---

If `agent-notes` exits non-zero or its JSON shape doesn't match what
this skill expects, the CLI contract has drifted — file a breadcrumb
under project agent-notes-mcp.
