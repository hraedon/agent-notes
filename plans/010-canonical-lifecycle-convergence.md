# Plan 010 — Canonical lifecycle convergence (dossier Plan 007 spawn)

**Status:** Proposed 2026-06-26; **decisions resolved + readied 2026-06-28.**
**Implemented 2026-06-28** (WI-1 through WI-5, behind `AGENT_NOTES_REGISTA_WRITES`).
The agent-notes implementation plan for dossier Plan 007 WI-1/WI-2. **Gated on
regista Plan 023** (built-in dual-mode validators — see §3) **and dossier Plan
008** (the `deferred` state); both prerequisites shipped before this work. **All
five work items landed; 394 tests green; lint clean.**
**Author:** glm-5.2; readied by Opus 4.8 (2026-06-28); implemented by glm-5.2
(2026-06-28)

## 0a. Decisions resolved 2026-06-28 (were §5 open questions)

- **Deferred mapping (§5.1):** breadcrumb `deferred` → a **new first-class
  canonical `deferred` state**, specified in **dossier Plan 008** (not `blocked`,
  not a custom field). The state must exist before WI-3's verb remap.
- **Gate is dual-mode (regista Plan 023):** `done` always requires a cross-lineage
  adversarial review pass (Invariant G), but the *final accept* is policy-gated —
  **strict** (human accept) vs **relaxed** (any actor, incl. the author, after an
  independent cross-lineage pass). **Homelab default = relaxed**, so an agent can
  file → another-lineage actor reviews → close, with no per-item human bottleneck,
  while the mixed-chain integrity guarantee still holds. agent-notes registers the
  canonical workflow with `relaxed`; a workplace deployment would register `strict`.
- **Validators source (§3):** option A confirmed — the validators are **regista
  built-ins** (Plan 023), registered by name. agent-notes neither vendors nor omits
  them.
**Strategic role:** Swap agent-notes from the breadcrumb lifecycle onto the
dossier v3 canonical lifecycle + review gate, demote lease to regista claims,
so dossier and agent-notes drive the **same** work-items through one lifecycle
(one mixed human+agent verified chain). This is the agent-notes half of
"ingest every other project's work-items and standardize."

## 0. Done this session (prerequisite, unblocked)

- **`model_lineage` on the agent-notes Actor** (`core/actor.py`), resolved from
  `AGENT_NOTES_MODEL_LINEAGE`, threaded into `actor_metadata()`. Required by the
  dossier `adversarial_review` cross-lineage rule (which fails closed on an
  undeclared agent lineage). Tests in `tests/test_actor_lineage.py`.

## 1. Why this is not a quick swap (the entanglement)

The breadcrumb lifecycle is load-bearing throughout agent-notes, not just in the
workflow YAML:

- `WorkItemModel._transition_for_status_change` and the entire status lattice
  (`open > claimed > deferred > closed`) are baked into `work_item_model.py`.
- The local **projection** (`work_items.status`, `work_item_leases`) and all
  queries/CLI/orient (`ready_*`, `claimable_*`) speak the breadcrumb vocabulary.
- **`claimed` IS the lease** (deviation D6): claim/release move lifecycle state.
  Demoting lease to regista claims (WI-2) removes `claimed` as a state, which
  ripples through the projection vocabulary and ~388 tests.
- The breadcrumb lattice and the canonical lifecycle **do not align cleanly**:
  breadcrumb `closed` is terminal; canonical `done` is reachable only through
  `in_review → in_human_review → accept`. Per dossier-007 §6.2 (decided: agent
  work must pass the gate), agent-notes' `close` maps to `submit_for_review`,
  not `done` — so agent-notes can no longer reach `done` unilaterally.

## 2. Target verb/state mapping (proposed)

| agent-notes verb | breadcrumb | canonical action | notes |
|---|---|---|---|
| file | → `open` | create (`open`) | direct |
| start work | (n/a) | `start` → `in_progress` | new explicit verb |
| close | → `closed` | `submit_for_review` → `in_review` | agent work awaits a cross-lineage review pass; final accept is **policy-gated** (relaxed = any actor incl. author may accept; strict = human accept). Homelab runs relaxed. See §0a + regista Plan 023 |
| amend | `amend_*` self-transition | `append_event(transition="amend")` | non-state event (like comment) |
| claim/release | `claim`/`release` (state) | **regista claims** (`acquire_claim`/`release_claim`) | WI-2; `claimed` is no longer a lifecycle state |
| heartbeat | local lease only | `heartbeat_claim` | WI-2; authoritative liveness in regista |
| defer | → `deferred` | **§6.1 decision** | blocked |
| reopen | `reopen` | `reopen` | direct (done → open) |

