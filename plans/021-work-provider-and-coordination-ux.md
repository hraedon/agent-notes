# Plan 021 — Finish the WorkProvider seam and borrow proven coordination UX

**Status:** Proposed 2026-07-11.
**Author:** GPT-5.6 Sol, after a fresh Chainlink, Beads, Gas Town, and local
work-kernel comparison.
**Depends:** Plans 008, 010, 014, and 016.
**Strategic role:** Preserve regista's signed work authority while finishing the
provider boundary already implicit in `WorkItemModel` and adopting the small
coordination affordances that materially improve agent work.

## Decision

Do not replace the work-item/breadcrumb system with Chainlink or Beads/Gas Town.

- Chainlink is a cohesive local issue/session tracker, but it is a subset of the
  current dependency, history, cross-project, lease, identity, and review model.
- Beads is now a strong competing work graph. Current Beads documents Dolt-backed
  versioning, atomic `ready --claim`, routes/external dependencies, formulas,
  molecules, gates, and multi-agent operation. It does not document the suite's
  per-principal signed transition, delegation, replay-verification, or enforced
  cross-lineage review contract.
- Gas Town is an orchestration runtime over Beads: dispatch, roles, worktrees,
  sessions, mail, supervision, convoys, and merge queues. Adopting it would be a
  product-direction change and conflicts with agent-suite's deliberate
  no-control-plane boundary.

Regista remains the default work authority; the native op-log remains the named
degrade provider. Finish the seam so that this is an explicit choice rather than
a concrete-face check. Do not build a tracker adapter until a deployment chooses
another authority intentionally.

## Ground truth

- `core/work_item_model.py` is already a thin dispatch facade over regista and
  native modules for most mutations.
- Dispatch calls `face_factory.get_face()` and therefore exposes the concrete
  regista integration at the generic boundary.
- Several reads, graph traversals, links, cross-project queries, and projections
  still assume local agent-notes tables even when regista is authoritative.
- The current model already has dependency-aware ready/claimable queries,
  claims/heartbeats/releases, request/wait cross-project operations, review-gate
  transitions, a native op-log verifier, and signed regista events.
- Plan 008's June 2026 prior-art note says Gas Town claims are non-atomic. That
  describes the Wasteland advisory claim discussed then, but current Beads also
  exposes atomic `bd update --claim` and `bd ready --claim`. The historical plan
  needs a dated addendum, not a rewritten conclusion.

References:

- https://github.com/dollspace-gay/chainlink
- https://github.com/gastownhall/beads
- https://gastownhall.github.io/beads/architecture
- https://gastownhall.github.io/beads/next/cli-reference/ready
- https://github.com/gastownhall/gastown
- https://github.com/gastownhall/gascity

## Invariants

1. Work state and learned memory remain separate provider domains.
2. Exactly one work provider is authoritative for a project.
3. Unsupported lifecycle, claim, review, cross-project, audit, or replay
   capability is named. A provider cannot simulate parity with a successful
   no-op.
4. `done` retains the canonical independent-review invariant for providers that
   claim the suite workflow capability.
5. Notifications, orientation, cached projections, and orchestration runtimes
   are accelerators or faces; they do not silently become work authority.
6. No bidirectional regista/Beads synchronization is introduced.

## Phase 0 — Refresh the decision record

### WI-0.1 — Dated Beads/Gas Town prior-art addendum

Append a dated correction to Plan 008 distinguishing Wasteland's advisory claim
from current Beads atomic claim commands. Re-run the comparison against current
official documentation.

**AC:**

- Every claim about Chainlink, Beads, Gas Town, and Gas City is dated and linked
  to a primary source.
- The addendum records what has changed: Dolt-only storage, server-mode
  multi-writer operation, atomic ready/claim, formulas/molecules/gates, routes,
  and current multi-clone sync caveats.
- The retained-regista decision is justified narrowly by suite requirements:
  enrolled/signed principals, delegation, replay verification, canonical
  lifecycle enforcement, and cross-lineage review.
- The adoption trigger is explicit: if autonomous swarm throughput becomes the
  product center, integrate or adopt Gas Town rather than rebuilding its
  control plane here.

## Phase 1 — Typed WorkProvider boundary

### WI-1.1 — Protocol and capabilities

Define a `WorkProvider` protocol with capability declarations for:

- create/get/update/query/delete;
- lifecycle transitions and review gates;
- dependencies, links, ready, and blocker explanation;
- cross-project request/wait and reverse blockers;
- acquire/heartbeat/release claim and atomic claim-next;
- audit/history/replay and health.

**AC:**

- `RegistaWorkProvider` and `NativeWorkProvider` implement the mandatory
  protocol without changing public CLI behavior.
