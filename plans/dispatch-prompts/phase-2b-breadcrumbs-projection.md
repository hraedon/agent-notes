# Phase 2b — Breadcrumbs projection + `/end` skill update

You are implementing Phase 2b of the agent-notes-mcp project: adding the markdown projection apparatus to the breadcrumbs server, the one-shot normalize/regenerate script, and the `compute_projection_paths` integration. **The `/end` skill change is a separate manual step** the operator does after this lands — you don't touch `~/.claude/skills/end/`.

## Project state at HEAD

Phase 2a is complete and merged: breadcrumbs schema (`schema/100_breadcrumbs.sql`), DB-only breadcrumbs server (`src/agent_notes/servers/breadcrumbs.py`), `import_legacy_bc.py`, ~30 new tests. Markdown projection is NOT yet wired — `file_breadcrumb` only writes the DB row, `update_breadcrumb` only updates `file_path` in the DB without touching disk.

Core library already provides everything you need: `core/projection.py` exposes `parse_frontmatter`, `render_frontmatter`, `slugify`, `render_index`, and `safe_write(absolute_path, content, expected_sha256) -> SafeWriteOutcome` with the full DRIFT/WRITTEN/UNCHANGED/FS_ERROR contract.

## Read first

1. `plans/001-architecture-and-implementation.md` — §2 (decisions, especially 8, 15, 17, 24, 26), §4.2 (breadcrumbs schema — note `projection_sha256` and `projection_dirty` columns are already there), §7 (markdown projection spec), §9 Phase 2b (task table).
2. `AGENTS.md`.
3. `plans/dispatch-prompts/README.md` for the hard test requirement.
4. `src/agent_notes/core/projection.py` to understand the `safe_write` contract.
5. `src/agent_notes/servers/breadcrumbs.py` (Phase 2a output) — you extend `file_breadcrumb` and `update_breadcrumb`.
6. A handful of real breadcrumb files for the canonical frontmatter shape: `/projects/substrate/breadcrumbs/195-no-visibility-into-downstream-constructor-consumers.md`, `/projects/software-factory-2/breadcrumbs/120-implementer-interface-amendment.md`. The frontmatter today uses keys like `number`, `title`, `severity`, `status`, `kind`, `author`, `date`, `tags`, `related`. Plan §1 specifies the v1 frontmatter (under decision 8 / §7) uses `bc_frontmatter_version: 1`, `identifier`, etc. — see the example block.

## Scope

### 2b.1 — Wire `file_breadcrumb` to write markdown

