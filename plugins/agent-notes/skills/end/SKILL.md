---
name: end
description: Wrap up a working session and leave a clean handoff before context resets. Invoke when the user says "/end", "wrap up", "we're done", "close this out", or when you yourself recognize the session is ending and want loose ends reconciled.
---

# /end — Session Wrap-Up

A session ends well when the next agent (or future-you) can pick up without re-deriving what just happened. That means three things end up on disk before you stop:

- work items that match the actual state of the world (closed if fixed, opened if newly noticed),
- a reflection that captures the subjective read (what was hard, what you'd want a second pair of eyes on),
- a commit that bundles all of the above.

Steps 1–4 below are the discipline. The pressure to skip them comes from the same place every time: the user said "wrap this up so I can go," reflection feels like ceremony, and the diff is already green. Do them anyway. The reflection is what makes this skill worth running; if you cut it, you've reduced /end to `git commit`.

---

## Step 1 — Reconcile work items

### 1a. Locate the work items convention

Look for a work items directory at the repo root, in this order: `work-items/`, `.work-items/`, `docs/work-items/`. Read its `README.md` if present — honor the schema (frontmatter fields, statuses, resolved-folder convention). Do not impose your own.

If no work items directory exists *and* this repo isn't wired to the agent-notes CLI, skip step 1 entirely — do not create one unprompted. Note it in the final report.

### 1b. Auto-reconcile against git first

Before closing anything by hand, let the tool catch what commits already say. The
single most common drift is *silent resolution* — the work landed in a commit
("resolve WI-094") but nobody told the DB, so the work item sits open for weeks.
Run:

```
agent-notes breadcrumb reconcile --path <repo-path>          # dry-run: shows matches
agent-notes breadcrumb reconcile --path <repo-path> --apply  # transition them to resolved
```

It scans recent git history for open work items whose identifier appears in a
commit with resolution intent, and (with `--apply`) resolves them and stamps the
resolving commit into `external_refs`. Review the dry-run list first — it's
conservative but not infallible. This handles the work items you resolved via
commit; the next step covers anything reconcile can't see.

### 1c. Close work items worked on this session

For each work item you actually addressed but that reconcile did *not* catch (no
commit, or a commit message that didn't name it), use the `update-breadcrumb`
skill (or invoke `agent-notes work-item update` directly). Transition status to
`closed` (or the project's "done" value). If you partially addressed one, leave
it open and append a note describing what's outstanding.

### 1d. Open work items for issues noticed

For each defect, design question, or gap noticed during the session that wasn't fixed, use the `file-breadcrumb` skill — which itself requires a dedup search first. Be honest about severity. The "I noticed it but it's not worth filing" instinct is wrong by default; if you noticed it enough to consider filing, file it.

---

## Step 2 — Sync AGENTS.md known-issues section

If — *and only if* — the project's `AGENTS.md` already contains a "Known issues" section that summarizes open work items, update it to match the reconciled state:

1. List open work items (and any other "open-ish" statuses the project uses: `open`, `claimed`) via `agent-notes work-item find --status <s> --json`.
2. Recompute the severity breakdown and rewrite the summary line.
3. Replace stale bullets.

If `AGENTS.md` has no such section, skip this step. **Do not invent one** — fabricating a "Known issues" section is worse than not having one.

---

## Step 3 — Run the `reflect` skill

**Invoke the skill. Do not replicate it inline.**

The `reflect` skill writes the markdown file *and* ingests it into the agent-notes memory store as a `reflection`-type memory. If you write the file directly with Write or `cat > … <<EOF`, you skip the DB ingest — which means the reflection is invisible to `/start` next session, invisible to cross-kind search, and stops half of what reflections are for. The file-on-disk is the *artifact*; the memory-store row is the *index*. You need both.

Invoke the skill via your harness's skill mechanism (Claude Code: the Skill tool with `skill: "reflect"`; opencode: `/reflect`). Do this *after* step 1 so the reflection can reference the breadcrumbs it just saw move.

If the reflect skill is genuinely unavailable in your harness, fall back: write the file using the template in `~/.claude/skills/reflect/SKILL.md`, then run `agent-notes memory add --type reflection --name reflection-<date>-<model-slug> --body "$(cat <file>)"` explicitly. Note the fallback in the final report — don't hide it.

---

## Step 4 — Commit

Run in parallel:

```bash
git status
git diff --stat
git log --oneline -5
```

Review what's staged/unstaged. Watch for: secrets, `.env`, generated artifacts, files unrelated to this session's work. Stage deliberately — prefer named paths over `git add -A` when there's any doubt. `git add -A` is acceptable only when the diff has been reviewed and nothing surprising is present (an untracked reflection or breadcrumb you just authored is *not* a surprise).

Subject ≤ 72 chars, imperative mood, summarizing the session's outcome. Body: bullet the main changes; reference work item identifiers resolved or opened. Use a HEREDOC for the message. Never `--no-verify`. Do not push unless the user asked.

If you intend to push, this skill is the wrong gate — `/push` runs documentation checks and the test suite. `/end` is local wrap-up only; it does not run tests.

---

## Final report

```
## Session End — <date>

**Work items closed:** <list or "none">
**Work items opened:** <list or "none">
**Reflection:** <path written by /reflect> (ingested: yes / fallback)
**Commit:** <short hash> <subject>
```

State explicitly any step that couldn't be completed (no breadcrumbs convention, AGENTS.md had no Known-issues section, reflect skill unavailable, nothing to commit). Do not propose further work. Do not ask follow-up questions.

---

## Rationalizations to refuse

| Excuse | Reality |
|--------|---------|
| "I'll write the reflection file inline — saves a tool call." | You skip the DB ingest. The reflection becomes invisible to `/start` next session. Half the point is gone. |
| "I'll commit now and add the reflection in a follow-up." | The follow-up doesn't happen. The reflection rides in this commit or not at all. |
| "Tests passed earlier in the session, so I don't need to run them." | /end doesn't gate on tests; that's `/push`. But don't claim a green tree as evidence — the diff has moved. |
| "The work item dir is empty, so I don't need to dedup-search." | Search includes resolved/ and other workspaces. Empty active/ ≠ empty store. |
| "The user just said 'wrap it up so I can go,' so I'll trim." | The user asked for a wrap-up, not a half-wrap-up. If genuine time pressure is on, surface it explicitly and ask which step to cut — don't silently cut the reflection. |
| "Two minor issues aren't worth filing." | If they were salient enough to consider, file them. The cost of an unneeded breadcrumb is one row; the cost of a missed one is rediscovery next session. |
| "I'll just `git add -A` and trust it." | Untracked files you didn't author get swept up. Look at `git status` first; stage named paths if anything surprising is there. |

## Red flags — stop

- About to use Write to create a reflection file directly, bypassing the reflect skill.
- About to skip step 3 because "the user is in a hurry."
- About to run `git add -A` without having read `git status` since the last edit.
- About to invent an AGENTS.md "Known issues" section that wasn't there.
- Wording like "I'll add it after" or "follow-up commit" — those don't happen.

---

If `agent-notes` exits non-zero or its JSON shape doesn't match what
this skill expects, the CLI contract has drifted — file a work item
under project agent-notes.
