# Plan 013 — Single-source status vocabulary (close the Plan-010 drift category)

**Status:** Proposed 2026-06-29.
**Author:** glm-5.2
**Strategic role:** Retire the entire class of "a validation/resolution surface
never caught up to the canonical lifecycle" bugs by making the work-item status
vocabulary and its properties authoritative in **one** place, with every
consumer referencing it instead of re-declaring it.

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

**Goal:** exactly one authoritative definition of the status set and its
properties; every consumer imports/queries it. Adding a state becomes a
one-line change. A missed surface becomes impossible.

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
