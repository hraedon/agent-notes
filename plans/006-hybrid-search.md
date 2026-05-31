# Plan 006 — Hybrid Search (vector + keyword)

**Status:** proposed 2026-05-28 — **LOW priority / optional**
**Author:** Opus 4.8 (portfolio review)
**Strategic role:** Optional slack-feature track. agent-notes is more done than
the "add features" framing implied — embeddings are already local
(`nomic-embed-text-v1.5`), the CLI surface is broad, and it is dogfooded daily.
Its load-bearing contribution to the 3-week plan is **being the cross-project
attested action in agent-provenance Plan 006**, not a new feature. This plan is
the one genuine slack item, to pick up only if Track 1 and Track 2 are staffed.

## First: two free wins before any feature work

1. **Commit the pending Phase 9d deletion.** The MCP removal (`core/mcp.py`,
   `core/server.py`, `core/resources.py`, `servers/*`, and their tests) is
   sitting uncommitted in the working tree. A half-deleted transport is the
   worst state to leave it in. Commit it.
2. **Defer the regista migration.** The CROSS-PROJECT-UNIFICATION doc wants to
   dissolve agent-notes into a regista-event-sourced "cairn-noted" (migrate the
   standalone Postgres in, projections rebuilt by replay). Do **not** do this
   yet: it couples the one tool that works daily to the audit stack before that
   stack has proven the event-sourced model on a single concern. Let
   agent-provenance Plan 006 prove it first. Record this deferral explicitly.

## The feature: hybrid search

Search today (`core/search.py`) is semantic-only — pgvector cosine over
`all_notes_search_v`. Pure vector search misses exact-term matches (an error
code, a flag name, a BC number), which is a real recall gap for a
breadcrumb/memory store full of identifiers.

### Scope
- Add a lexical signal alongside the vector signal: Postgres full-text search
  (`tsvector`/`tsquery`) or `pg_trgm` similarity over the same rows.
- Fuse with the existing vector score via Reciprocal Rank Fusion (RRF) — no
  extra dependency, robust to score-scale differences.
- Expose as the default ranking for `agent-notes search all/breadcrumb/memory`,
  with `--mode vector|lexical|hybrid` to override (hybrid default).
- Keep `--json` output shape stable for the skills that shell to it.

### Acceptance
- A query that is an exact identifier (e.g. a BC number or flag) returns the
  matching row even when its embedding is not the nearest neighbor.
- Semantic queries are no worse than today.
- `--mode` flag works; default is hybrid; JSON shape unchanged.
- Tests cover the fusion ranking and the exact-identifier recall case.

## Non-goals
- Cross-encoder reranking (heavier dep; revisit only if RRF proves insufficient).
- The regista migration (deferred above).
- Any change to the embedding provider — it is already local and fine.
