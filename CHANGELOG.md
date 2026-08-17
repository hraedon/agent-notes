# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Changed

- **BREAKING (write path): work-item writes refuse an undeclared model lineage (WI-062).** An agent-kind event with no `model_lineage` can never clear regista's cross-lineage review gate (`derive_authors` sets `agent_author_undeclared` per event, and history cannot be cured), so `work-item file/update/close/review/claim/release/heartbeat/delete` and their `breadcrumb` aliases now fail closed with a named `UNDECLARED_LINEAGE` error instead of writing history that can never be reviewed. The same refusal applies on the native (degrade) op-log path — for write-path parity, so degrade mode is not a bypass (op-log ops are *not* replayed into regista as authored events; migration snapshot-migrates under the system migration actor, and outbox reconcile replays outbox envelopes — corrected in WI-068). Declare the model *family* with `--model-lineage`, or set `AGENT_NOTES_MODEL_LINEAGE` in the environment or `suite.env`. Note-shaped entities (memories, reflections) are unaffected — they are not read by the review gate.
- **Lineage-gate surface gaps closed (WI-068, findings from the WI-062 adversarial pass).** The *native* (degrade-mode) `claim`/`release`/`heartbeat`/`delete` verbs are now lineage-gated like their regista twins (they had been left ungated despite the WI-062 note above), and `delete` stamps the acting `actor_id` on its tombstone snapshot op and change_log row instead of committing them anonymous. `breadcrumb reconcile --apply` no longer relabels `UndeclaredLineageError` into a generic per-item error — the canonical `UNDECLARED_LINEAGE` envelope (exit 3) surfaces with its remedy intact. `--model-lineage` is now accepted on `work-item claim/release/heartbeat/attest-gate`, so the documented per-invocation fallback is no longer env-only there; the regista lease verbs honour the per-invocation `--model-lineage` declaration too (the claim identity stays the env-resolved actor — Plan 010 WI-2/WI-5). The cross-project verbs (`work-item request/wait/link-cross`) — the last ungated authored-write surface — are now gated the same way and commit their ops with the *resolved* actor instead of `actor_id=None`, and their CLI subcommands (plus `work-item delete` / `breadcrumb delete`) take `--actor-id`/`--model-lineage`.
- **Lease ops and link audit rows are attributed (WI-069, follow-on to WI-068).** The native (degrade-mode) `claim`/`release`/`heartbeat` op payloads and their change_log rows now record the gate-resolved `model_lineage` (the per-invocation declaration when given, the env declaration otherwise), so the op-chain can say *which* lineage held a lease after the fact — the regista-path claim already carries it in `actor_metadata`. `links.add_link`/`remove_link` change_log rows (`link_added`/`link_removed`) are no longer NULL-actored: they carry the caller-resolved actor (`link-cross` passes its lineage-gated actor; the plain `link add`/`link remove` CLI stamps the env-resolved default). Attribution only — gate/refusal behavior is unchanged.
- **Actor identity now honours the documented suite precedence (WI-062 follow-on).** `load_actor_config` loaded `suite.env` but consulted it for `principal_id` only, so `AGENT_NOTES_ACTOR_ID`, `AGENT_NOTES_MODEL_LINEAGE`, `AGENT_NOTES_PRINCIPAL_KIND`, and `AGENT_NOTES_PRINCIPAL_DISPLAY_NAME` were process-env only — setting them in the per-user overlay looked right and did nothing. All four now resolve `process env > per-user suite.env > system suite.env > default`, like every other suite fact.

### Added

- **`--actor-id` / `--model-lineage` on `breadcrumb reconcile`,** so `/start` and `/end` can declare a lineage for the status transitions they apply instead of depending on process env alone (WI-062).

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
