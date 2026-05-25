---
name: end
description: Wrap up a working session — update/close breadcrumbs the agent worked on, open new ones noticed during the session, run the reflect skill, then commit. Invoke when the user signals end of session ("/end", "wrap up", "we're done", "close this out").
---

# /end — Session Wrap-Up

Close out a session cleanly in this order:

1. Reconcile breadcrumbs (close worked-on, open newly-noticed)
2. Sync AGENTS.md known-issues section from breadcrumbs state
3. Run the `reflect` skill
4. Commit the changes

Do not skip steps. Do not reorder — the reflection should see the breadcrumb updates so it can reference them, and the commit should pick up everything including the reflection file.

---

## Step 1 — Reconcile breadcrumbs

### 1a. Locate the breadcrumbs convention

Look for a breadcrumbs directory at the repo root, in this order:

1. `breadcrumbs/`
2. `.breadcrumbs/`
3. `docs/breadcrumbs/`

Read the directory's `README.md` if present — it usually defines the schema (frontmatter fields, statuses, severity levels, resolved-folder convention). Honor whatever convention the project uses; do not impose your own.

If no breadcrumbs directory exists, **skip this step entirely** — do not create one unprompted. Mention in the final summary that the project has no breadcrumbs convention.

### 1b. Close breadcrumbs worked on this session

For each breadcrumb addressed during the session, use the
`update-breadcrumb` skill (or call the CLI directly):

```
agent-notes breadcrumb update <identifier> \
  --path <repo-path> \
  --status <project-done-value> \
  --body "<existing body + resolution note>" \
  --json
```

Match the project's "done" value — `resolved`, `implemented`, or `closed` as the README/existing resolved entries show. If the project moves resolved items to a sub-folder on disk, leave that to the projection layer (Plan 003); the CLI/DB transition is the source of truth.

Only close breadcrumbs you actually addressed. If you partially addressed one, leave it open and append a note describing what's still outstanding.

### 1c. Open breadcrumbs for issues noticed

For each defect, design question, or gap noticed during the session that wasn't fixed, use the `file-breadcrumb` skill. Be honest about severity — don't downgrade real issues to look tidier; don't upgrade trivia to look thorough.

---

## Step 2 — Sync AGENTS.md known-issues section

If the project has an `AGENTS.md` with a "Known issues" line that summarizes open breadcrumb counts and key items, update it to match the reconciled state:

1. List open breadcrumbs:

   ```
   agent-notes breadcrumb find \
     --path <repo-path> \
     --status open --limit 50 --json
   ```

   (Repeat for any other "open-ish" statuses the project uses: `new`, `proposed`, `in-progress`.)

2. Compute severity breakdown (critical/high/medium/low) from the returned JSON.
3. Update the summary line: `**Known issues:** N open breadcrumbs (C critical, H high, M medium, L low) + R RFCs + D defect classes.`
4. Replace the bullet list under it with the current open breadcrumbs plus any recently-resolved items worth flagging. Do not keep stale entries.

If `AGENTS.md` has no known-issues section, skip this step.

---

## Step 3 — Run the reflect skill

Invoke the `reflect` skill. It will write a reflection file under the project's reflections directory covering: opinion of the project, the work done, what remains, and gaps.

Do this *after* the breadcrumb reconciliation so the reflection can accurately describe breadcrumb state.

---

## Step 4 — Commit

Run in parallel:

```bash
git status
git diff --stat
git log --oneline -5
```

Review what's staged/unstaged. Watch for:

- Secrets, credentials, `.env` files — never commit.
- Generated artifacts, caches, build output — usually should not be committed.
- Files unrelated to this session's work — investigate before including.

Stage deliberately — prefer named paths over `git add -A` if there's any doubt. For routine sessions where the diff has been reviewed and nothing suspicious is present, `git add -A` is acceptable.

Write a commit message:

- Subject line ≤ 72 chars, imperative mood, summarizing the session's outcome (not a file list).
- Body: bullet the main changes; reference breadcrumb numbers resolved or opened.
- Never use `--no-verify`. If a hook fails, fix the underlying cause and create a *new* commit (do not amend).

Use a HEREDOC for the message to preserve formatting. Do not push unless the user explicitly asked.

---

## Final report

After committing, output a compact summary:

```
## Session End — <date>

**Breadcrumbs closed:** <list or "none">
**Breadcrumbs opened:** <list or "none">
**Reflection:** <path written by /reflect>
**Commit:** <short hash> <subject>
```

Do not propose further work. Do not ask questions. If a step couldn't be completed (e.g. no breadcrumbs convention, nothing to commit, pre-commit hook failure), state that fact in the summary instead of the missing line.

---

If `agent-notes` exits non-zero or its JSON shape doesn't match what
this skill expects, the CLI contract has drifted — file a breadcrumb
under project agent-notes-mcp.
