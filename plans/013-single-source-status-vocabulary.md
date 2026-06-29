# Plan 013 — Single-source status vocabulary (close the Plan-010 drift category)

**Status:** Implemented 2026-06-29 (WI-1 through WI-5 complete; adversarial-reviewed).
**Author:** glm-5.2
**Reviewed:** 2026-06-29 (opus-4.8) — approved with the refinements in §0. P0
lattice bug verified live in `kernel._STATUS_LATTICE` on current main.
**Strategic role:** Retire the entire class of "a validation/resolution surface
never caught up to the canonical lifecycle" bugs by making the work-item status
vocabulary and its properties authoritative in **one** place, with every
consumer referencing it instead of re-declaring it.

## 0. Reviewer addendum (refinements, not objections)

The diagnosis and architecture hold. Four refinements keep the success criteria
honest:

1. **"Single source" is really three cross-checked layers, not one.** Status
   truth still lives in regista's `canonical.workflow.yaml` (workflow
   authority), the DB `wi_status` vocab (runtime flags), and the new
   `lifecycle.py` (static props). They are reconciled by tests, not unified.
   So "adding a state is a one-line change / a missed surface is impossible"
   (§2) is overstated: adding a state is *three* coordinated edits (YAML + SQL
   seed + `lifecycle.py`), and "impossible to miss" means "a test fails if you
   miss one." Still a major reduction (N≈7 → 3 with enforced agreement) — just
   don't claim compile-time single-sourcing the design doesn't deliver.
2. **WI-4's guards are load-bearing and MUST run in default CI.** The entire
   "can't miss a surface" guarantee rests on `test_lifecycle_module_matches_db_vocab`
   and the transitions-vs-YAML cross-check actually running. If they are
   `ephemeral_db`-gated and CI skips DB-backed tests, the guarantee evaporates
   silently. Treat "these run in the default CI job" as an explicit AC of WI-4.
3. **The P0 is live on the now-primary states; don't let WI-3 trail WI-2.**
   Since the regista agent-SoT MVP went live, canonical states are the common
   case. The dangerous case is not "open dominates done" (that is the safe
   direction) — it is **canonical-vs-canonical ties resolving by `op_id`**: a
   concurrent `done` with a smaller `op_id` beats `in_progress` and *silently
   completes*, violating the fail-safe principle. WI-3 depends only on WI-1
   (`lifecycle.rank`), so it may land immediately after WI-1 — ahead of the
   mechanical WI-2 wiring if convenient.
4. **Point the §5 adversarial review at the cross-axis `claimed` comparison.**
   The within-lifecycle ordering is fine. The soft spot is that `claimed` is a
   *liveness/lease* axis, not a lifecycle state, yet it shares the 0–8 rank
   scale — so a concurrent `claimed`-vs-`in_review` resolves two different axes
   against each other. Low stakes (legacy-only, transitional) but it is the one
   place the rank table can surprise; scope the review there.

Also: this plan prevents *future* mis-resolution but contains no detection or
repair for items the live P0 may have *already* silently mis-resolved. Accepting
"no repair pass — incidence is low" is defensible, but record it as a decision
rather than leaving it unstated (see §8).

## 1. The problem (evidence)

Plan 010 (canonical lifecycle convergence) layered the canonical states
(`in_progress` / `blocked` / `in_review` / `in_human_review` / `done`) on top of
the legacy breadcrumb vocabulary (`open` / `claimed` / `closed` / `deferred`).
The lifecycle is defined once in regista's `canonical.workflow.yaml`, but the
**properties** of each status (validity, terminal/open flags, lattice rank,
legacy synonyms, valid transitions) are re-declared independently in **six**
Python surfaces plus the DB `wi_status` vocab table. Plan 010 updated some and
missed others. Three concrete bugs have already been found this session, all the
same root cause:

| Surface | File | Symptom | Status |
|---|---|---|---|
| CLI legacy map | `cli/breadcrumbs.py:_BC_STATUS_TO_WI` | `--status in_progress` → `claimed` (stuck item) | **Fixed** (commit d6dd95d) |
| File-import map | `core/bc_files.py:_map_bc_status_to_wi` | same | **Fixed** (d6dd95d) |
| Verifier policy | `core/verifier.py:_VALID_STATUS` | `agent-notes verify` flagged canonical `set_status` ops as violations | **Fixed** (cc20c84) |
| **Fold lattice** | `core/kernel.py:_STATUS_LATTICE` | canonical states resolve to rank `-1`; concurrent-op resolution broken for them | **OPEN (P0)** |
| Transition table | `work_item_model.py:_CANONICAL_TRANSITIONS` | canonical, complete — but only the regista path enforces it | divergence (§3) |
| State→status map | `core/projection.py:_STATUS_FROM_STATE` | canonical, complete | OK |
| DB vocab flags | `wi_status` (is_terminal/is_open) | canonical, seeded by `820_canonical_lifecycle.sql` | OK |

