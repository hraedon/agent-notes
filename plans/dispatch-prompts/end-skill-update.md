# End Skill Update — Phase 2b integration

This document describes the exact changes needed to `~/.claude/skills/end/SKILL.md` so the `/end` skill uses the projection apparatus added in Phase 2b.

## Why

Phase 2b added `compute_projection_paths`, `render_index`, and `audit` as MCP tools on the breadcrumbs server, but the `/end` skill still operates on the old file-move-by-hand convention. Without updating the skill, the projection apparatus is wired but the closing-loop integration is unowned: markdown files get written by the server, but status transitions (active → resolved) leave stale files at the old path and the README index drifts.

## Changes to apply

### 1. Insert a new step after Step 1b (close breadcrumbs worked on)

After updating frontmatter status and before opening new breadcrumbs, add:

> **1b-ii. Re-project closed breadcrumbs via the MCP server**
>
> For each breadcrumb whose status changed to a terminal state during this session:
>
> 1. Call `compute_projection_paths` with `(project, identifier)` to get the old and new canonical paths.
> 2. If `old_absolute != new_absolute`, perform `git mv <old_absolute> <new_absolute>` (use `--force` if the new file already exists from the server's safe_write). If `old_absolute` doesn't exist on disk (server hasn't written it yet), skip the mv — the server will project to the correct path on next file_breadcrumb/update_breadcrumb call.
> 3. After all moves, call `render_index` for each touched project to regenerate `breadcrumbs/README.md`.
> 4. If `audit` reports any drift for the project, surface the drift to the user and do **not** auto-commit until resolved.

### 2. Update Step 1c (open breadcrumbs for issues noticed)

After creating the new breadcrumb file, add:

> Call `render_index` for the affected project so the new entry appears in `breadcrumbs/README.md`.

### 3. Update Step 4 (commit)

After staging files and before writing the commit message, add:

> Run `audit` for each touched project. If any drift is reported:
> - Surface the audit output to the user.
> - Do **not** auto-commit. Instruct the user to run `reconcile_projection` for each drifted breadcrumb, or to confirm the commit with a manual override.
>
> If audit is clean, proceed with commit as usual.

### 4. Update the final report

Add a line:

> **Audit status:** <"clean" or list of drifted identifiers>

## Protocol summary

The key invariant: **DB is the source of truth; markdown on disk is a deterministic projection** (decision 4). The `/end` skill is the only actor that runs `git` (decision 15). The server never runs `git` itself. On status transitions:

1. Server writes to DB + writes markdown to the canonical new path via `safe_write`.
2. The old file at the previous path is left in place (the server doesn't delete it).
3. `/end` skill calls `compute_projection_paths` to discover old → new mapping, runs `git mv`, then `render_index`, then commits.

If the old file doesn't exist on disk (e.g. the server couldn't write due to FS error and `projection_dirty` is set), the skill should still proceed — the next `render_index` or explicit `reconcile_projection` call will fix the discrepancy.

## Not in scope

- Memory server projections (memories don't project to disk per decision 4).
- Cross-kind search (Phase 4).
- The `regenerate_markdown.py` one-shot script (that's an operator manual step, not a skill concern).