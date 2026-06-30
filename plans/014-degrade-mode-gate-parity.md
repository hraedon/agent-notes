# Plan 014 — Degrade-mode gate parity (retire the behavior-drift category)

**Status:** Proposed 2026-06-29.
**Author:** opus-4.8.
**Strategic role:** Plan 013 retired the *vocabulary* drift category (one status
vocabulary, cross-checked). This plan retires the *behavior* drift category: the
native (no-regista) write path and the regista path must agree not just on what a
status is *called*, but on what a verb *does* — specifically whether work can be
completed unilaterally. This is the deferred item recorded in **Plan 013 §8**.

## 1. The problem (evidence)

`agent-notes` forks every mutating verb on `face_factory.get_face()`:

- **Regista path** (face present, `AGENT_NOTES_REGISTA_WRITES=1` — the live SoT
  steady state): `close` → `submit_for_review` → `in_review`. The agent **cannot**
  reach `done` unilaterally; completion requires a cross-lineage adversarial pass
  + accept (Invariant G), enforced by regista's built-in validators (Plan 023).
  See `work_item_model._close_work_item_regista` (raises on already-done / blocked
  / deferred / legacy-claimed; routes open/in_progress through the gate).
- **Native path** (no face — the degrade / `WRITES=0` rollback path): `close`
  writes a raw terminal `close` op (`op_type="close"`, `payload={"reason":
  "manual_close"}`) → fold → the item is **terminal immediately**. No
  `submit_for_review`, no review gate, no cross-lineage check. See
  `work_item_model.close_work_item` (native branch, ~line 685).

So the *same verb* produces divergent lifecycle outcomes by path: `in_review` on
the regista path, terminal on the native path. Plan 013 WI-5 closed the
`update_work_item` / `set_status` transition-shape gap (both paths now reject the
same illegal `set_status` transitions), but `close_work_item`'s native branch
still writes a terminal op directly — it never consults `lifecycle.transition_for`
and so is unaffected by WI-5.

**Why it matters.** The system's stated fail-safe principle (Plan 013 §5) is
"surface work, never silently complete it" — it is the rule the concurrent-op
lattice is built on. The native `close` path violates exactly that principle: it
silently completes work. And because the native path structurally cannot run the
regista built-in validators, even a "well-shaped" native drive
(`open → in_progress → in_review → in_human_review → done` via `set_status`)
would satisfy transition *shape* while bypassing the *cross-lineage* requirement
that is the whole point of Invariant G. The gate is not a property of the
vocabulary; it is a property of the path.

## 2. Goal / non-goals

**Goal:** the native path and the regista path agree on the one invariant that
matters for provenance integrity — **neither can unilaterally complete (reach a
terminal `done`/`closed`) without the review gate having run.** "Done" becomes
reachable only through regista, on every path.

**Non-goals:**
- Re-implementing the cross-lineage validators on the native side. They are
  regista built-ins by deliberate decision (Plan 010 §0a, Plan 023); duplicating
  them would re-create the drift this plan exists to remove. The native path must
  *defer* completion, not *fake* the gate.
- Removing the native write path. It remains the rollback/degrade path
  (`WRITES=0`). This plan makes it *fail-safe*, not absent.
- Changing the relaxed/strict accept policy (that is a per-deployment regista
  workflow choice, already settled in Plan 010 §0a).

## 3. The decision (the part that needs the user)

When regista is unreachable (or `WRITES=0`) and an agent runs `close`, what
should happen? Three coherent options:

- **Option A — Fail-closed, defer to regista (recommended).** Native `close`
  does **not** write a terminal op. It either (a) refuses with a clear message
  ("completion requires regista; the review gate cannot run in degrade mode"), or
  (b) records the item as `in_review` locally (the same shape the regista path
  produces at `submit_for_review`), leaving the actual adversarial-pass + accept
  to run when regista is reached. Completion is *impossible* off-regista. This is
  the choice consistent with the existing fail-safe lattice principle and with
  "native op_log = read-only history" (the go-live framing in
  [[reference-production-regista-store]]).

