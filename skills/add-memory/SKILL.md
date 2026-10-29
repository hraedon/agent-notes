---
name: add-memory
description: Record a cross-session fact — something the next agent (or future-you) needs to know that isn't tied to a specific defect. Invoke when the user says "remember that…", "save this as a memory", or when you discover a non-obvious project convention worth preserving.
---

# /add-memory — Record Something the Next Session Needs

Memories are persistent notes at one of three scopes (below) that record
facts, conventions, decisions, and reframings. They are
*not* breadcrumbs — a breadcrumb is "a problem to track until fixed";
a memory is "a fact that stays true regardless of any specific fix."

Examples of good memories:

- "This repo's tests need testcontainers Postgres; in-process mocks
  cause Phase-3.6 regressions."
- "User works in a regulated environment; AI agent tools blocked
  upstream due to audit/provenance gaps."
- "Project pivot: agent-notes moves from MCP to CLI+skills in
  Plan 004; MCP servers stay until skill-only sessions are proven."

Examples of bad memories (file these elsewhere):

- "Fix the broken test in cli.py:42" — that's a breadcrumb.
- "Today I refactored the memory model" — that's a reflection or a
  commit message.
- "The capital of France is Paris" — that's not project-specific.

Use the store the user intends. These commands write shared agent-notes
project memory; Codex native memory is a separate destination with its own
write rules. Do not copy private harness memory into the shared store without
a request that covers sharing it.

## Scope: where the memory lives

Pick the narrowest scope that is true everywhere it will be read:

| Scope | Use it for | Example |
|---|---|---|
| `repo` (default) | Code and design facts about this project: architecture, conventions, decisions, gotchas | "Tests need testcontainers Postgres" |
| `vm` | Facts about this machine: paths, services, GPU, mounts, installed toolchains, ports | "Docker Desktop runs here; the 3090 is CUDA device 1" |
| `global` | Cross-host conventions and user preferences that hold in every repo on every machine | "User prefers direct strategic reads" |

If the fact would be wrong on another machine, it is `vm`. If it would be
wrong in another repo, it is `repo`. Only what survives both is `global`.

- `--scope vm|global` works from any directory, including outside a
  registered repo. `--scope vm` uses this machine's ID (`AGENT_NOTES_VM`, else
  the short hostname); `--vm <id>` files under another machine's ID. A host
  with a generated name (container, pod) must set `AGENT_NOTES_VM`.
- Each scope has its own namespace. The same name may exist at several
  scopes, and `memory search` (default `--scope composed`) returns the most
  specific one (repo > vm > global) and lists the hidden ones in `shadows`.
  Every result carries `scope` and `scope_key`.
- Use `--scope repo` (or omit it) for `get`, `update`, and `delete` of a repo
  memory; pass the same `--scope` you used to add a vm or global one.

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

You are the judge of whether a new fact duplicates an old one; the CLI only
shows you the evidence. Before adding, search:

```
agent-notes memory search "<query>" --path <repo-path> --json
agent-notes memory list --path <repo-path> --type <type> --json
```

`search` covers repo, vm, and global together. `list` shows one scope at a
time (default `repo`); add `--scope vm`, `--scope global`, or `--scope composed`.

`memory add` also checks for you. It compares the new body with the facts in
the target scope and its parent scopes (repo > vm > global) and returns the
nearest five as `dedup.candidates` (`id`, name, scope, type, `score`,
`match`, `blocking`). If any candidate scores at or above `dedup.threshold` (0.85), or
has the same name in a parent scope (which the new memory would shadow), the
add is refused with exit code 4 and error code `POSSIBLE_DUPLICATE`. The
candidates are then in `error.detail`, a JSON string, and `blocking` lists
each unanswered one with its scope. Read them, then re-run with a choice for
every blocking candidate (`--distinct` answers all of them at once):

| Flag | Meaning | When |
|---|---|---|
| `--supersedes NAME` | Replace NAME in the scope you write to; it is deactivated and recorded in `supersedes` | The new body is the corrected or fuller version of NAME |
| `--related NAME` (repeatable) | Keep both and link them with `relates_to` | Same topic, different facts (for example a feedback memory about a spec) |
| `--distinct` | Keep both, unlinked | The similarity is superficial |

Each flag answers one memory. `--related NAME` links the most specific scope
that holds NAME; when the same name is a candidate in two scopes, answer the
other with a scope prefix, for example `--related global:NAME`.
`--supersedes` takes the same forms but only within the scope you write to.
Both flags try the value as a literal name first: if a memory is actually
called `global:NAME`, that memory wins, and the prefix applies only when no
such name exists.

To revise a memory, re-`add` it under the **same** name in the same scope: that
supersedes the prior version, is never blocked (`dedup.revision` is `true`),
and cannot be combined with `--supersedes`. Reflections are dated episodes and
skip this check.

## Contradictions

When two memories disagree and you can't yet say which is right, record it:

```
agent-notes memory link <newer-name> <older-name> --kind contradicts --path <repo-path> --json
```

Add `--to-scope vm|global` when the other memory lives in another scope.
`memory get`, `memory search` and `memory recall` then put a `contradictions`
list on both memories and a one-line warning in `warnings`. The contradiction
closes when either memory is superseded under another name or deleted, or
when you run `agent-notes memory unlink <a> <b> --kind contradicts`.

## Body shape

Lead with one sentence that captures the fact. Then add context: where
you learned it, what changes if it stops being true, links to related
memories or breadcrumbs. Two to six short paragraphs is typical.
Embedding-based search works better with a few well-chosen keywords
than with prose padding.

Keep `project`, `user` and `feedback` bodies under 2,000 characters. Above
that, `memory add` and `memory update` still write the memory but add a
`warnings` entry: move long material into a `reference` memory, or split it
into several facts.

## How to run it

```
agent-notes memory add --path <repo-path> --name <kebab-case-name> --type <user|feedback|project|reference> --body "<one sentence + context>" --json
agent-notes memory add --scope vm --name <kebab-case-name> --type reference --body "<host fact>" --json
agent-notes memory add --scope global --name <kebab-case-name> --type user --body "<convention or preference>" --json
```

Parse the JSON; confirm the `id` and any `supersedes` field (the
latter tells you you replaced an older version). Check `warnings`, and glance
at `dedup.candidates` even when the add succeeded: a candidate just under the
threshold may still deserve `memory link ... --kind relates_to`.

If the add exits 4 with `POSSIBLE_DUPLICATE`, decide as above and re-run with
the flag, for example:

```
agent-notes memory add --path <repo-path> --name <name> --type project --body "<body>" --related <existing-name> --json
```

`agent-notes memory hygiene --path <repo-path>` (read-only) reports the
store's existing near-duplicates, oversized facts, shadowed names, open
contradictions, stale facts and possible secrets.

For project-specific conventions worth referencing across sessions,
see Plan 002 in this repo's `plans/` directory — it documents the
naming and dedup rules in more detail.

---

If `agent-notes` exits non-zero or its JSON shape doesn't match what
this skill expects, the CLI contract has drifted — file a breadcrumb
under project agent-notes.
