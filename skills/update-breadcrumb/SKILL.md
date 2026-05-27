---
name: update-breadcrumb
description: Update an existing breadcrumb — transition its status, append context to the body, or mark it resolved. Invoke when the user says "close BC-X", "this resolves BC-X", "update breadcrumb X", or when you yourself addressed a breadcrumb during this session.
---

# /update-breadcrumb — Move a Breadcrumb Forward

Breadcrumbs are not write-once. As work happens, their state changes:
status moves through a small lifecycle, body accumulates findings,
severity gets re-rated when evidence comes in. Keep them current; a
stale breadcrumb is worse than no breadcrumb because it makes the
backlog dishonest.

## Status transitions

The legal status values are project-defined — check
`agent-notes vocabulary list --workspace <ws> --kind breadcrumb_status
--json` if unsure. The common shape is:

- `new` / `proposed` — just filed, not triaged.
- `open` / `accepted` — confirmed real, not yet worked.
- `in-progress` — actively being addressed.
- `resolved` / `closed` — done. Body should explain how.
- `wontfix` — deliberately not addressing; body must say why.

Move forward, not sideways. Don't flip `resolved` back to `open` to
record a regression — file a new breadcrumb that references the old one
via `agent-notes link add` and explain the relationship.

## When to append body vs. file a new breadcrumb

Append to the existing body when:

- New evidence narrows or refines the same issue.
- You partially addressed it and want to record what's left.
- A reviewer left a question or pushback worth preserving.

File a *new* breadcrumb (and link them) when:

- The investigation surfaced a distinct second problem.
- The original was filed against the wrong project or workspace.
- The scope drifted far enough that one title can't honestly cover both.

## Resolving

When you actually fixed it in the diff you're about to commit:

1. Append a short resolution note to the body — what you changed, where,
   and what to verify. One paragraph is enough.
2. Transition status to the project's "done" value (`resolved`,
   `implemented`, `closed` — match what existing resolved entries use).
3. If the project moves resolved entries to a sub-folder, that's
   handled by the on-disk projection layer, not by this skill.

## How to run it

```
agent-notes breadcrumb update <identifier> \
  --path <repo-path> \
  --status <new-status> \
  --body "<existing body + appended note>" \
  --json
```

Two things to know about the CLI:

- `--body` **replaces** the body. To add new context without losing the
  old, use `--append-body "<note>"` — the CLI inserts a blank-line
  separator and writes atomically. `--body` and `--append-body` are
  mutually exclusive.
- You can pass `--status` alone to transition without touching body.

Parse the JSON response. Confirm the new status to the user.

---

If `agent-notes` exits non-zero or its JSON shape doesn't match what
this skill expects, the CLI contract has drifted — file a breadcrumb
under project agent-notes.
