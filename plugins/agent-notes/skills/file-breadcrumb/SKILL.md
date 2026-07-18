---
name: file-breadcrumb
description: File a breadcrumb when you've found a problem worth tracking — a bug, design question, latent gap, or todo that won't fit in the current change. Invoke when the user asks to "file a breadcrumb", "track this", "note this for later", or when you yourself spot something the next agent should know about.
---

# /file-breadcrumb — Track a Problem Worth Remembering

Breadcrumbs are persistent, project-scoped notes about defects, design
questions, and gaps. They survive across sessions and agents. They are
not TODOs in the change you are making right now — those belong in the
diff. A breadcrumb is for things you noticed but won't fix this turn.

## When to file (and when not to)

File one when:

- You saw a real defect, latent issue, or sharp edge that the current
  change does not address.
- You hit a design question that needs human judgment and you don't have
  it yet.
- You discovered a convention that exists in some files but not others,
  and normalizing it is out of scope here.
- A test is missing for a load-bearing behavior and you can't add it now.

Do **not** file one when:

- The thing is already fixed in the diff you're about to commit.
- It's a vague "we should clean this up someday" without a concrete
  consequence. Vague breadcrumbs become noise.
- It belongs in a commit message, PR description, or AGENTS.md instead.
- You haven't searched. Almost-duplicates are worse than no breadcrumb.

## Workflow

1. **Search first. No exceptions.** Even when the active/ directory
   looks empty, even when you're sure it's new, even when the user is
   in a hurry. Duplicates are the single failure mode this store has,
   and they're cheap to prevent and expensive to clean up. Run both
   of these before going further:

   ```
   agent-notes work-item find --path <repo-path> --scope workspace --text "<keywords>" --json
   agent-notes search all "<query>" --path <repo-path> --json
   ```

   The first hits breadcrumbs across the whole workspace (not just
   active/, not just this project's current view). The second catches
   cases where the problem was recorded as a memory or reflection
   rather than a breadcrumb. If a close match exists in either, use
   the `update-breadcrumb` skill on the existing record instead. For
   a richer search workflow (scopes, result interpretation), see the
   `find-breadcrumb` skill.

2. **Pick a type from the project's existing vocabulary.** Run
   `agent-notes vocabulary list --workspace <ws> --kind wi_kind
   --json` to see what's in use. Common values: `bug`, `defect`,
   `design-question`, `todo`, `observation`, `rfc`. Don't invent a new
   type unless none of the existing values fit; new vocabulary entries
   are a project-level decision, not a per-breadcrumb one.

3. **Write the body so future-you understands why it matters.** Two
   short paragraphs is usually right. Include: what you observed,
   where (file/line if applicable), what's at risk if nobody addresses
   it, and any first idea about the fix. Do not pad with PR-style
   prose. The reader is an agent or person under time pressure.

4. **Title is a sentence, not a label.** "Memory CLI swaps workspace/
   project args in get_memory call site" beats "fix get_memory". The
   title is what shows up in lists; it must be readable on its own.

5. **File it.** Run:

   ```
   agent-notes work-item file \
     --path <repo-path> \
     --title "<sentence>" \
     --body "<two paragraphs>" \
     --type <existing-type> \
     --status open \
     --severity <low|medium|high|critical> \
     --json
   ```

   Parse the JSON; the returned `identifier` is the canonical handle
   (e.g. `WI-001`). Tell the user that identifier so they can
   reference it.

## Severity guidance

- `critical`: data loss, security exposure, or a workflow is broken now.
- `high`: real defect, user-visible, no workaround.
- `medium`: defect with a workaround, or design debt with concrete cost.
- `low`: cosmetic, naming inconsistency, mild ergonomics.

Be honest — both directions. Don't downgrade real issues to look tidy;
don't upgrade trivia to look thorough.

---

If `agent-notes` exits non-zero or its JSON shape doesn't match what
this skill expects, the CLI contract has drifted — file a breadcrumb
under project agent-notes.
