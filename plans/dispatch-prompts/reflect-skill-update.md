# Reflect Skill Update — Phase 5 integration

This document describes the exact changes needed to `~/.claude/skills/reflect/SKILL.md` so the `/reflect` skill also ingests reflections into the agent-notes memory store via MCP.

## Why

Phase 5 (decision 25) concluded that memories suffice for reflections. Reflections are stored as memories with `memory_type='reflection'`. Without updating the skill, reflections written to disk are not searchable via `search_memory` / `find_reflections` — defeating the point of unifying reflections with the memory store.

The file-on-disk stays for human readability and git history; the DB version is the searchable canonical form.

## Prerequisites

- The agent-notes memory server must be configured as an MCP server in the agent's harness config (e.g. `opencode.json` or `.claude/settings.json`).
- The workspace and project must exist in the agent-notes DB. The skill should call `list_projects` to find the correct project slug for the current repo.

## Changes to apply

### 1. After Step 4 (Save the file), insert a new step:

> **4b. Ingest the reflection into the agent-notes memory store**
>
> After saving the markdown file to disk, call the `add_memory` tool on the agent-notes-memory server with these arguments:
>
> ```
> workspace: <workspace slug for this repo>
> project:   <project slug for this repo>
> name:      "reflection-<YYYY-MM-DD>-<model-slug>"   (same stem as the filename)
> memory_type: "reflection"
> body:      <full reflection content including the YAML frontmatter>
> attributes:
>   model: <model-id>
>   gaps_extracted_at: "<ISO timestamp>"
> ```
>
> If the agent-notes MCP server is not configured or the call fails, log a brief warning and continue — the file on disk is still valid. The DB ingest is best-effort, not blocking.

### 2. Update Step 5 (Report)

Change the one-line report from:

> Reply with one line: the path of the file you wrote.

To:

> Reply with one line: the path of the file you wrote, followed by confirmation of DB ingest (e.g. "reflection-2026-05-21: ingested into project sf2" or "reflection-2026-05-21: written to disk only (MCP server not available)").

### 3. Update the "Gaps to flag" template section

Add a note after the existing template text:

> When writing gap entries, use the `[[identifier]]` wikilink syntax where possible to reference known breadcrumb identifiers. This allows `extract_gaps` to parse them automatically and `trace_graph` to traverse them. Example: `- [[BC-195]]: Missing error handling in embed singleton.`

## Protocol summary

The invariant: **disk file is for humans and git; DB row is for search and tools** (decision 4 — DB is working SoT, markdown is projection; for reflections, both directions matter).

1. Skill writes the markdown file to disk (existing behavior, unchanged).
2. Skill calls `add_memory` with the same body, `memory_type='reflection'`, and structured attributes.
3. The `[[name]]` wikilinks in the body auto-create `relates_to` links in the `links` table (Phase 3.3).
4. Future sessions can `find_reflections` / `search_memory(memory_type='reflection')` to discover prior art.
5. `extract_gaps` parses the "Gaps to flag" section and returns structured BC drafts.
6. `mark_gaps_filed` tracks which gaps have been converted to breadcrumbs.

## Not in scope

- Modifying the reflect skill's template or analysis steps — only the storage path changes.
- The `import_reflections.py` one-shot script (operator manual step, not a skill concern).
- Breadcrumb projection apparatus (Phase 2b / end-skill-update).
