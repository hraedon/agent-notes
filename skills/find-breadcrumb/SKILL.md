---
name: find-breadcrumb
description: Search existing breadcrumbs before filing a new one (dedup), or look one up by topic. Invoke before /file-breadcrumb, when the user asks "is there a breadcrumb for X", or when you need to check whether a problem is already tracked.
---

# /find-breadcrumb — Search Before You File

The single biggest failure mode of a breadcrumb store is duplicates.
Two near-identical breadcrumbs split context, halve the chance either
gets resolved, and erode trust in the backlog. This skill exists to
make "search before file" cheap enough that you always do it.

## When to use

- **Always before `/file-breadcrumb`.** No exceptions. Even when you're
  sure it's new, search anyway — phrasing varies, and a 5-second check
  beats a 5-minute reconciliation later.
- When the user asks "have I noted anything about X?"
- At session start (the `/start` skill calls this implicitly).
- Before opening a related work item or substrate spec — knowing the
  existing context shapes the proposal.

## Two-pronged search

The CLI gives you two complementary tools; use both.

1. **Structured filter** — fast, exact on type/status:

   ```
   agent-notes breadcrumb find \
     --path <repo-path> \
     --status open \
     --type <type-if-known> \
     --text "<short query>" \
     --json
   ```

   `--text` does embedding similarity scoped to breadcrumbs only. Good
   for "find all open bugs near this topic."

2. **Cross-kind semantic search** — embedding similarity across
   breadcrumbs, memories, and reflections:

   ```
   agent-notes search all "<query>" --path <repo-path> --json
   ```

   This catches cases where the problem was recorded as a memory or
   reflection, not a breadcrumb. Useful when you don't know which form
   prior context took.

## Scope: project, workspace, global

Pass `--scope` to broaden the search:

- `--scope project` (default) — current project only, resolved from
  `--path` or `--workspace`/`--project`.
- `--scope workspace` — every project in the current workspace.
- `--scope global` — everything the user has access to; no `--path` or
  `--workspace` required.

```
agent-notes breadcrumb find --path <repo-path> --scope workspace --text "<q>" --json
agent-notes breadcrumb find --scope global --text "<q>" --json
```

Do both project-scoped and workspace- (or global-) scoped searches when
the issue might plausibly affect more than one project. The marginal
cost is ~one CLI call.

## Interpreting results

The JSON returns a `breadcrumbs` array with `identifier`, `title`,
`kind`, `status`, and a `score` when `--text` was used. Show the top
3–5 to the user in a compact list:

```
- BC-CLI-007 (bug / open) — CLI swaps workspace/project args in get_memory
- BC-CLI-002 (todo / new) — install-skills lacks frontmatter validation
```

If any look like the issue at hand, link to the `update-breadcrumb`
skill instead of filing new.

---

If `agent-notes` exits non-zero or its JSON shape doesn't match what
this skill expects, the CLI contract has drifted — file a breadcrumb
under project agent-notes-mcp.
