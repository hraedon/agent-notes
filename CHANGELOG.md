# Changelog

All notable changes to the agent-notes project. This project follows the versioning scheme defined in the architecture plans.

## [v1.0.0] — 2026-06-08

### Tier A — Switch to the work-log coordination kernel

Plan 008 Tier A is complete. This is the switch-to-new-version bar.

- **P0 kernel** — `op_log`, `content_blobs`, `work_items` cache, fold, `ready`/`claimable` query, event surface, CLI.
- **P1 verifier** — Standalone `agent-notes verify` CLI: hash-chain, signature, policy checks.
- **P2 status lattice** — Fail-safe `open > claimed > closed > deferred` with tie-break by `op_id`. Deterministic `merge_entity` / `reconcile_entity`.
- **P3 cross-project** — `request`/`wait` ops, derived index (`cross_project_ops`, `cross_project_work_items`, `cross_project_freshness`), registry (`projects.log_location` / `wake_channel`), export/ingest JSONL, reverse-edge map, trigger loop (`agent-notes-trigger-loop`).
- **P4 local lease** — `work_item_leases` table, claim/heartbeat/release CLI, `requeue_expired` sweep.
- **Tier A degrade contract** — `coordinator-absent / local-lease` is the default safe mode. `doctor` reports the active mode. No distributed coordinator is required for daily use.
- **Migration script** — `migrate_breadcrumbs_to_work_items.py` idempotently migrates breadcrumbs → work-item entities, links → typed edges, `change_log` → op-log.
- **Surface update** — All 7 skills updated to use `work-item` commands and `wi_*` vocabulary.
- **Docs** — README and AGENTS.md describe the kernel as the live model and state plainly that the regista coordinator is an optional, not-yet-attached L3 layer.

### Fixes

- Fixed `schema/700_work_log_kernel.sql` vocabulary seeding to use a dynamic CTE lookup for the `default` workspace ID instead of hardcoding `workspace_id = 1`. This prevents missing `wi_kind`/`wi_status`/`wi_severity` entries on existing databases where the default workspace was created with a different ID.

### Tier B — Deferred (not required for switch)

- L3 regista coordinator integration (atomic distributed claim/lease/heartbeat).
- `requeue_expired` daemon timer (cron/systemd).
- Causal-stability watermark + archival truncation.
- Keyless / Sigstore signer.

---

## Pre-v1.0.0

Prior work is documented in the plan files:

- **Plan 001** — Original architecture and peer-review history.
- **Plan 003** — Projection removal and web frontend (Phase 8a complete).
- **Plan 004** — MCP→CLI flattening (Phases 9a–9d complete).
- **Plan 007** — Lifecycle enforcement spine (Pieces 0–2 complete).
- **Plan 008** — Work-log coordination kernel (this release).

Rollback instructions for v1.0.0: restore the pre-migration database backup and check out the previous tag. The `breadcrumbs` table is left intact by the migration script as a safety net.
