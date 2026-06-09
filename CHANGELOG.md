# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.0.0] — 2026-06-09

### Added

- **Work-log coordination kernel (Plan 008):** op-CRDT operation log (`op_log`), content-addressed blobs (`content_blobs`), deterministic fold into `work_items` cache, fail-safe status lattice (open > claimed > closed > deferred), and standalone verifier CLI.
- **Cross-project coordination (P3):** `request`/`wait` ops, project registry (`log_location`, `wake_channel`), derived cross-project index, export/ingest JSONL interchange, reverse-edge map, and `agent-notes-trigger-loop` daemon for wake routing.
- **Lease and claim system (P4):** `work_item_leases` table, `claim`/`release`/`heartbeat`/`requeue-expired` CLI, `work_items_claimable_v` view excluding leased items.
- **Tier A degrade contract:** `coordinator-absent / local-lease` is the default safe mode; `doctor` reports coordination mode. The regista coordinator is an optional, not-yet-attached L3 layer.
- **Migration script:** `migrate_breadcrumbs_to_work_items.py` — idempotent one-shot migration from `breadcrumbs` table to `work_items` with full status mapping, content-addressed blobs, and `close`/`set_status` ops for terminal items.
- **Opencode enforcement plugin (Plan 007 Piece 2):** `integrations/opencode/index.js` injects orientation into system prompt and reconciliation checklist before compaction.
- **Lifecycle enforcement spine (Plan 007):** `agent-notes init` + `orient` + Claude Code SessionStart hook + opencode plugin.

### Changed

- **CLI is the primary sync surface** (Plan 004): MCP servers removed in favor of `agent-notes` CLI + skills + NOTIFY bridge.
- **Skills updated** to use `work-item` commands and `wi_*` vocabulary (Plan 008 Tier A surface update).
- **Removed dead `query` subcommands** from both `breadcrumb` and `work-item` CLIs — `find` is the superset (BC-018).
- **Pool shutdown:** `_reset_pool` now calls `pool.close()` before dropping the singleton; added public `close_pool()` API (BC-019).

### Fixed

- BC-022: breadcrumb get no longer returns `?` for missing title/status fields.
- BC-025: reflection filename collisions handled with `-2`, `-3` suffixes; DB uses `supersedes` chain.
- BC-026: `--json` output is clean JSON; human-readable messages go to stderr.
- BC-003: bulk import available via `agent-notes import`.

### Deferred (Tier B — not required for v1.0.0)

- Regista coordinator integration (L3 atomic distributed claims).
- `requeue_expired` as managed daemon timer.
- Causal-stability watermark + archival truncation.
- Keyless/Sigstore signer.
