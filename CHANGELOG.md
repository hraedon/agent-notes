# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [1.1.0] — 2026-08-22

### Changed

- **BREAKING (regista configuration):** the write-through face now accepts only
  the canonical `REGISTA_DSN`, `REGISTA_KEY_PATH`, `REGISTA_REQUIRE_SSL`, and
  `AGENT_NOTES_PROJECT` settings. The `config.json` `regista` block uses
  `key_path`; retired aliases and the old key name are ignored. The
  `AGENT_NOTES_REGISTA_WRITES` write-mode gate is unchanged.
- **BREAKING (v6 write path, WI-072):** authored writes now resolve a canonical
  principal from `AGENT_NOTES_ACTOR_ID` (falling back to
  `REGISTA_PRINCIPAL_ID`) and reject missing or malformed IDs before writing.
  Legacy proxy-principal fields, `AGENT_NOTES_MODEL_LINEAGE`, and per-command
  `--actor-id` / `--model-lineage` overrides are no longer accepted.
- **Signed producer provenance (v6, WI-072):** producer and model-lineage
  metadata is resolved by regista from `REGISTA_PRODUCER_*` process settings;
  review transitions validate that the ambient producer has a model and
  canonical lineage, then send only the review note in the payload; optional
  signed action-delegation evidence is loaded from configured credential files.
- **External provisioning (v6, WI-072):** workflow registration and epoch/genesis
  opening are no longer implicit side effects of constructing a face. Projects
  must be provisioned before ordinary writes; the consumer exposes the
  canonical workflow bytes for that provisioning step.

### Added

- **`agent-notes invariants probe` — the genesis-gate session-identity probe (WI-071).** The suite genesis gate (`agent_suite.genesis_gate`) requires a passing `agent_notes.session_identity_resolvable` check from agent-notes before an epoch may open. `agent-notes invariants probe --json` remains read-only and measures the ambient canonical actor configuration through `face_factory.write_actor`, with named failure reasons.

- **Canonical v6 operator skills:** `/start`, `/end`, filing, updating, and
  adversarial-review instructions now configure identity through the process
  environment rather than passing identity overrides on each command.

- **Codex lifecycle integration (Plan 019 / suite Plan 007):** `install-harness codex` installs the canonical skills at `$HOME/.agents/skills` plus hash-owned `SessionStart` orientation and `Stop` reconciliation groups in `$CODEX_HOME/hooks.json`; the bounded `plugins/agent-notes` component bundle carries drift-checked copies of the same skills and hook definitions without packaging the checkout or `.venv`. Hook output is bounded, metadata-only, fail-open, and never requests Stop continuation. Surgical uninstall preserves Cairn, user, duplicated, and locally modified groups. Doctor names plugin, direct, duplicate, stale, absent, and `/hooks` trust-unverified states.
- **Suite-shape `doctor --json` (Plan 017 WI-3.1):** `agent-notes doctor --json` (and `agent-notes-doctor --json`) now emit the suite contract shape — `{component, version, status, regista:{reachable, project, writes_enabled, chain_ok, mode}, checks:[…]}` — so a suite-doctor umbrella can aggregate agent-notes alongside the other components. `status` is three-state: `healthy` / `degraded` (spine absent — coordinator-absent is the default safe mode, non-failing) / `unhealthy`. A configured-but-unreachable regista is a failure; an unconfigured one is `degraded`. The human-readable `doctor` now runs the same suite-layer checks (chain integrity, skills installed, harness wired, regista reachable) so both surfaces agree.
- **`SUITE.lock` + suite install runbook (Plan 017 WI-2.2):** `SUITE.lock` records the regista git SHA + envelope/workflow versions this release is tested against; `deploy/SUITE-INSTALL.md` documents the pin procedure and the embedding-model pre-cache path (HF_HOME) for air-gapped installs.

### Security

- **`doctor` no longer leaks DSN/username into output:** all exception details in health checks are secret-safe (type name only, never `str(exc)`), so a connection failure cannot surface the DSN password or DB user in JSON/logs.

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
- BC-025: reflection filename collisions handled by skip-with-message + `ON CONFLICT DO NOTHING` (previously raised UniqueViolation).
- BC-026: `--json` output is clean JSON; human-readable messages go to stderr.
- BC-003: bulk import available via `agent-notes import`.
- `heartbeat_work_item` now writes a `heartbeat` op to `op_log` and emits an `item.heartbeat` event, matching every other mutation's provenance contract.
- `claim_work_item`'s `set_status` op is now a proper child of the `claim` op in the hash chain (was incorrectly chained to the entity root).
- `release_work_item`'s `set_status` op is now a proper child of the `release` op in the hash chain (same bug as claim).
- Lease interval SQL used `interval '%s seconds'` which bound the positional parameter index instead of the value — fixed to `make_interval(secs => %s)`.
- `_check_vocab_references` collapsed duplicate `bc_*`/`wi_*` branches into a single namespace-mapped code path.
- `verify --check-cache` added: compares `work_items` cache against op-log fold to detect cache drift.
- Web viewer: optional bearer-token auth via `AGENT_NOTES_WEB_TOKEN` env var (backward compatible — unset means open).

### Deferred (Tier B — not required for v1.0.0)

- Regista coordinator integration (L3 atomic distributed claims).
- `requeue_expired` as managed daemon timer.
- Causal-stability watermark + archival truncation.
- Keyless/Sigstore signer.