## 3. Blocker: where do the shared validators live? (regista)

The canonical workflow's review gate references two validators,
`adversarial_review` and `human_gate`, currently implemented in **dossier**
(`dossier/src/dossier/validators.py`) and registered at runtime. agent-notes
cannot import dossier (separate repo; would create a coupling that violates the
"thin face" boundary). Options:

- **(A) Promote them to regista built-in validators** (recommended). Both faces
  register/use the same regista-provided validators — zero duplication, zero
  drift, the gate is enforced identically wherever the transition runs. **This
  is regista work → the dispatched regista teammate.** Until this lands,
  agent-notes cannot register the canonical workflow with a trustworthy gate.
- (B) Vendor copies in agent-notes. Rejected: provenance-critical logic
  duplicated across two faces is exactly the drift risk the project avoids.
- (C) agent-notes registers the lifecycle WITHOUT validator annotations.
  Rejected: that lets agent-notes self-review (no gate) — a provenance hole.

**Sequencing:** this plan executes **after** regista ships the built-in
validators (A). The regista task: move dossier's `adversarial_review` +
`human_gate` into regista (with their tests), expose them as registerable
built-ins, and have dossier consume them from regista (deleting dossier's
copies) so there is one implementation.

## 4. Work items

- **WI-1 — Canonical workflow in agent-notes.** Vendor/register the dossier v3
  lifecycle workflow (states open/in_progress/blocked/in_review/in_human_review/
  done; two-stage gate) using the **regista built-in validators** (§3A). Keep
  the `breadcrumb` work_item_type + its custom fields; only the lifecycle changes.
- **WI-2 — Lease as regista claims.** Rework `claim_work_item`/`release_work_item`/
  `heartbeat_work_item` to `acquire_claim`/`release_claim`/`heartbeat_claim`;
  retire `claimed` as a lifecycle state; the projection derives "leased by X"
  from regista claims (the `work_item_leases` table becomes a projection mirror).
- **WI-3 — Verb remap + projection vocabulary.** Remap `_transition_for_status_change`
  and the face/model verbs per §2. Decide the projection's display status: keep a
  breadcrumb-derived display vocabulary mapped FROM canonical states (so CLI/orient
  stay readable), or migrate the projection to canonical states outright.
- **WI-4 — Migration.** breadcrumb-items → canonical workflow (one-way, idempotent
  via `source_identifier`); replay final state; `replay()==0` post-migration. Old
  breadcrumb workflow + op_log stay read-only.
- **WI-5 — Tests.** Lifecycle round-trip on the canonical workflow; lease via
  claims (no `claimed` state); agent `close`→`submit_for_review` (cannot reach
  `done` alone); cross-lineage pass + human accept (via the shared validators)
  reaches `done`; the mixed chain verifies.

## 5. Decisions

1. **~~§6.1 deferred mapping~~ — RESOLVED 2026-06-28** → new canonical `deferred`
   state (dossier Plan 008). See §0a.
2. **Projection vocabulary (WI-3) — RESOLVED 2026-06-28 (implemented):** migrate
   the local projection to **canonical states directly** (Option A), not a
   breadcrumb display remapping. Rationale: "one lifecycle" is this plan's
   purpose — a display remapping layer is exactly the drift the project avoids.
   The `state_to_status` map is identity for canonical states; legacy breadcrumb
   states (`claimed`/`closed`) remain valid in the projection column for
   pre-migration items (the schema CHECK constraint accepts both vocabularies;
   `820_canonical_lifecycle.sql` is additive and backward-compatible). `closed`
   is accepted as a target alias for `done` in `_transition_for_status_change`
   for callers that pass the legacy vocabulary. The `wi_status` vocabulary marks
   `done` and `closed` as terminal (so `closed_at` fires on both); `deferred`
   was corrected to non-terminal (canonical semantics — it was mis-marked
   terminal in the breadcrumb vocabulary).
