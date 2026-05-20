# Phase 5 — Reflections (conditional on Phase 3.6 spike)

This phase is conditional. Its scope is determined by `plans/dispatch-prompts/phase-3.6-reflections-spike-outcome.md`.

## If 3.6 concluded "memories suffice"

Phase 5 collapses to a small follow-up. Write a one-page dispatch prompt at this path (overwrite this file) with these tasks:

1. Add `reflection` to the `memory_types` vocabulary in every workspace (or document the convention so it's added at workspace creation time).
2. Implement `extract_gaps(memory_name)` as a tool on the memory server (NOT a new server): fetches the memory body, parses the "Gaps to flag" section, returns structured BC drafts (kind/title/body/severity guesses). Agent confirms then calls `file_breadcrumb`.
3. Implement `mark_gaps_filed(memory_name, bc_identifiers: list[str])` — updates a `metadata JSONB` field on the memory row (add the column if Phase 3.6 hasn't already) with `gaps_filed_as` array and `gaps_extracted_at` timestamp. Decision 19 says reflection metadata is mutable even though the body is append-only.
4. Import historical reflections from `/projects/software-factory-2/reflections/`, `/projects/substrate/reflections/`, `/projects/software-factory/reflections/`, `/projects/breadcrumb-mcp/reflections/`, and `/projects/memory-mcp/reflections/` (if any). One memory per reflection file.
5. Write `plans/dispatch-prompts/reflect-skill-update.md` describing the `~/.claude/skills/reflect/SKILL.md` change: skill writes the markdown file as today, AND calls `add_memory` with `memory_type='reflection'`. The file-on-disk stays for human readability; the DB version is searchable.
6. Tests: every state-mutating tool gets a stdin-driven test asserting `change_log` rows; `extract_gaps` test against a real reflection file content.

Validation: 5 historical reflections imported and searchable via `search_memory(query="substrate constructor", memory_type="reflection")`.

## If 3.6 concluded "dedicated server needed"

Phase 5 is roughly the size of Phase 3. Write a full dispatch prompt at this path covering:

1. `schema/300_reflections.sql` per plan §4.4 — `(workspace_id, project_id, slug)` key, structured sections JSONB, `gaps_extracted_at` + `gaps_filed_as TEXT[]`, append-only body + sections per decision 19, `replaced_by` link relationship.
2. `src/agent_notes/servers/reflections.py` with `add_reflection`, `find_reflections`, `get_reflection`, `extract_gaps`, `mark_gaps_filed`. Inherits 9 core tools.
3. Update `agent-notes-search`'s `search_all_notes` and `all_notes_search_v` to include the new kind.
4. Update `agent_notes/cli.py` to add the `reflections` kind to the dispatch.
5. Add `agent-notes-reflections` entry point to `pyproject.toml`.
6. Import historical reflections.
7. `reflect` skill update doc.

The shape of the dispatch prompt follows the Phase 3 template (in this directory) — copy that file and adapt.

## What to do right now (before 3.6 lands)

Nothing. Wait for Phase 3.6's outcome report; then come back and write the correct version of this prompt above.
