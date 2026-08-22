---
name: update-breadcrumb
description: Update an existing breadcrumb — transition its status, append context to the body, or mark it resolved. Invoke when the user says "close BC-X", "this resolves BC-X", "update breadcrumb X", or when you yourself addressed a breadcrumb during this session.
---

# /update-breadcrumb — Move a Work Item Forward

Work items are not write-once. As work happens, their state changes:
status moves through a small lifecycle, body accumulates findings,
severity gets re-rated when evidence comes in. Keep them current; a
stale item is worse than no item because it makes the backlog dishonest.

## Status transitions

The legal status values are project-defined — check
`agent-notes vocabulary list --workspace <ws> --kind wi_status
--json` if unsure. The common shape is:

- `open` — just filed, not triaged.
- `claimed` — actively being addressed.
- `closed` — done. Body should explain how.
- `deferred` — deliberately not addressing; body must say why.

Move forward, not sideways. Don't flip `closed` back to `open` to
record a regression — file a new work item that references the old one
via `agent-notes link add` and explain the relationship.

## When to append body vs. file a new work item

Append to the existing body when:

- New evidence narrows or refines the same issue.
- You partially addressed it and want to record what's left.
- A reviewer left a question or pushback worth preserving.

File a *new* work item (and link them) when:

- The investigation surfaced a distinct second problem.
- The original was filed against the wrong project or workspace.
- The scope drifted far enough that one title can't honestly cover both.

## Resolving

When you actually fixed it in the diff you're about to commit:

1. Append a short resolution note to the body — what you changed, where,
   and what to verify. One paragraph is enough.
2. Transition status to `closed` (or the project's "done" value).
3. If the project moves resolved entries to a sub-folder, that's
   handled by the on-disk projection layer, not by this skill.

## How to run it

```
agent-notes work-item update <identifier> \
  --path <repo-path> \
  --status <new-status> \
  --body "<existing body + appended note>" \
  --json
```

Identity is ambient v6 configuration, never a command-line override. Set the
canonical actor and regista producer variables in the environment or
`~/.config/agent-suite/suite.env` before running the command.

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
