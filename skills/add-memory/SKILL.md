---
name: add-memory
description: Record a cross-session fact — something the next agent (or future-you) needs to know that isn't tied to a specific defect. Invoke when the user says "remember that…", "save this as a memory", or when you discover a non-obvious project convention worth preserving.
---

# /add-memory — Record Something the Next Session Needs

Memories are persistent, project-scoped (or workspace-scoped) notes
that record facts, conventions, decisions, and reframings. They are
*not* breadcrumbs — a breadcrumb is "a problem to track until fixed";
a memory is "a fact that stays true regardless of any specific fix."

Examples of good memories:

- "This repo's tests need testcontainers Postgres; in-process mocks
  cause Phase-3.6 regressions."
- "User works in a regulated environment; AI agent tools blocked
  upstream due to audit/provenance gaps."
- "Project pivot: agent-notes-mcp moves from MCP to CLI+skills in
  Plan 004; MCP servers stay until skill-only sessions are proven."

Examples of bad memories (file these elsewhere):

- "Fix the broken test in cli.py:42" — that's a breadcrumb.
- "Today I refactored the memory model" — that's a reflection or a
  commit message.
- "The capital of France is Paris" — that's not project-specific.

## Memory types

Four conventional types, mirrored from the user's auto-memory
conventions in `CLAUDE.md`:

- `user` — facts about the user (preferences, working style, constraints).
- `feedback` — explicit feedback the user gave that should shape future
  responses ("be more direct on strategic questions", etc.).
- `project` — facts about a specific project (architecture, pivots,
  decisions, conventions).
- `reference` — durable cross-cutting reference material (how a tool
  works, an external convention worth recording).

`reflection` is also a type but is owned by the `/reflect` skill — do
not file reflections through this skill.

If the project has additional conventional types, check
`agent-notes vocabulary list --workspace <ws> --kind memory_type --json`.

## Naming convention

Names are slugs, kebab-case, hyphen-separated. Lead with the type or
domain so list views are scannable:

- `project-agent-notes-refactor`
- `user-regulated-workplace`
- `convention-maybe-projects`
- `feedback-honest-strategic-reads`
- `reference-wake-and-hooks-across-harnesses`

Stable names matter — they're how the memory is referenced from other
memories' bodies (wikilinks: `[[name]]`) and how
`agent-notes memory get <name>` retrieves it. Renaming is a soft-delete
of the old + add of the new; avoid unless the original name is wrong.

## Dedup against existing memories

Before adding, search:

```
agent-notes memory search "<query>" --path <repo-path> --json
agent-notes memory list --path <repo-path> --type <type> --json
```

If a close match exists, the right move is usually to update its body
in place (re-`add` with the same name supersedes the prior version)
rather than create a near-duplicate. Memories are versioned via
`supersedes`; the CLI handles the chain.

## Body shape

Lead with one sentence that captures the fact. Then add context: where
you learned it, what changes if it stops being true, links to related
memories or breadcrumbs. Two to six short paragraphs is typical.
Embedding-based search works better with a few well-chosen keywords
than with prose padding.

## How to run it

```
agent-notes memory add \
  --path <repo-path> \
  --name <kebab-case-name> \
  --type <user|feedback|project|reference> \
  --body "<one sentence + context>" \
  --json
```

Parse the JSON; confirm the `id` and any `supersedes` field (the
latter tells you you replaced an older version).

For project-specific conventions worth referencing across sessions,
see Plan 002 in this repo's `plans/` directory — it documents the
naming and dedup rules in more detail.

---

If `agent-notes` exits non-zero or its JSON shape doesn't match what
this skill expects, the CLI contract has drifted — file a breadcrumb
under project agent-notes-mcp.