The fold-lattice bug (P0) is live: `kernel._status_rank` returns `-1` for every
canonical state, so in the concurrent-ops branch of `_resolve_status_lattice`,
an `open` (rank 3) dominates an `in_progress`/`done`, and ties between canonical
states are broken purely by lexicographic `op_id`. This silently mis-resolves
concurrent native-path status writes on canonical states.

**Root cause:** the status vocabulary is defined in N places. Adding a state
(Plan 010) requires updating N surfaces, and there is no compile-time or test
signal that catches a missed one. Each surface is independently "obviously
correct" in isolation, so review misses it.

## 2. Goal / non-goals

**Goal:** one authoritative definition *per layer* of the status set and its
properties — the regista workflow YAML, the DB vocab, and `lifecycle.py` — with
every consumer importing/querying its layer instead of re-declaring, and
test-enforced agreement *between* the layers. Adding a state drops from ~7
independent edits to 3 coordinated ones, and any missed surface fails a
consistency test rather than shipping silently (see §0.1 for why this is "three
cross-checked layers," not literal single-sourcing).

**Non-goals (explicitly out of scope):**
- Unifying the **semantic** divergence between the regista and native write
  paths (e.g. `close` → review gate vs. terminal `close` op). That is a product
  decision about whether degrade-mode enforces the review gate (Invariant G);
  it belongs in a separate plan. This plan addresses **vocabulary** drift, not
  behavior drift.
- Removing the legacy states (`claimed`/`closed`) — they remain valid for
  pre-migration items and the op-log history.

## 3. The dual-path divergence (context, fixed by this plan's extraction)

`work_item_model.py` forks every mutation on `face_factory.get_face()`:

- **Regista path** (`_update_work_item_regista`): validates transitions via
  `_transition_for_status_change` (strict `_CANONICAL_TRANSITIONS` table); an
  unsupported transition raises.
- **Native path** (`update_work_item`, no face): writes a raw `set_status` op
  with any vocab-valid status — **no transition check**.

This is why the reported bug (WI-078/079) manifested differently per path: the
regista path raised "Unsupported status transition"; the native path silently
wrote a stuck `claimed`. The two paths must agree on what transitions are legal.
This plan extracts that decision to one place both call.

## 4. Design — the single source

**4a. Authoritative source = the DB `wi_status` vocabulary + a code module for
the static properties.**

The DB vocab table already holds `is_terminal` / `is_open` per status (seeded by
`820_canonical_lifecycle.sql`). The properties that are *static* (lattice rank,
valid transitions, legacy synonyms) belong in one code module so they are
importable without a DB round-trip and unit-testable in isolation.

New module: `agent_notes/core/lifecycle.py` — the **single** place that knows
status properties:

```
CANONICAL_STATES        # the set (open/in_progress/blocked/deferred/
                        #   in_review/in_human_review/done)
LEGACY_STATES           # claimed/closed (valid, pre-migration)
ALL_VALID_STATES        # union (drives verifier._VALID_STATUS)

IS_TERMINAL / IS_OPEN   # mirrors the vocab flags (single source; the vocab
                        #   table is the runtime ref, this is the static ref)

STATUS_RANK             # lattice rank for concurrent-op resolution (replaces
                        #   kernel._STATUS_LATTICE); covers ALL states

VALID_TRANSITIONS       # (old,new) -> transition name (replaces
                        #   work_item_model._CANONICAL_TRANSITIONS); the regista
                        #   YAML remains the workflow authority, this is the
                        #   agent-side mirror used for pre-flight validation

LEGACY_TO_CANONICAL     # legacy synonym -> canonical (replaces
                        #   cli/breadcrumbs._BC_STATUS_TO_WI and
                        #   core/bc_files._map_bc_status_to_wi)

transition_for(old,new) -> str | None   # raises ValueError if unsupported
rank(status) -> int
is_valid(status) -> bool
```

**4b. Consumers reference `lifecycle.py`, never re-declare:**
- `verifier._VALID_STATUS` → `lifecycle.ALL_VALID_STATES`
- `kernel._STATUS_LATTICE` / `_status_rank` → `lifecycle.rank` (delete the local
  table)
- `work_item_model._CANONICAL_TRANSITIONS` / `_transition_for_status_change` →
  `lifecycle.VALID_TRANSITIONS` / `lifecycle.transition_for`
- `cli/breadcrumbs._BC_STATUS_TO_WI` → `lifecycle.LEGACY_TO_CANONICAL`
- `core/bc_files._map_bc_status_to_wi` → `lifecycle.LEGACY_TO_CANONICAL`
- `projection._STATUS_FROM_STATE` → derived from `lifecycle` (it is a
  state-alias map; keep, but cross-check against `lifecycle` in a test)

**4c. Consistency guard (the thing that makes "can't miss a surface" true):**
a test that asserts the code module and the DB vocab agree:

```
def test_lifecycle_module_matches_db_vocab(ephemeral_db):
    # every wi_status row's is_terminal/is_open == lifecycle.IS_TERMINAL/IS_OPEN
    # every lifecycle state is present in the vocab
```

and a test that asserts `lifecycle.VALID_TRANSITIONS` matches the canonical
workflow's `from`/`to` pairs (already shipped in regista; mirror the existing
`test_canonical_convergence.py` pattern that compares packaged YAML bytes).

