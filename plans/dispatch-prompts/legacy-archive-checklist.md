# Legacy Archive Checklist (Phase 6.4)

Manual steps to archive the old `breadcrumb-mcp` and `memory-mcp` repositories after the `agent-notes-mcp` omnibus has proven stable.

## Preconditions

1. **One week of clean operation** against the new server:
   - Run `agent-notes-doctor` daily and confirm all checks pass (green).
   - No audit drift (`agent-notes-breadcrumbs` → `audit` returns zero dirty projections).
   - No missing imported data (spot-check legacy breadcrumbs and memories by identifier).
   - All harness clients (Claude Code, OpenCode, Gemini CLI) work against new binaries.

## Archive steps

1. **Update legacy README redirect**  
   Edit `/projects/breadcrumb-mcp/README.md` — add at the top:
   > Superseded by `/projects/agent-notes-mcp/`. Historical reference; do not extend.

2. **Update legacy README redirect**  
   Edit `/projects/memory-mcp/README.md` — add at the top:
   > Superseded by `/projects/agent-notes-mcp/`. Historical reference; do not extend.

3. **Move to archive directory or mark**  
   Operator preference:
   - Option A: move each repo to an `archive/` directory alongside `/projects/`.
   - Option B: keep in place but add an `archived: true` badge / GitHub topic.

4. **Update harness configs**  
   Search any harness JSON/YAML that still references old binaries:
   ```bash
   grep -r "breadcrumb-mcp" /path/to/harness/
   grep -r "memory-mcp" /path/to/harness/
   ```
   Replace with `agent-notes-breadcrumbs`, `agent-notes-memory`, or `agent-notes-omnibus` as appropriate.

5. **Final commit each legacy repo**  
   ```bash
   cd /projects/breadcrumb-mcp
   git add README.md
   git commit -m "archived: superseded by agent-notes-mcp"
   cd /projects/memory-mcp
   git add README.md
   git commit -m "archived: superseded by agent-notes-mcp"
   ```

## Verification

- Both legacy repos have a final commit message matching `archived: superseded by agent-notes-mcp`.
- No remaining harness configs point to old binaries.
- `agent-notes-doctor` reports green on the consolidated DB.

## Out of scope

- Do NOT delete legacy repos — only archive/mark.
- Do NOT migrate new data back to legacy repos.
