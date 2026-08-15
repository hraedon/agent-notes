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
repo isn't git or isn't registered, reconcile prints nothing — move on.

## Zero — declare the session identity

Before anything else, declare which model this session is. Work-item
attribution and the cross-lineage review gate key off the session's declared
`model_lineage` (WI-067); an undeclared session reads as UNKNOWN and fails
closed at every write. You know which model you are — write it down once:

```
agent-notes session declare --model-lineage <family>
```

`<family>` is the canonical family (e.g. `claude-opus`, `glm`, `qwen`,
`deepseek`, `kimi`, `longcat` — `agent-notes session status` and regista's
canonical registry list them). This writes a private, per-session record keyed
by the harness session id; it does not touch host-wide config.

Once declared, the record is the **stable source** for this session: declaring
a *different* family later is refused (a session cannot relabel itself
mid-session to manufacture cross-lineage independence). Re-declaring the same
value is idempotent. Verify with:

```
agent-notes session status
```

Skip this only if you already declared it earlier this session (the record
persists across tool calls on the same session id).

**If `session declare` fails with `NO_SESSION_ID`** — your harness does not
export a session id to tool subprocesses. This happens under **opencode**:
the opencode plugin threads the session id only into the processes *it*
spawns (orientation, outbox), not into tool calls the agent itself runs.
Declare explicitly by naming the session id (you can see it in the plugin's
session log line, e.g. `[agent-notes] session <id> → dir …`):

```
agent-notes session declare --session-id <session-id> --model-lineage <family>
agent-notes session status --session-id <session-id>
```

The explicit `--session-id` is per-invocation (no process-global state) and
keeps the same stable-source rule: once declared, that session's lineage
cannot change mid-session.

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

Also pull `--status claimed` if the project uses that. Combine and
sort by severity then recency. Show the top 5–8 in a list:

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
- WI-003 (rfc / low)    — expose LISTEN helpers via CLI

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
this skill expects, the CLI contract has drifted — file a work item
under project agent-notes.
