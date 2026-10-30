# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- **Memory tiers (Plan 025 Phase 2):** memories now live at `repo`, `vm`, or
  `global` scope. `memory add|get|update|delete --scope repo|vm|global [--vm ID]`;
  `memory list|search|recall --scope ...|composed` (`search` and the new `recall`
  default to `composed`: one similarity query over repo, vm, and global, repo >
  vm > global on name collisions, hidden entries in `shadows`). `--scope vm|global`
  works outside a registered repo. VM ID: `--vm`, then `AGENT_NOTES_VM`
  (env or suite.env), then the short hostname; generated hostnames (pods,
  containers, `localhost`) are refused. `doctor` reports the resolved ID.
- **orient:** additive `memories_vm` / `memories_global` sections (cap 3 each,
  `--full` / `--limit` as for the other sections), `vm`, and `--vm`. Outside a
  registered repo, orient succeeds with just those sections (`project` is null).
- **Schema 850:** `projects.kind` (`repo`|`vm`|`global`). Run the migration
  before upgrading clients. The synthetic rows are excluded from every project
  listing (`workspace list`, web UI, export `projects`, project resolution by slug
  or path); `export` carries them separately under `memory_scopes`.

### Removed

- **Regista write-through (Plan 026 D1).** The native op-log is the only write
  path. Removed the `outbox`, `projection`, `migrate-to-regista`,
  `memory rebuild-from-regista` and `memory check-drift` commands. A hidden
  `agent-notes outbox ...` no-op briefly kept old per-repo Stop hooks from
  exiting 2; it was removed in Phase 3 (below).
  `AGENT_NOTES_REGISTA_WRITES` / `regista.writes_enabled` now makes every write
  fail with `RETIRED_SETTING`. `REGISTA_DSN`, `REGISTA_KEY_PATH` and
  `REGISTA_REQUIRE_SSL` are no longer read. `doctor --json` drops its `regista`
  block and never reports `degraded`; a new `retired_settings` check fails
  while the writes setting is on, and `legacy_outbox` fails while the old
  outbox directory still holds queued files (archive them).
- **Hindsight memory engine (Plan 026 D6).** Native is the only memory engine.
  The `memory-provider` command is gone, and `AGENT_NOTES_MEMORY_ENGINE` set to
  anything but `native` is an error rather than a silent fallback.
- **The `regista-hraedon` dependency (Plan 026 D3).** agent-notes no longer
  installs or imports regista. The principal grammar is vendored into
  `core/actor.py` and the error-envelope codes into `core/errors.py`
  (`ErrorCode`, `AgentNotesError`); `--json` error envelopes are byte-identical
  to 1.1.1's, pinned per code by `tests/test_error_envelope.py`. Removed with it:
  - `SUITE.lock`, `scripts/dev-install.py`, `scripts/suite_lock.py`,
    `scripts/check_suite_lock.py`, the `DEV_AGAINST` / `check-suite-lock`
    Makefile targets and `docs/develop-against-lock.md`. `make dev` is a plain
    `uv pip install -e ".[test]"`; CI installs the same way.
  - **Action delegation.** `AGENT_NOTES_ACTION_DELEGATION_PATHS` is ignored; no
    host set it, and nothing used the credentials once regista writes were gone.
  - **Secret-backend DSN refs.** A DSN of the form `env:`, `file:`, `literal:`,
    `vault:` or `azure:` now fails with `RETIRED_SETTING`; set a literal DSN.
    No host used a ref.
  - The structlog-to-stderr setup at CLI entry (only regista logged through it).
  - Not removed: the `REGISTA_PRINCIPAL_ID` and `REGISTA_PRODUCER_*` names.
- **The hidden `agent-notes outbox ...` no-op.** All 51 per-repo Claude Stop
  hooks that called it were cleared on 2026-10-29, so `outbox` is an unknown
  subcommand again (exit 2). `agent-notes init <repo>` still removes the old
  hook if a copy resurfaces, and `doctor`'s `legacy_outbox` check stays.

### Changed

- `PyYAML` is now a declared dependency; `core/bc_files.py` imported it but it
  had arrived only transitively (regista, `huggingface_hub`).

### Fixed

- **`work-item claim` no longer strands the item (WI-086).** A claim records the
  lease and leaves the status alone, so `update --status in_progress` and then
  `in_review` work after a claim without `--force`. Items in `open` or
  `in_progress` can be claimed. `claimable` now excludes items under a live
  lease (it had been an alias for `ready`), and an expired lease makes an item
  claimable again. Rows the old claim left in `claimed` leave it as `open`
  would (`start`, `defer`, close), and `release` still returns them to `open`.
- **Async `review request --work-item` can be applied with `--attempt` (WI-087).**
  The dispatcher journalled every review job as a freeform brief, so `work-item
  review pass|accept --attempt` always refused it as "for a different work
  item". It now binds the attempt to the real work item and its freshness
  watermark. An item that is missing, refiled or not `in_review` fails the job
  instead of falling back to a brief. Attempts already journalled as briefs stay
  unusable; request a fresh review.
- **orient text collapses newlines in stored titles and names.** Hooks inject
  this text (on agy as a system message), so a newline in a work-item title or
  memory name could forge orient lines. The OpenCode plugin does the same.

### Added (Plan 027 Phase 2)

- **`memory hygiene --notify`** posts one "memory curation: N items" notice per
  project with findings, and skips an unchanged report. `--dry-run` previews it.
  The new `curate-memory` skill covers acting on a notice. Systemd units for the
  nightly run are in `deploy/`.
- **orient lists live notices** (Plan 025 D4): id and age in the text, plus the
  subject in `--json`. Read them with `agent-notes msg inbox`.

## [1.1.1] - 2026-10-17

### Fixed

- **OpenCode 2 installer:** `agent-notes install-harness opencode` now installs
  the V2 plugin package to `~/.config/opencode/agent-notes` and registers it in
  `opencode.json` under `plugins` as `./agent-notes`. Existing V1 `plugin`
  entries (`integrations/opencode/index.js`) are removed during install, and
  uninstall removes the copied package. The plugin package is force-included in
  the wheel so artifact-only hosts can install it without a source checkout.

## [1.1.0] - 2026-08-22

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