- Capability types distinguish strong atomic claims from advisory assignment,
  and signed replay from ordinary change history.
- The conformance suite covers lifecycle parity, prohibited self-review,
  contested claims, lease expiry, dependency readiness, cross-project blockers,
  outage, and unsupported features.

### WI-1.2 — Complete dispatch and projection isolation

Move all work reads and writes, including cross-project and graph paths, through
the selected provider or an explicitly declared rebuildable projection.

**AC:**

- `WorkItemModel` contains no `get_face()` or concrete `RegistaFace` check.
- Provider selection is project-scoped, typed, cached, resettable, and visible in
  doctor.
- A projection cannot be mistaken for authority; stale/unavailable projection
  state is reported.
- Existing work-item and breadcrumb alias CLI/skill contracts do not regress.

## Phase 2 — Adopt the worthwhile Beads interaction patterns

### WI-2.1 — Atomic ready-and-claim

Add a single operation and CLI surface that selects the first ready item matching
declared filters and claims it atomically.

**AC:**

- `agent-notes work-item ready --claim --json` is one provider transaction, not
  `ready` followed by `claim` in the client.
- Under concurrent contenders, at most one actor receives a given item.
- Filters, selection order, lease TTL, actor attribution, and no-work result are
  deterministic and documented.
- Providers without atomic claim-next report it unsupported; they do not emulate
  it racefully.

### WI-2.2 — Explain readiness and blockers

Add machine-readable and concise human explanations for why an item is ready,
blocked, deferred, leased, awaiting review, or waiting on foreign work.

**AC:**

- `work-item ready --explain` and item-specific diagnosis enumerate local and
  cross-project blockers, their state/freshness, lease owner/expiry where
  authorized, and the exact rule excluding the item.
- Explanation and ready-query results are derived from the same provider
  decision, preventing diagnostic drift.
- Golden tests keep text concise and JSON stable.

## Phase 3 — Improve orientation and handoff

### WI-3.1 — Compact prime/orient view

Refine `orient` around the information a fresh agent needs: active assignment,
ready work, blockers, review queue, recent handoff, and provider health. Borrow
Beads' `prime` economy without importing its rules or storage.

**AC:**

- The default orientation fits a declared token budget and offers explicit
  detail commands instead of dumping history.
- Every item includes stable identifiers and the next valid action.
- Unavailable provider/projection states remain visible and do not produce an
  apparently empty queue.

### WI-3.2 — Signed handoff and prior-attempt context

Record an explicit handoff linked to work item, session, actor, attempt, current
state, next action, blockers, and relevant commit/evidence references. On
redispatch, surface prior failed or interrupted attempts and review notes.

**AC:**

- Handoff is a signed event/note linked to work, not a second work lifecycle.
- A newly assigned agent can identify prior owner, last verified state, failure
  or interruption reason, review feedback, and next action without reading the
  raw event history.
- Repeated handoffs preserve history and never overwrite signed evidence.
- Session-end integration remains safe when no work item is active.

## Phase 4 — Optional external-provider trigger, not implementation

### WI-4.1 — Beads/Gas Town adoption spike trigger

Run this work only when an operator commits to Gas Town/Gas City or another
Beads-native orchestrator. Compare two one-authority modes:

1. regista remains authority and the orchestrator consumes a read-only/dispatch
   projection; or
2. Beads becomes the declared `WorkProvider`, while regista records clearly
   labelled result/evidence attestations rather than mirrored mutable state.

**AC:**

- The spike proves authority, identity mapping, claim semantics, review mapping,
  deletion, reconciliation direction, outage behavior, and rollback on a real
  multi-agent job.
- If Gas Town requires bidirectional mutable synchronization, the integration is
  rejected rather than shipping dual authority.
- No generic tracker adapter enters core merely to make the protocol appear
  extensible.

## Explicit deferrals

- **Convoys/work sets:** wait for an observed need to track one outcome across
  multiple independent work items. If triggered, prefer a typed grouping/link
  whose progress is derived from member work; do not add another lifecycle.
- **Formulas/molecules:** compare their instantiation ergonomics with regista
  workflow composition only after repeated workflows create real friction.
- **Chainlink adapter:** no trigger exists; its useful session UX is covered by
  Phase 3.
- **Gas Town control plane:** permanently outside agent-notes and agent-suite.
- **Regista↔Beads bidirectional sync:** prohibited.

## Sequencing

The prior-art addendum lands first so implementation proceeds from current
facts. Phase 1 is a behavior-preserving boundary change. Phase 2 and Phase 3 are
independent once the provider contract exists. Phase 4 remains dormant until an
external orchestration deployment supplies the trigger.
