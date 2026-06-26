# Plan 010 — Canonical lifecycle convergence (dossier Plan 007 spawn)

**Status:** Proposed 2026-06-26. The agent-notes implementation plan for dossier
Plan 007 WI-1/WI-2. **Blocked on regista** (shared validators — see §3) and on
dossier-007 §6.1 (deferred mapping). Not started.
**Author:** glm-5.2
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
| close | → `closed` | `submit_for_review` → `in_review` | **§6.2**: agent work awaits cross-lineage pass + human accept (via dossier) |
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

## 5. Decisions to surface to a human

1. **§6.1 deferred mapping** (from dossier-007): breadcrumb `deferred` → canonical
   `blocked`, a custom field, or a new canonical state? This must be resolved
   before WI-3.
2. **Projection vocabulary** (WI-3): migrate the local projection to canonical
   states, or keep a breadcrumb display vocabulary mapped from canonical? The
   latter minimizes CLI/test churn but adds a mapping layer.
3. **Shared project (dossier-007 §6.3):** this plan keeps agent-notes in its own
   regista project (D1) for safety; full one-universe convergence (shared project
   + dossier board queries across both) is the subsequent step.

## 6. Sequencing

Blocked on: regista built-in validators (§3A, teammate) + dossier-007 §6.1
decision. Then: WI-1 + WI-2 (shape) → WI-3 (verbs/projection) → WI-4 (migration)
→ WI-5 (tests). Land behind the existing `AGENT_NOTES_REGISTA_WRITES` flag; flip
per-project after migration + `replay()==0`.

## 7. Risks

- **Projection vocabulary churn** — the breadcrumb lattice is pervasive; a
  careless swap breaks orient/queries/tests. WI-3 is the careful part.
- **`close`→`submit_for_review` semantics** — agent workflows that auto-close now
  leave items in `in_review` pending a human; agent-notes must surface "awaiting
  review" honestly (orient/STALE machinery already exists).
- **Validator drift** if §3A is skipped — do not vendor (§3B).
- **Migration fidelity** breadcrumb→canonical across non-aligning lattices; old
  store read-only until trust established.
