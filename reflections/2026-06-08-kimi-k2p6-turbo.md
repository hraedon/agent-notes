---
model: fireworks-ai/accounts/fireworks/routers/kimi-k2p6-turbo
datetime: 2026-06-08T14:00 UTC
project: agent-notes
---

# Session Reflection — 2026-06-08

**Work summary:** Implemented Plan 008 P3 cross-project layer — the derived index, registry, export/ingest, and cross-repo ready query. Also completed P2 merge/reconcile (deterministic merge + reconcile_entity) which was flagged as pending from the previous session.

---

## On the project

The agent-notes codebase is coherent and the Plan 008 phasing is well-designed. The separation of P0 (single-writer kernel), P1 (verifier), P2 (merge/reconcile), and P3 (cross-project) is clean — each layer adds capability without breaking the previous one. The existing test infrastructure (testcontainers for Postgres) is solid and makes the integration tests trustworthy.

One thing that feels slightly fragile is the `work_items_ready_v` view — it now has two NOT EXISTS subqueries (same-project blockers via `links` and cross-project blockers via `cross_project_work_items`). The query plan hasn't been checked at scale; if the cross-project cache grows large, the view might need materialization. The plan document mentions this is a P3 concern but doesn't explicitly call out a performance benchmark or limit. I'd flag this as a latent gap.

## On the work done

**What went well:**
- The schema migration (`701_work_log_cross_project.sql`) is fully idempotent and the view update is clean. The `ON CONFLICT` UPSERT pattern in `ingest_jsonl_ops` means ingestion is idempotent by design — which is exactly what a derived index needs.
- The `cross_project.py` module reuses the same fold logic (`_apply_op_to_state`, `_resolve_status_lattice`) from `kernel.py`, so there's no drift between local and foreign folding. This was the right call.
- The round-trip test (`test_export_then_ingest`) proves the export → ingest → rebuild pipeline works end-to-end.

**What I'd want a second pair of eyes on:**
- The `cross_project_work_items` cache doesn't store the `body_hash` content — only the hash. If the foreign body is needed, we need to also ingest `content_blobs` from the source repo. The current design assumes bodies are resolved lazily (or not needed for the blocker query). Is that the right assumption? The plan says "bodies are content-addressed blobs" but the cross-project layer doesn't replicate the blob table.
- The `freshness_offset` field in `cross_project_ops` is populated from the JSONL but never actually used for incremental ingestion. The current approach is "ingest everything, rely on idempotent UPSERT." For a large repo's op-log, this is O(N) every time. The plan mentions size-or-time segments (P4) but not incremental delta ingestion.

**What was awkward:**
- The `ingested_at` column name in `cross_project_ops` vs `created_at` in `op_log` caused a subtle bug in the first fold attempt (the code looked for `created_at` which doesn't exist in the cross-project table). The test caught it immediately, but it suggests the column naming convention isn't uniform across tables.

## On what remains

**P3 (next session):**
1. Cross-project trigger loop — wake routing for `request.created`/`dependency.blocked` events. This needs an async listener on the `agent_notes_op_log_events` NOTIFY channel, which maps to the agent-wake HTTP bridge. The current `bridge.py` listens on `agent_notes_changes` (the old `change_log` channel). The new channel `agent_notes_op_log_events` needs its own bridge or an extension of the existing one.
2. The `wait` op should probably resume a session when the target closes. The current `wait_on_work_item` method writes the `wait` op but there's no mechanism to poll the foreign status or route the wake event back to the waiting session.

**P4 (future):**
1. regista coordinator integration — atomic claim/lease/heartbeat
2. Degrade contract — coordinator down → reads + progress on held items + append/file freely; no new claims
3. `requeue_expired` sweep for lease-expired items
4. Causal-stability watermark for compaction

## Gaps to flag

- **`content_blobs` cross-project replication:** `cross_project_work_items` stores `body_hash` but not the body content. If a foreign work item's body is needed (e.g., for `diagnose` or `get --with-body`), the current code will look up the hash locally and fail. The `get_cross_project_work_item` function does not attempt to fetch the body. `src/agent_notes/core/cross_project.py:244`
- **`freshness_offset` unused:** The schema has `freshness_offset` but the ingestion code doesn't use it for incremental delta ingestion. The `rebuild_cross_project_cache` function rebuilds *everything* every time. For a large repo, this is O(N). `src/agent_notes/core/cross_project.py:283`
- **No performance benchmark for `work_items_ready_v`:** The view now has two NOT EXISTS subqueries. The cross-project one joins `cross_project_links` to `cross_project_work_items`. At scale, this may need a materialized view or an index-only scan. No benchmark exists. `schema/701_work_log_cross_project.sql:114`
- **Schema drift in `created_at` vs `ingested_at`:** The `cross_project_ops` table uses `ingested_at` instead of `created_at`, which is the name used in `op_log`. This tripped a bug in the first fold attempt. The test caught it, but it's a convention inconsistency. `schema/701_work_log_cross_project.sql:44`
- **`projects.log_location` / `wake_channel` not validated:** The registry columns are free-text with no format validation. A bad log_location URL or wake_channel endpoint won't be caught until the bridge tries to POST. `schema/701_work_log_cross_project.sql:17`