3. **Shared project (dossier-007 §6.3):** this plan keeps agent-notes in its own
   regista project (D1) for safety; full one-universe convergence (shared project
   + dossier board queries across both) is the subsequent step.

## 5b. Implementation record (2026-06-28)

- **WI-1** — `breadcrumb.workflow.yaml` v2: canonical states
  (open/in_progress/blocked/deferred/in_review/in_human_review/done), two-stage
  review gate (`adversarial_review` + `human_gate` with `require_human: false` —
  relaxed homelab default), `deferred` as first-class non-terminal idle state,
  `amend` self-transitions (one per non-terminal state) for field-only updates.
  `RegistaFace` gained `acquire_claim`/`heartbeat_claim`/`release_claim` (lease
  axis); `_amend_transition_for` returns the constant `"amend"` (regista resolves
  by `from` state). The `_RegistaLike` Protocol includes the claim methods.
- **WI-2** — `_claim_work_item_regista`/`_release_work_item_regista`/
  `_heartbeat_work_item_regista` call `face.acquire_claim`/`release_claim`/
  `heartbeat_claim`. The lifecycle does NOT move to `claimed` (it stays
  open/in_progress); the local `work_item_leases` row is a projection mirror of
  the authoritative regista claim. `OutboxAwareFace` delegates claims to the base
  face directly (claims are a liveness primitive, NOT buffered in the outbox —
  if regista is unreachable the claim fails honestly; there is no authority to
  claim against).
- **WI-3** — `_transition_for_status_change` remapped onto the canonical
  transition table (`_CANONICAL_TRANSITIONS`); `close`→`submit_for_review`
  (start first if `open`); the agent cannot reach `done` alone (Invariant G).
  Projection `state_to_status` accepts canonical + legacy states. Schema
  `820_canonical_lifecycle.sql`: expands the `work_items.status` CHECK constraint,
  seeds canonical `wi_status` vocabulary entries (is_terminal/is_open), corrects
  `deferred` to non-terminal, clears stale `closed_at` on non-terminal items.
  Outbox terminal-transition detection updated (`accept` + `close_` prefix +
  `reopen`).
- **WI-4** — `migrate_to_regista.py`: `claimed`→`start`(in_progress),
  `deferred`→`defer`, `closed`→`close_from_open`(done). The migration mirrors the
  canonical state back onto the local projection (status column). The review gate
  is NOT replayed (D8 snapshot; old store stays read-only for full history).
- **WI-5** — `test_regista_write_through.py` rewritten for canonical semantics:
  full review-gate round-trip (file→start→close→adversarial_pass→accept→done→
  reopen), `test_agent_close_cannot_reach_done_alone` (Invariant G), claims as
  regista claims (status unchanged), heartbeat via regista claim. Outbox AC tests
  updated for canonical transitions. `test_projection.py` canonical states.

## 6. Sequencing

Gated on: **regista Plan 023** (built-in dual-mode validators) + **dossier Plan
008** (the `deferred` state). Both are prerequisites, not open questions. Then:
WI-1 + WI-2 (shape) → WI-3 (verbs/projection) → WI-4 (migration) → WI-5 (tests).
Register the canonical workflow with the **relaxed** gate policy (homelab
default). Land behind the existing `AGENT_NOTES_REGISTA_WRITES` flag; flip
per-project after migration + `replay()==0`.

**Cleanup (post-conversion):** retire the Plan 008 op_log engine remnants
(`core/kernel.py`, `core/verifier.py`) once the breadcrumb workflow is read-only
and nothing imports them; prune the stale `worktree-agent-*` git worktrees and
merged feature branches.

## 7. Risks

- **Projection vocabulary churn** — the breadcrumb lattice is pervasive; a
  careless swap breaks orient/queries/tests. WI-3 is the careful part.
- **`close`→`submit_for_review` semantics** — agent workflows that auto-close now
  leave items in `in_review` pending a review pass; agent-notes must surface
  "awaiting review" honestly (orient/STALE machinery already exists). The
  **relaxed** gate (homelab default) keeps friction low — a second-lineage agent
  review suffices to close, no human bottleneck — but the item still cannot reach
  `done` without that independent pass (Invariant G).
- **Validator drift** if §3A is skipped — do not vendor (§3B).
- **Migration fidelity** breadcrumb→canonical across non-aligning lattices; old
  store read-only until trust established.