**These guards must run in the default CI job, not a DB-gated or opt-in suite.**
The "can't miss a surface" guarantee is only real if the consistency tests
actually execute on every PR; an `ephemeral_db` fixture that CI skips would make
the guarantee silently false. "Runs in default CI" is an explicit AC of WI-4.

## 5. Lattice rank design (the P0 fix)

Fail-safe principle (existing): in a concurrent conflict, **the more-unfinished
state wins** (surface work, never silently complete it). Ranks, highest = most
unfinished:

| status | rank | rationale |
|---|---|---|
| `open` | 8 | untouched — most unfinished |
| `in_progress` | 7 | actively worked |
| `blocked` | 7 | unfinished, externally blocked (≈ in_progress) |
| `in_review` | 6 | work done, awaiting review |
| `in_human_review` | 5 | past adversarial pass, awaiting accept |
| `claimed` | 4 | legacy lease-held (liveness, not lifecycle) |
| `deferred` | 2 | deliberately parked — low but above terminal |
| `done` | 0 | terminal |
| `closed` | 0 | legacy terminal |

Ties (`in_progress` vs `blocked`, `done` vs `closed`) break by lexicographic
`op_id` as today. `claimed` sits below the active lifecycle states (a lease
should not override real lifecycle progress) but above terminal. **This needs
adversarial review** — conflict-resolution semantics are the core CRDT property.
Scope that review at the **cross-axis `claimed` comparisons** specifically:
`claimed` is a liveness/lease axis rather than a lifecycle state, so ranking it
on the same scale means a concurrent `claimed`-vs-`in_review` resolves two
different axes against each other. The within-lifecycle ordering
(`open` > `in_progress` > `in_review` > … > `done`) is the uncontroversial part;
the `claimed` placement is the part that can surprise.

## 6. Work items

- **WI-1 — `core/lifecycle.py` + unit tests.** Define the module; port the five
  existing tables into it; full unit coverage (no DB). This is pure extraction —
  behavior identical, just relocated.
- **WI-2 — Wire consumers (mechanical).** Replace each local table with an
  import from `lifecycle`. Five call sites. Suite stays green.
- **WI-3 — Fix the lattice (P0).** Delete `kernel._STATUS_LATTICE`; route
  `kernel._status_rank` through `lifecycle.rank`. Add concurrent-resolution
  tests for canonical states (the gap that hid the bug).
- **WI-4 — Consistency guards.** `test_lifecycle_module_matches_db_vocab` and
  the transitions-vs-workflow-YAML cross-check. These are the regression nets
  that make "missed a surface" impossible going forward.
- **WI-5 — Shared transition pre-flight for the native path.** Have the native
  `update_work_item` path call `lifecycle.transition_for` before writing the
  `set_status` op, so both paths reject the same illegal transitions. Decide
  the policy for repair/admin overrides (an explicit `force=True` escape hatch,
  not a silent permissive path).

## 7. Sequencing & risk

WI-1 → WI-2 (pure refactor, behavior-preserving, low risk) → WI-3 (P0 fix,
needs the rank table reviewed) → WI-4 (guards) → WI-5 (behavior change on the
native path — highest risk; defer until regista go-live is confirmed stable, and
gated behind the consistency tests).

WI-1/WI-2 are safe to land immediately. WI-3 is the live bug fix. WI-5 changes
degrade-mode behavior and should be its own decision point.

## 8. What this does NOT consolidate (deferred)

The semantic dual-path divergence (`close` → review gate vs. terminal; claim as
state vs. lease primitive) is intentional degrade-mode behavior from Plan 010
§1. Unifying it is a product decision (does degrade-mode enforce Invariant G?)
and is out of scope here. This plan retires the **vocabulary** drift category;
that one retires the **behavior** drift category.

**Repair of already-mis-resolved items (decision).** The live P0 (§5) can have
*already* silently mis-resolved concurrent status writes on canonical states.
This plan stops it going forward but ships **no detection or repair pass** for
historical damage. Decision: accept this — incidence is low (concurrent writes
to the *same* item's status on *canonical* states, within the short live-MVP
window) and the op-log retains the raw ops, so a forensic pass is possible later
if a discrepancy surfaces. Recorded here so the absence is a choice, not a gap.

**Note (vocabulary vs. behavior — when the "stuck state" class actually
closes).** WI-1–4 consolidate the *vocabulary* and fix the lattice. The native
`update_work_item` path still writes any vocab-valid status with no transition
check until **WI-5** lands — so an illegal state remains creatable from the
native path (now merely spelled consistently). The "stuck/illegal state" class
is only fully retired at WI-5; treat WI-1–4 as necessary-not-sufficient for it.
