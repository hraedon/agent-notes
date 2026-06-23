# Plan 009 — Convergence on regista: P1 (write-through) + P2 (outbox)

**Status:** Proposed 2026-06-23. Implementation plan spawned by dossier Plan 006
("Convergence on regista"). P1 + P2 in scope for this pass; P3 (projection +
enforcement hooks) and P4 (note entity, blocked on regista Plan 022) are later.
**Author:** glm-5.2

This plan implements dossier-006 §8 P1 and P2 **in agent-notes**, making
agent-notes a *face* of regista (the agent/CLI face) instead of its own
work-item authority.

## 0. Decisions surfaced to a human

dossier `AGENTS.md:62-68` requires surfacing "the workflow definition" and
"anything touching signing/provenance configuration." The strategic direction is
decided by dossier-006; the following are the implementation-level calls, made
here deliberately and reversibly:

- **D1 — Project topology (own project, not shared with dossier yet).** agent-notes
  writes through to its **own** regista project (schema) for P1/P2, configured via
  env (`AGENT_NOTES_REGISTA_PROJECT`, default `agent_notes`). The dossier-006 §2
  end-state is "one work-item universe" shared with dossier; reaching that
  requires sharing one HMAC keyset and unifying workflows, which is a later,
  deliberate convergence step. Own-project first is the safe increment dossier-006
  §8 P1 calls for ("write-through only, fail-fast — proves the model on the happy
  path"). The `RegistaFace` (§2) takes the project name from config, so switching
  to a shared project later is a config change, not a code change.
- **D2 — Signing.** regista signs all work-item events with the HMAC keyset at
  `AGENT_NOTES_REGISTA_HMAC_KEY_PATH` (or `REGISTA_HMAC_KEY_<KEY_ID>` env
  injection). agent-notes' existing DSSE/Ed25519 op_log signatures are **not**
  translated** (they cannot be — different canonicalization, different scheme);
  the migration re-creates work-items under regista's key and the old op_log stays
  read-only (dossier-006 §9: "old store kept read-only until trust is
  established"). The **outbox** (P2) is signed by agent-notes' own
  `LocalKeySigner` (Ed25519, already in `core/envelope.py`) — this is an integrity
  stamp on the staging area (AC-2), distinct from regista's chain.
- **D3 — Actor binding.** agent-notes has no auth (decision 43). The agent face
  constructs an `Actor(actor_kind="agent")` whose `actor_id` comes from
  `AGENT_NOTES_ACTOR_ID` (default: a stable per-host agent id) and whose
  `on_behalf_of` carries the human principal from `AGENT_NOTES_PRINCIPAL_ID` /
  `_DISPLAY_NAME` (default: the git `user.name`/`user.email` of the repo). This is
  the agent-notes analog of dossier's session-resolved Actor; it is trust-rooted
  in environment, never in prompt input.
- **D4 — Local store becomes a projection.** regista is the authority for
  lifecycle + events. agent-notes' `work_items`/`memories` tables become a
  **search/read projection** (dossier-006 §5: "pgvector is a search projection,
  rebuildable from regista-sourced content"). The write-through updates the local
  projection on success so existing reads/search keep working without a full
  rebuild this pass. The op_log stops receiving writes (retired; §4).
- **D5 — Actor on the regista write path.** `WorkItemModel` regista branches use
  `face_factory.default_actor()` (env-resolved). The legacy `actor_id` parameter is
  ignored on that branch because agent-notes has no auth and the env-resolved actor
  is the trust root (D3).

These are flagged, not blocking. They are reversible (config-driven) and match
dossier-006's stated phasing.

## 1. Target (P1 — write-through, fail-fast)

```
agent-notes CLI ──► RegistaFace (sole choke point, Actor-injected)
                       │
                       ├──► regista (Postgres)   [AUTHORITY: lifecycle + signed events]
                       │
                       └──► local work_items row [PROJECTION: search/embedding]
```

- Every write goes through `RegistaFace`, which calls regista (authority) then
  mirrors the result into the local `work_items` projection.
- The op_log is **retired**: `WorkItemModel` write methods no longer call
  `kernel.commit_op`. The fold/verifier remain as **read-only historical** tools
  (pre-migration entities only); they are not deleted (dossier-006 §9 trust window).
- No outbox yet: if regista is unreachable, the write **fails fast** (raises). P2
  removes that failure surface.

## 2. The `breadcrumb` workflow

Registered in agent-notes' regista project. States mirror the agent-notes status
lattice (`core/kernel.py:197`): `open` > `claimed` > `deferred` > `closed`
(fail-safe: open dominates). One `work_item_type`, `breadcrumb`, with custom
fields. regista custom-field types include `json` (`_workflow.py:43`), so the
free-form metadata columns fit directly. Self-transitions (`from == to`) are
allowed (no schema/semantic rejection), so field amendments use `amend`.

States: `open` (initial), `claimed`, `deferred`, `closed` (terminal).

Transitions (one per source state — regista `from` is a single string):
- `claim`: open → claimed
- `release`: claimed → open
- `close_open`: open → closed  | `close_claimed`: claimed → closed | `close_deferred`: deferred → closed
- `defer_open`: open → deferred | `defer_claimed`: claimed → deferred
- `reopen`: closed → open | `undefer`: deferred → open
- `amend_open`: open → open | `amend_claimed`: claimed → claimed | `amend_deferred`: deferred → deferred
  (field-only updates via the `custom_fields` param on `transition()`)

Custom fields on `breadcrumb`:
- `title` (string, required), `description` (string), `severity` (enum:
  low/medium/high/urgent, default medium), `kind` (enum: the `wi_kind` vocab —
  todo/observation/decision/risk/task/bug/feature/improvement/question/experiment/spike/refactor/docs/ci/job),
- `external_refs` (json), `diagnostic_keys` (json),
- `source_identifier` (string) — the legacy `WI-NNN`, for migration traceability.

Status map (agent-notes → regista state): open→open, claimed→claimed,
closed→closed, deferred→deferred (1:1; no remapping needed since we keep the
lattice names).

## 3. `RegistaFace` — the sole choke point

New module `src/agent_notes/core/regista_face.py`. Mirrors dossier's
`RegistaGateway` (`dossier/src/dossier/gateway.py:27`): every method takes a
server-resolved `Actor` and cracks it open internally — **no method accepts an
`actor_id` string** (G1 attribution made structural). Duck-types regista so
`InMemoryRegista` works in fast tests.

Public surface:
- `RegistaFace(regista)` — wraps a `Regista`/`InMemoryRegista`, calls
  `register_workflow()` with the packaged `breadcrumb` YAML.
- `from_config()` classmethod — builds `Regista(dsn, project, hmac_key_path)` from
  env (D1/D2/D3).
- `file_breadcrumb(actor, *, kind, title, description, severity, external_refs, diagnostic_keys, source_identifier=None) -> (work_item_id, state)`
- `amend_breadcrumb(actor, work_item_id, **field_changes) -> state`
- `transition_breadcrumb(actor, work_item_id, transition_name, payload=None, custom_fields=None) -> state`
- `comment(actor, work_item_id, body)` — `append_event(transition="comment")`
- `get(work_item_id)`, `list(current_states=..., page_size=...)`, `history(work_item_id)`
- `close()` / `replay()` (delegates)

Actor: `core/actor.py` — frozen dataclass `Actor(actor_id, actor_kind,
display_name, on_behalf_of=None)` + `resolve_actor()` reading env (D3).

## 4. Write-path switch + op_log retirement

`core/work_item_model.py` write methods (`file_work_item`, `update_work_item`,
`set_status`, `close_work_item`, `claim_work_item`, `release_work_item`,
`heartbeat_work_item`, `delete_work_item`) are reworked:
- They resolve a `RegistaFace` (lazy singleton from config).
- They translate the call to the face (create/amend/transition), then **mirror the
  regista result into the local `work_items` projection** (title/description from
  regista custom_fields; status ← regista state; keep `embedding` by re-embedding
  the new title+description; write `change_log` for NOTIFY/bridge compatibility).
- They **do not** call `kernel.commit_op` / fold / `op_log_events` for new writes.
- `claim/release/heartbeat` move to regista claims (`acquire_claim` /
  `release_claim` / `heartbeat_claim`) for the authoritative lease; the local
  `work_item_leases` table becomes a projection mirror (kept so existing
  `ready`/`claimable` views work this pass).

A feature gate (`AGENT_NOTES_REGISTA_WRITES=1`, default off until migration is
run) controls whether the write path goes to regista or the legacy op_log. This
lets the change land behind a flag and flip on per-project after migration. When
the flag is off, behavior is unchanged (legacy op_log). The flag and the
migration together are the controlled "retire" — deletion of legacy write code is
a later cleanup once trust is established.

Read methods (`get_work_item`, `query_work_items`, `find_work_items`, `ready_*`,
`claimable_*`, search) are unchanged this pass — they read the local projection,
which the write-through keeps current.

## 5. Migration (breadcrumb → regista work-item)

New `agent-notes migrate-to-regista [--project <slug>] [--apply]` (dry-run default).
- Reads each local `work_items` row (the folded state — authoritative for legacy
  data) + its body from `content_blobs`.
- Creates a regista `breadcrumb` work-item via the face (actor = a `migration`
  system actor), then replays the lifecycle: if the source status is
  `closed`/`deferred`/`claimed`, applies the corresponding transition(s) so the
  regista state matches.
- Records the mapping in a new `regista_work_item_map(entity_id, identifier,
  regista_work_item_id UUID, migrated_at)` table.
- Idempotent: if a mapping row exists, skip (or verify state matches and warn on
  drift). One-way: regista is the new authority; the local row is left as the
  read-only historical + search projection.
- `--apply` runs against a real regista project; dry-run reports counts + sample
  mappings without writing.

## 6. P2 — the outbox (AC-1/2/3)

### 6.1 Location (decided — dossier-006 §4.2)
Centralized, not per-repo:
`$XDG_STATE_HOME/regista/outbox/<project>/<session>.jsonl` (default
`~/.local/state/regista/outbox/`), env-overridable (`AGENT_NOTES_OUTBOX_DIR`) for
tests. Lives outside git by construction (D2 / the gitignore-risk elimination).

### 6.2 Op shape + signing (AC-2)
Each line is a JSON object: a DSSE envelope (`core/envelope.make_envelope`,
`payload_type="agent-notes-v1/outbox-op"`) over:
```json
{"op": "create"|"amend"|"transition"|"comment",
 "work_item_id": "<uuid or null for create>",
 "args": { ...the regista call args, including actor resolved at enqueue time... },
 "client_seq": <monotonic int per session file>}
```
Signed by `LocalKeySigner` (Ed25519). Reconcile verifies every signature; a
hand-edited or unsigned line is **rejected loudly** (logged + moved to a
`.rejected` sidecar, reconcile aborts that op) — it cannot become truth. AC-2.

### 6.3 Write command never surfaces failure (AC-1)
`RegistaFace` write methods gain an internal try/except around the regista call:
- regista reachable → write through (live event + live provenance), mirror
  projection, return success.
- regista unreachable (`OperationalError` / connection refused / timeout) →
  append the signed op to the session outbox file, return success. **The agent
  never sees "regista unreachable."** The projection row is marked
  `pending_sync=True` (new column) so `orient`/reads can show "STALE — N ops
  pending sync" honestly (dossier-006 §3).

`_is_regista_reachable()` is a cheap ping (e.g. a tiny `SELECT 1`-equivalent on
the regista pool, short timeout). Reachability is checked once per write, not
once per session — a mid-run DB kill is caught (AC-1 test).

### 6.4 Reconcile (AC-3)
`agent-notes outbox reconcile [--project <slug>]`: walks the centralized
`outbox/` tree, for each `<project>` replays its pending ops in `client_seq`
order into regista via the face:
1. Verify signature (AC-2); reject hand-edits loudly.
2. For `create`: re-create; store mapping.
3. For `amend`/`transition`: load the regista work-item; if its current state is
   **not** what the offline op assumed (the item moved while offline — detected by
   comparing the op's recorded `expected_state` against regista's live state, or
   via regista's `expected_event_seq` optimistic-concurrency param on
   `transition()`), the op **blocks for human resolution** — it is written to a
   `.conflicts` sidecar with full context and the reconcile reports it; it does
   **not** auto-merge and does not silently accumulate. AC-3.
4. On success, remove the line from the outbox (rewrite the file without it) and
   clear the projection `pending_sync` flag.

Conflicts are surfaced in `agent-notes orient` (a "N conflicts pending
resolution" section) and in `agent-notes doctor`.

### 6.5 Gating (AC-3)
While a project has a non-empty outbox, the face **rejects** key terminal
transitions (`close_*`, `reopen`-as-done) with a clear error directing to
`outbox reconcile` — an actor cannot mark work done with ops still pending sync.
Non-terminal ops (amend, comment) are allowed (they queue behind the pending
ones).

### 6.6 Acceptance tests (the gate)
- **AC-1**: point the face at a regista project, then force-unreachable
  (InMemoryRegista: inject a `raise OperationalError`; Postgres e2e: drop the
  schema mid-run). A `file_breadcrumb` returns success; the op is in the outbox
  file with a valid signature; the projection row is `pending_sync`.
- **AC-2**: hand-edit a line in the outbox (flip a byte in the payload);
  `outbox reconcile` rejects it loudly (non-zero exit, `.rejected` sidecar, no
  regista write). A line with no/invalid signature is likewise rejected.
- **AC-3**: enqueue an offline `close` op; concurrently transition the same
  regista item to `closed` by another path; `outbox reconcile` surfaces a
  blocking conflict (`.conflicts` sidecar, non-zero exit), does not overwrite.
  Plus: with a non-empty outbox, a terminal transition is rejected by the face.

## 7. Config + dependency

- `pyproject.toml`: add `regista>=0.4.0` to dependencies (sibling lib,
  `/projects/regista`; the test extra already pulls testcontainers).
- Env vars (all optional; when `AGENT_NOTES_REGISTA_DSN` is unset, regista writes
  are disabled and the legacy op_log path is used unchanged):
  `AGENT_NOTES_REGISTA_DSN`, `AGENT_NOTES_REGISTA_PROJECT` (default
  `agent_notes`), `AGENT_NOTES_REGISTA_HMAC_KEY_PATH`, `AGENT_NOTES_REGISTA_WRITES`
  (0/1), `AGENT_NOTES_ACTOR_ID`, `AGENT_NOTES_PRINCIPAL_ID`,
  `AGENT_NOTES_PRINCIPAL_DISPLAY_NAME`, `AGENT_NOTES_OUTBOX_DIR`.
- `agent-notes doctor`: report regista face health (reachable? project? pending
  outbox ops? conflicts?).

## 8. Test plan

- Fast tier (InMemoryRegista, no Postgres): `RegistaFace` round-trip (file →
  amend → claim → close → reopen), actor-binding (no actor_id string path),
  workflow registration, migration dry-run against an in-memory face.
- Postgres tier (`postgres`-marked, unique project per run, `drop_project_schema`
  cleanup — mirror dossier `test_e2e_postgres.py`): write-through creates a real
  signed regista event; migration `--apply` round-trip; the three AC tests
  (AC-1/2/3) against a real regista project where the "unreachable" case is
  forced by dropping the schema / closing the pool.
- Regression: existing 324 tests stay green when `AGENT_NOTES_REGISTA_WRITES` is
  unset (legacy path unchanged). New behavior is entirely behind the flag.

## 9. Sequencing (landable increments)

1. **Foundation**: workflow YAML + `core/actor.py` + `core/regista_face.py` +
   config/env + regista dep + `doctor` wiring. (fast-tier tests)
2. **Write-path switch** behind `AGENT_NOTES_REGISTA_WRITES` + projection mirror
   + migration (`migrate-to-regista`). (fast + postgres tests)
3. **Outbox** (signed ops, never-fail write, `pending_sync`) + **reconcile**
   (verify, conflict-block, gating) + **AC-1/2/3 tests**.
4. Cross-lineage adversarial review; apply findings; full suite green.

## 10. Non-goals (this pass)

- Deleting the op_log/fold/verifier code (read-only historical; later cleanup).
- Shared project with dossier (D1; later convergence).
- Memories → regista note entity (dossier-006 P4; blocked on regista Plan 022).
- Generated md projection + SessionStart/Stop/PreCompact harness hooks
  (dossier-006 P3).
- Re-pointing all reads/search at regista (projection stays local this pass).

## 11. Review outcomes + recorded deviations (2026-06-23)

Cross-lineage adversarial review run: nemotron-3-ultra on the foundation (glm) +
P1 (kimi); kimi on P2 (glm). Findings applied (suite 324→381, lint clean):

- **Applied:** `file_work_item` now honors a non-open `status` on the regista
  branch (create→transition); `amend_breadcrumb` reads regista's live state
  instead of trusting the caller's (possibly stale) `current_state`; migration is
  idempotent via `source_identifier` (reuses an existing regista work-item on
  re-run instead of duplicating); outbox sets `pending_sync=True` on enqueue for
  known work-items (AC-1 honest-staleness); redundant `closed_at` writes removed
  from `projection.py` (the `wi_status_changed_fn` trigger owns it); added a
  negative test that a regista **business** error surfaces (is NOT swallowed into
  the outbox — AC-1 guardrail).
- **Recorded as deliberate P1 deviations (not bugs; follow-ups):**
  - **D6 — claims/heartbeat use workflow transitions, not regista's claims API.**
    P1 `claim`/`release` move the breadcrumb lifecycle state (signed) and mirror a
    local `work_item_leases` row for TTL; heartbeat touches the local lease only.
    regista's `acquire_claim`/`heartbeat_claim`/`release_claim` (TTL, attempt,
    auto-steal) are NOT wired. Rationale: dossier's MVP also defers regista claims
    (`docs/provenance-model.md:20`); for single/few-writer agent-notes the
    workflow transition is the authoritative ownership event. Follow-up: wire
    regista claims when multi-writer contention is real.
  - **D7 — delete is local-projection-only.** regista events are immutable, so
    `delete_work_item` on the regista branch removes the local projection row and
    records `regista_retained: True` in change_log; the work-item remains in
    regista (queryable via `face.get`/`face.list`). A future `deleted` terminal
    state or tombstone event is the clean fix if hard-delete semantics are needed.
  - **D8 — migration reproduces the final state, not full lifecycle history.** A
    snapshot migration (create from `open`, then one transition to the target
    status); intermediate lifecycle (e.g. open→claimed→deferred) is not replayed.
    This is a fidelity limit already flagged in dossier-006 §9; the old store
    stays read-only for full history.
  - **D9 — `actor_id` param retained on legacy signatures.** Kept for API
    compatibility; **ignored on the regista branch**, which uses
    `face_factory.default_actor()` (env-resolved). G1 holds: no caller-supplied
    actor_id reaches regista. A future API split (private `_legacy_*`) can remove
    the footgun once the legacy path is deleted.
- **Accepted tradeoff (not fixing this pass):** the regista commit and the local
  projection mirror are separate transactions, so a crash between them leaves the
  projection stale relative to regista (regista is authority; projection is
  rebuildable). D4. A `rebuild_projection_from_regista` recovery command is the
  follow-up if this bites in practice.

## 12. P3 — projection + enforcement hooks (dossier-006 §8.3, §3, §6)

P1/P2 made regista authoritative and the write path offline-tolerant. P3 makes
**staleness honest and drift non-optional**: the agent always sees sync state,
and the harness reconciles on lifecycle boundaries. (Shipped 2026-06-23.)

### 12.1 Staleness surfacing (§3, §6.3-6.4)
- `orient` gains a **regista_sync** section: pending outbox ops
  (`outbox.count_ops`), unresolved conflicts/rejected sidecars, and
  `pending_sync` projection rows. Cheap and DB-only (no embedding load); the
  human STALE line reports the components (pending / conflicts / rejected /
  stale-rows) rather than summing heterogeneous counts. Surfaced in `--json`
  (for hooks) and human output.
- `doctor._check_regista_face` reports regista face health (enabled? project?)
  + outbox counts; wrapped to **warn**, never fail the whole doctor.

### 12.2 Projection rebuild (closes the §11 accepted-tradeoff gap)
- `agent-notes projection rebuild-from-regista [--project <slug>]`: pages
  through every regista `breadcrumb` work-item (the face `list()` now paginates
  via cursor) and re-mirrors the local projection (match by
  `regista_work_item_id` / `source_identifier`), clearing `pending_sync`. The
  recovery path when a crash left the projection stale. Idempotent; per-item
  commit so a mid-rebuild failure is resumable.

### 12.3 Generated md view with staleness banner (§3)
- `breadcrumb export-index` prepends a prominent `⚠ STALE — N ops pending sync`
  banner when the outbox is non-empty. md stays a generated view, not a record.

### 12.4 Enforcement hooks (§6) — non-optionality
- **SessionStart** → `orient` (already wired on Claude + opencode); with §12.1
  it surfaces sync state every session.
- **Stop / PreCompact** → reconcile; must accept "no change"; if regista is
  unreachable, leave the outbox and surface "N ops pending sync."
  - opencode `experimental.session.compacting`: runs `agent-notes outbox
    reconcile` + `outbox status` and injects the real sync block (parses the
    report regardless of exit code — conflicts exit non-zero but still print
    JSON; independent try/catch so a reconcile timeout doesn't hide the status).
  - Claude Code `init` now also wires a `Stop` hook → `agent-notes outbox
    reconcile` (suppresses verbose stdout, lets stderr through, `|| true` so
    session-end never blocks).

### 12.5 P3 review outcomes (2026-06-23)
Cross-lineage review (nemotron on kimi+glm P3 work). Applied: `RegistaFace.list`
cursor pagination (was one page → missed items >100, broke rebuild); doctor
warn-not-fail; orient component-wise STALE line; opencode JS null-guard +
status-agnostic report parsing + independent try/catch; Stop hook stderr
surfacing.

Not bugs (verified): reconcile ordering (apply→remove→clear) is the **safe**
order — the op leaves the outbox only after regista accepted it, so a
`pending_sync` clear failure is a recoverable cosmetic flag (the reviewer's
proposed clear-then-remove swap would risk duplicate creates); outbox keyed by
`cfg.project` is correct under D1.

Follow-ups (non-blocking): the optional `TestE2EPostgres` skips because regista
migrations target pg15 while the agent-notes testcontainer is pg17 — point that
test at regista's pg15 test DSN (as dossier's e2e does); add `export-index`
git-`check-ignore` confirmation per dossier-006 §4.2.


