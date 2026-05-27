---
name: reflect
description: Write a session reflection and record it as a reflection-type memory so the next session can find it. Invoke when the user asks for a reflection, retrospective, or end-of-session writeup; also invoked by /end as step 3.
---

# /reflect — Write a Session Reflection

Write an honest, subjective reflection on the current session and save it under the working project's reflections directory.

The point of a reflection is to leave a useful signal for the next agent (or the user reading later). Be direct. Avoid PR-style summaries — those belong in commit messages. Say what you actually think.

## Steps

### 1. Locate the reflections directory

In order of preference, write to the first that exists:

1. `.substrate/reflections/` (substrate convention)
2. `.claude/reflections/`
3. `reflections/` at repo root
4. `docs/reflections/`

If none exist, create `reflections/` at the repo root (use the git toplevel, not the cwd) — unless the user specifies a different location. Mention in your final reply where you wrote it.

### 2. Gather session context

Ground the reflection in specifics, not vibes. Before writing, skim:

- `git status` and `git diff --stat` to see what actually changed this session
- `git log` for recent commits if work was committed
- Any project worklog (e.g. `.substrate/worklog.md`, `WORKLOG.md`, `CHANGELOG.md`)
- Any breadcrumbs / TODO / issues directories that show known gaps

If you can't ground a section in something concrete, say so in the section rather than padding it.

### 3. Write the reflection

Use the template below. All four content sections are required. Length is up to you — write as much as is honest, not as much as looks thorough. A short, sharp reflection beats a long bland one.

```markdown
---
model: <model-id, e.g. claude-opus-4-7>
datetime: <YYYY-MM-DDTHH:MM, local or UTC — note which>
project: <repo or project name>
---

# Session Reflection — <YYYY-MM-DD>

**Work summary:** <2–3 sentences — what was actually done this session, grounded in commits/diff>

---

## On the project

Subjective take on the project itself. What it's trying to do, how well the
current shape of the code/spec/docs serves that, what's elegant, what feels
wrong or fragile. Don't recap the README — say what you think after working
in it.

## On the work done

Subjective take on this session's work specifically. What went well, what
was awkward, what you're confident in vs. what you'd want a second pair of
eyes on. Be honest about anything you finished but aren't sure is correct.

## On what remains

What's left to do — both the obvious next steps and the ones that won't be
obvious from reading the code. Sequence them if order matters. Distinguish
"needed before this can ship" from "nice to have."

## Gaps to flag

Things you'd actively flag for the user or next agent: missing tests, silent
failure modes, spec drift, dependencies that look load-bearing but
under-exercised, conventions that exist in some files but not others,
anything you noticed but didn't fix. One bullet each, with location
(`path:line` if useful). This section is the most valuable — don't soften it.
```

### 4. Save the file

Filename: `<reflections-dir>/YYYY-MM-DD-<model-slug>.md`

Where `<model-slug>` is a short identifier derived from the model id: e.g. `claude-opus-4-7` → `opus-4-7`, `claude-sonnet-4-6` → `sonnet-4-6`, `glm-5.1` → `glm-5-1`.

If a file with that name already exists (multiple sessions same day), append `-2`, `-3`, etc.

### 4b. Ingest the reflection into the agent-notes memory store

After saving the markdown file to disk, run the agent-notes CLI to record the reflection as a memory of type `reflection`:

```
agent-notes memory add \
  --path <repo-path> \
  --name "reflection-<YYYY-MM-DD>-<model-slug>" \
  --type reflection \
  --body "$(cat <reflection-file>)" \
  --json
```

Parse the JSON. If the CLI exits non-zero (e.g. project not configured, DB unavailable), log a brief warning and continue — the file on disk is still valid. The DB ingest is best-effort, not blocking.

Note: if you previously used the MCP `add_memory` tool, that path is being retired (Plan 004). Use the CLI as shown above.

### 5. Report

Reply with one line: the path of the file you wrote, followed by confirmation of DB ingest (e.g. "reflection-2026-05-25: ingested into project agent-notes-mcp" or "reflection-2026-05-25: written to disk only (agent-notes CLI not available)").

---

If `agent-notes` exits non-zero or its JSON shape doesn't match what
this skill expects, the CLI contract has drifted — file a breadcrumb
under project agent-notes-mcp.