- **Option B — Degrade-and-mark.** Native `close` may reach terminal but stamps
  the op `gate=bypassed` (degraded provenance), and `agent-notes verify` surfaces
  any terminal item whose chain lacks a cross-lineage pass as a **degraded /
  unverified completion**. A later reconcile pass re-runs the gate against regista
  and flags violations. Keeps degrade-mode fully functional at the cost of a
  weaker (but *legible*) guarantee.

- **Option C — Drop the native write path entirely.** Make `WRITES=0` mean
  read-only: all mutating verbs require regista. Removes the dual path rather than
  reconciling it. Cleanest invariant, but eliminates the rollback escape hatch
  Plan 012 WI-4 deliberately kept ("set `WRITES=0` → writes revert to native;
  nothing lost"). Not recommended while the go-live MVP is still young and the
  rollback path is load-bearing safety.

**Recommendation: Option A(b)** — native `close` maps to a local `in_review`
(not terminal, not a hard refusal). It makes the two paths agree on the invariant
("no unilateral completion"), preserves the rollback path as *usable* (you can
still record progress, just not completion), and needs no new "degraded" op
concept. The verify-surfacing from Option B is worth adopting *additionally* as a
detector regardless of which path is chosen.

## 4. Work items (pending the §3 decision; written for Option A(b))

- **WI-1 — Route native `close` through the transition shape.** Replace the raw
  terminal `close` op in `close_work_item`'s native branch with the
  `submit_for_review`-equivalent: write the `set_status` op to `in_review`
  (via the WI-5 pre-flight, so `open` first `start`s, mirroring
  `_close_work_item_regista`). Keep the legacy `close` op only behind `force=True`
  (admin/repair). Update `item.closed` event emission accordingly (it now means
  "submitted," not "closed").
- **WI-2 — Forbid unilateral terminal on the native path.** `set_status` /
  `update_work_item` to `done`/`closed` on the native path raises without
  `force=True` (the `("open","done"): close_from_open` review-exempt transition is
  the one deliberate exception — confirm it is intended to remain reachable in
  degrade mode, or also gate it).
- **WI-3 — Degraded-completion detector in `verify`.** `agent-notes verify`
  flags any terminal item whose op-chain shows a terminal `close`/`set_status`
  to `done` *without* a preceding cross-lineage `adversarial_pass` in the chain.
  This catches both pre-existing native-path completions and any future
  `force=True` overrides. (This is Option B's legibility, adopted as a net.)
- **WI-4 — Reconcile pass (optional, pairs with the outbox).** When a degraded
  item later reaches regista, re-drive it through the gate (or leave it terminal
  and record an attestation that the gate was retroactively waived). Decide
  whether this is automatic or operator-invoked.
- **WI-5 — Cross-path behavior test.** A single test that drives the *same* verb
  sequence through both `face_factory` branches and asserts the terminal-reachability
  invariant holds identically. The Plan 010 unit suites test each path in
  isolation, which is exactly how this drift stayed invisible (cf. the dossier
  Plan 010 cross-face gap — same lesson).

## 5. Sequencing & risk

WI-1/WI-2 are the behavior change (degrade-mode `close` no longer completes) —
**this is a user-visible behavior change to the rollback path** and should not
land until the §3 decision is explicit. WI-3 (detector) is safe and valuable
independently and can land first as the legibility net. WI-5 is the regression
guard. WI-4 is deferrable.

**Risk:** anything currently relying on native `close` reaching terminal (scripts,
the `WRITES=0` workflow) changes meaning. Audit callers before WI-1. Given the
go-live MVP runs `WRITES=1`, the native completion path should have near-zero
legitimate steady-state users — but confirm before flipping.

## 6. Relationship to Plan 013

013 made the vocabulary single-sourced and fixed the P0 lattice. This plan makes
the *gate behavior* path-invariant. Together they retire both drift categories
named in 013 §8. After this lands, "what a status is" and "what a verb does" are
both consistent across the dual path, and the only remaining cross-path
difference is *where the validators run* (regista only) — which is by design, not
drift.
