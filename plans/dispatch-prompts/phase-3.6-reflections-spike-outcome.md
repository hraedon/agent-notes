# Phase 3.6 — Reflections-as-memories spike outcome

**Date:** 2026-05-20
**Author:** GLM-5.1 (Phase 3 implementation)
**Decision:** Memories suffice. Phase 5 collapses to a small follow-up.

## What was tested

Three integration tests in `tests/test_memory.py::TestReflectionsSpike`:

1. **`test_store_reflection_as_memory`** — stored a reflection (with structured sections, "Gaps to flag", wikilinks to BC identifiers) as `memory_type='reflection'` with JSONB `attributes` for model/gaps_extracted_at metadata. Retrieved via `get_memory`. All sections and gaps present and searchable.

2. **`test_search_reflections_by_type`** — semantic search filtered to `memory_type='reflection'` found the reflection by querying its content topics. The `memory_type` filter works cleanly with the HNSW index (index condition: `WHERE active = true AND embedding IS NOT NULL`, filter is post-index).

3. **`test_reflection_gaps_have_wikilinks`** — the `[[BC-001]]` and `[[BC-002]]` references in the "Gaps to flag" section auto-created `relates_to` links. `trace_graph` from the reflection returns both gap BCs as dependency nodes. This is the exact `extract_gaps` workflow the plan describes, achieved without a dedicated server.

Also examined a real historical reflection (`/projects/software-factory-2/reflections/2026-05-19-sonnet-4-6.md`, 53 lines, 5 sections, 6 gap items with wikilinks) to validate that the memory schema handles real content.

## Analysis

### What works well

- **`memory_type='reflection'`** is a clean discriminator. All reflection-shaped queries (`search_memory(memory_type='reflection')`, `list_memories(memory_type='reflection')`) work via standard filters.
- **`attributes JSONB`** (already in the `memories` schema) holds `model`, `gaps_extracted_at`, `gaps_filed_as` without schema changes. Decision 23 already establishes that non-trigger-load-bearing metadata goes in JSONB. Reflection metadata is exactly this category.
- **Wikilink gap extraction** comes free from Phase 3.3's `[[name]]` parser. Reflection gaps are `[[BC-NNN]]` references in the body; they auto-create `relates_to` links. `extract_gaps` can be a thin tool that fetches the memory body, parses the "Gaps to flag" section, and returns structured BC drafts.
- **Append-only body** is already the memory contract (you `add_memory` a new version that supersedes the old one; the old body stays in the DB with `active=false`). Decision 19's "body append-only, metadata mutable" is satisfied by this pattern.
- **No projection_sha256 needed** (decision 24) — reflection files on disk are the human-readable copy; the DB version is searchable. No hash-check apparatus required.

### What's slightly awkward

- **`mark_gaps_filed`** mutates `attributes JSONB` in place. The current `add_memory` tool doesn't have a generic "update attributes" operation — it only supersede-inserts. Phase 5 needs a lightweight `update_memory_attributes(name, key, value)` helper (or just let `mark_gaps_filed` do a direct SQL UPDATE). This is a ~20-line addition to the memory server, not a new server.
- **`list_memories` doesn't filter by `attributes`** — a `mark_gaps_filed` query like "find reflections with unfiled gaps" needs `WHERE attributes->>'gaps_filed_as' IS NULL`. This is a minor addition to `list_memories` or a dedicated query, not a schema strain.
- **The `slug` convention (`YYYY-MM-DD-model`)** isn't enforced by the schema. It's a naming convention that the `reflect` skill owns. Memories don't care about name format.

### What was NOT strained

- The partial unique index on `(project_id, name) WHERE active` works correctly with reflection names like `reflection-2026-05-20`.
- Supersedes chain works — a new reflection for the same date/model supersedes the old one.
- The `trace_graph` CTE doesn't need changes to support reflection-to-BC links.

## Conclusion

**Memories suffice for the reflection use case.** No dedicated server, no new schema, no `schema/300_reflections.sql`.

### Phase 5 scope (collapsed)

1. Add `extract_gaps` tool to the memory server (~30 lines): fetches memory body, parses "Gaps to flag" section, returns structured BC drafts.
2. Add `mark_gaps_filed` tool to the memory server (~15 lines): UPDATE `attributes` JSONB with `gaps_filed_as` array.
3. Import historical reflections from `reflections/` directories into the memory server.
4. Update `reflect` skill to call `add_memory` with `memory_type='reflection'`.
5. Tests for the two new tools.

### What to update in the plan

- §4.4: strike the dedicated `reflections` table. Replace with: "reflections are stored as memories with `memory_type='reflection'`; metadata in `attributes JSONB`; gaps extracted via wikilink parser."
- §9 Phase 5: collapse to the 5-task follow-up above.
- `plans/dispatch-prompts/phase-5-reflections-conditional.md`: replace with the collapsed scope.
