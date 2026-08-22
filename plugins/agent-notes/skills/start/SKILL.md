---
name: start
description: Orient at the start of a working session so the agent picks up with project context instead of re-deriving it from scratch. Invoke at the start of a session, when the user says "/start", "where were we", or after a long gap.
---

# /start — Session-Start Orientation

Sessions are short and agents change. Without orientation, work
starts by re-reading the README and rediscovering open issues that
were already known. This skill collapses that lookup into one place.

Run this when:

- A new session begins on a project with work items / memories.
- The user asks "where did we leave off?" or "what's outstanding?"
- You're picking up work from a different model or session.

If you need to know which workspaces exist (e.g. to broaden a search
beyond the current path), use:

```
agent-notes workspace list --json
```

It returns `id`, `slug`, `name`, and `project_count` for every
workspace. Don't go fishing through `vocabulary list` for this.

A note on `<repo-path>`: `--path` is matched as a **string** against the
registered `projects.repo_root` (exact, then ancestor) — the filesystem
is never stat'd. Most registered roots are spelled `/projects/<name>` (a few, e.g. `switchboard`, are not), so prefer
that form even if your checkout lives elsewhere. If a path won't resolve
(`PROJECT_NOT_REGISTERED`), fall back to `--workspace default
--project <slug>` on any command (limitation tracked as agent-notes WI-065).

## First — reconcile against git

Before listing what's open, let the tool self-heal the most common drift:
work items resolved in a commit ("resolve WI-094") that nobody transitioned in
the DB, so they sit open for weeks. Run:

```
agent-notes breadcrumb reconcile --path <repo-path>
```

It scans recent git history for open work items referenced with resolution
intent. The match list is conservative (negation-guarded) but not infallible —
glance at it. If the matches are genuine resolutions, apply them so the open
list below reflects reality:

```
agent-notes breadcrumb reconcile --path <repo-path> --apply
```

This is the session-start counterpart to the same step in `/end`; running it
here catches drift left by a prior session that ended without it. Note what you
reconciled in the briefing (e.g. "reconciled 2 already resolved in git"). If the
path isn't registered, reconcile errors (`PROJECT_NOT_REGISTERED`, exit 3) —
use the `--workspace`/`--project` fallback above or move on; with no usable
git history it just reports no matches.

## Then — three quick lookups

Run all three (parallel-safe), then synthesize a compact briefing.

### 1. Recent open work items

```
agent-notes work-item find \
  --path <repo-path> \
  --status open \
  --limit 10 \
  --json
```

Also pull `--status claimed` if the project uses that. The filter flag
is `--type` (there is no `--kind` flag), even though the JSON field it
matches is named `kind`. Valid kinds: `todo, observation, decision,
risk, task, bug, feature, improvement, question, experiment, spike,
refactor, docs, ci, job` — `rfc` is not one, here or in `work-item
file`. Combine and sort by severity then recency. Show the top 5–8 in a list:

```
- WI-007 (bug / high) — Memory CLI swaps workspace/project args
- WI-002 (todo / medium) — install-skills lacks frontmatter validation
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
## Session start — agent-notes

**Open work items (3):**
- WI-007 (bug / high) — workspace/project arg swap in get_memory
- WI-002 (todo / medium) — install-skills frontmatter validation
- WI-003 (feature / low) — expose LISTEN helpers via CLI

**Where we left off (reflection 2026-05-24-kimi-k2-6):**
- Phase 9a landed; 204 tests pass.
- Next: Phase 9b skills (file-breadcrumb, add-memory, start).
- Gaps: link trace --all CLI test still missing.

**Active anchor memory:**
- project-agent-notes-refactor — MCP→CLI+skills pivot via Plan 004.
```

Don't editorialize past the facts — the user/next-agent decides what
to do; you're just laying out the state.

---

If `agent-notes` exits non-zero or its JSON shape doesn't match what
this skill expects, the CLI contract has drifted — file a work item
under project agent-notes (`work-item file --workspace default
--project agent-notes --type bug ...`).