Order of operations (decision 26 — embed outside transaction):
1. Embed text in-process.
2. Compose canonical frontmatter (`bc_frontmatter_version: 1`, all the fields per §7's example block).
3. Compose body content = frontmatter + blank line + body markdown.
4. Compute absolute path: `projects.repo_root + projects.breadcrumbs_dir + file_path` where `file_path = f"{slugify(title)}.md"` (or `f"{identifier}-{slugify(title)}.md"` if you prefer the legacy convention — check what existing files use and match).
5. BEGIN transaction.
6. INSERT into `breadcrumbs` with `embedding`, `file_path`, no `projection_sha256` yet.
7. Write `change_log` event=`filed` (the trigger already does this on INSERT — verify it does and don't double-write).
8. COMMIT.
9. **After COMMIT**: call `safe_write(absolute_path, content, expected_sha256=None)` (first projection — no expected hash). On WRITTEN, UPDATE the bc row to set `projection_sha256 = sha256(content)`. On FS_ERROR, UPDATE to set `projection_dirty = true` and log clearly. UNCHANGED shouldn't happen on first projection.

The "write change_log first, then safe_write" order is eventual-consistency (decision 7): DB is authoritative; disk catches up. If the FS write fails the DB stays committed and `audit` will flag the dirty row.

### 2b.2 — Wire `update_breadcrumb` to re-write markdown + move on terminal status

When `update_breadcrumb` is called:
1. Apply DB updates as Phase 2a already does (re-embed on title/body change, update `closed_at` via trigger).
2. If status transitioned to a terminal status (check via `vocabularies.is_terminal` for the new status), compute the canonical resolved-dir `file_path` (e.g., `resolved/RFC-031.md`) and UPDATE the DB row's `file_path`. (Phase 2a already does this step.)
3. **After the DB transaction commits**: re-render the markdown content from the current row state. Call `safe_write(new_absolute_path, content, expected_sha256=old_projection_sha256)`.
4. **The server NEVER runs git** (decision 15). The `/end` skill calls `compute_projection_paths` to learn the old and new paths and performs the `git mv` itself. If the file at `new_absolute_path` was just written by safe_write, that's because we're projecting to the new location — but the old file still exists. **Do not delete the old file.** The `/end` skill's `git mv` (old → new) takes care of moving it; if the new file already exists at the destination, `git mv` will refuse cleanly. The simpler protocol: on terminal-status transition, safe_write to the NEW location; leave the old file in place; the `/end` skill detects the drift and resolves via `git mv` then a final render.
5. On `safe_write` DRIFT (hash mismatch): set `projection_dirty=true`, return a clear tool error message suggesting the agent call `reconcile_projection`.
6. On `safe_write` FS_ERROR: set `projection_dirty=true`; the row stays committed with the new state but the file lags.

### 2b.3 — New tools: `render_index`, `audit`, `reconcile_projection`

**`render_index(project_slug) -> str`**
1. Fetch all breadcrumbs for the project, sorted by status (using `vocabularies.sort_order`) then by `severity.sort_order` then by `identifier`.
2. Look for `breadcrumbs_dir/README.preamble.md` — if present, include verbatim at the top of the output. (Substrate's README has hand-written prose; this lets humans keep that without losing it on regeneration. Plan §5 of the OG breadcrumb-mcp plan called this out.)
3. Generate "Open" and "Resolved" sections per the existing substrate README format (read `/projects/substrate/breadcrumbs/README.md` for the target shape).
4. Compose `README.md` content; `safe_write` it to `repo_root + breadcrumbs_dir + "README.md"` with hash check.
5. Track the README's projection hash on... hmm, the projects table doesn't have a projection_sha256 column. **Implementation choice:** for the README, skip the projection_sha256 check (just overwrite); the README is generated, not user-edited. Document this in a comment citing this paragraph. Alternative: store the README hash in a `project_metadata JSONB` column on `projects` — overkill for now.

**`audit(project_slug) -> str`** (this is the doctor's BC-level companion)
1. For every BC row: compute the absolute path; if file exists, compute SHA-256 of disk bytes; compare to `projection_sha256`. Report drift.
2. For every BC row with `projection_dirty=true`: report.
3. For every file in `breadcrumbs_dir` that doesn't have a matching DB row: report orphan.
4. Return a human-readable summary. No mutations.

**`reconcile_projection(identifier, choice: Literal["use_disk", "use_db", "merge_manually"])`**
1. Look up the BC by `(project_slug, identifier)`.
2. If `use_disk`: parse the file's frontmatter and body, UPDATE the bc row to match (re-embedding on body change), then update `projection_sha256 = sha256(disk_bytes)`, clear `projection_dirty`.
3. If `use_db`: re-render from the DB state, `safe_write` (this time bypassing the hash check since the agent confirmed), update `projection_sha256`, clear `projection_dirty`.
4. If `merge_manually`: return both versions side by side and instruct the agent to edit + call `update_breadcrumb` then `reconcile_projection use_db`.

### 2b.4 — One-shot normalize/regenerate script: `scripts/regenerate_markdown.py`

CLI: `agent-notes-regenerate --project <slug>` (or `--all`). For each project:
1. For every BC row, render canonical frontmatter+body and `safe_write` to the canonical path. Use `expected_sha256=None` for this one-time pass (we're DECLARING the canonical form). After write, UPDATE `projection_sha256 = sha256(content)`.
2. Call `render_index` to emit the README.
3. Print a summary: N files written, N unchanged, N would-have-drifted-but-we-overwrote (with a flag to make it interactive if needed).

This is the "frontmatter v1 normalize" one-shot. The expected outcome on each of sf2/substrate/sf1 is one commit titled `breadcrumbs: frontmatter v1 normalize` (committed manually by the operator after reviewing the diff).

### 2b.5 — Tests

The same hard requirement as Phase 2a: every state-mutating tool gets a stdin-driven end-to-end test asserting the `change_log` row.

Additionally:
- `safe_write` is already tested in `tests/test_projection.py`; you're just composing it. But add an integration test that drives `file_breadcrumb` end-to-end against ephemeral PG + a tmp directory for `breadcrumbs_dir`, then verifies the file exists with the right content AND `projection_sha256` is set on the DB row.
- A drift test: `file_breadcrumb` writes a file; modify the file on disk; call `update_breadcrumb` (changing title); assert DRIFT outcome → tool returns error + `projection_dirty=true` is set.
- A terminal-status test: `file_breadcrumb` writes BC at `195.md`; `update_breadcrumb status=resolved`; assert the new file at `resolved/195.md` exists AND the old `195.md` still exists (the skill will mv it). Document this as the explicit protocol.
- `render_index` test: file 3 BCs in different statuses, call `render_index`, assert the output contains all three under the right section headers.
- `audit` test: create a clean state, run audit, assert "no drift"; modify a file on disk, run audit, assert it reports drift.
- `reconcile_projection` tests for all three `choice` modes.

### 2b.6 — `/end` skill update

**You do NOT modify the skill.** Instead, write a short companion doc `plans/dispatch-prompts/end-skill-update.md` that describes the exact changes needed to `~/.claude/skills/end/SKILL.md` so the operator can apply them. Include:
- The new steps the skill must perform after the existing "update breadcrumbs" stage: for each touched project, call `compute_projection_paths` for any BCs whose status transitioned, perform `git mv` (with `--force` if needed for the leftover old file), then call `render_index`, then `git add` and commit.
- The handling of `audit` output: if `audit` reports drift on any BC, surface to the user and don't auto-commit.

## Validation

1. All Phase 2a tests still pass.
2. `ruff check` clean.
3. New Phase 2b tests pass (~15 new).
4. `agent-notes-regenerate --project substrate --dry-run` (add a dry-run mode) shows the projected diff without writing.
5. The `agent-notes-breadcrumbs` binary's `tools/list` now includes `render_index`, `audit`, `reconcile_projection`, `compute_projection_paths`.
6. `plans/dispatch-prompts/end-skill-update.md` is clear enough that the operator can apply it without further questions.

## Out of scope

- Memory server (Phase 3).
- Cross-kind search (Phase 4).
- Reflections (Phase 3.6 spike / Phase 5).
- Resources surface for breadcrumbs (Phase 6.1).

## Report at end

- Files added/modified.
- Test counts before/after.
- Honest assessment of the projection protocol: does the "leave-old-file-for-skill-to-mv" pattern feel clean, or does something feel off?
- Any places where `safe_write`'s contract didn't fit and you had to work around it (those are bugs in `core/projection.py` worth raising).
