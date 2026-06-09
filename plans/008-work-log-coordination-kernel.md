# Plan 008 — Work-log coordination kernel (provenance-enforced, op-CRDT)

**Status:** complete 2026-06-09 — **Tier A shipped** (P0–P4 complete, migration executed and verified, backlog triaged, docs truthful, v1.0.0 tagged). Tier B items (regista coordinator, requeue daemon timer, causal-stability watermark, keyless signer) are explicitly deferred per the Definition of Done.

> **Switch-to-new-version bar is defined below** — see [Definition of Done](#definition-of-done-added-2026-06-08-opus-48). Adopting the kernel today requires only **Tier A** (degrade contract as default mode + migration + lived-in surface + backlog reconcile + green gates + tag). The regista coordinator and requeue daemon timer are **Tier B** — optional layers that attach later and are **not** blockers for the switch.

**Implementation log:**
- 2026-06-08: P0 kernel landed (op_log, content_blobs, work_items cache, fold, ready/claimable, event surface, CLI)
- 2026-06-08: P1 verifier landed (hash-chain, signature, policy checks, standalone CLI)
- 2026-06-08: P2 status lattice landed (fail-safe: open > claimed > closed > deferred, tie-break by op_id)
- 2026-06-08: P2 merge op support landed (merged_state replaces fold state)
- 2026-06-08: P2 deterministic merge/reconcile landed (`merge_entity`, `reconcile_entity` — union + sort + fold + merge op)
- 2026-06-08: P3 cross-project foundation landed (`request`/`wait` ops, `project:identifier` addressing, `add_cross_project_link`)
- 2026-06-08: P3 registry landed (`projects.log_location` + `projects.wake_channel`, Backstage-style descriptors)
- 2026-06-08: P3 derived index landed (`cross_project_ops`, `cross_project_work_items`, `cross_project_freshness`)
- 2026-06-08: P3 export/ingest landed (`export_ops_jsonl` → JSONL; `ingest_jsonl_ops` → derived index; `rebuild_cross_project_cache`)
- 2026-06-08: P3 cross-repo ready query landed (`work_items_ready_v` checks `cross_project_work_items` for blockers)
- 2026-06-08: P3 reverse-edge map landed (`cross_project_reverse_edges_v`, `get_blocked_by`, `get_cross_project_blockers`)
- 2026-06-08: P3 CLI landed (`work-item export-ops`, `work-item ingest-ops`, `work-item rebuild-cache`)
- 2026-06-08: P3 trigger loop landed (`agent-notes-trigger-loop` — LISTEN on `agent_notes_op_log_events`, route `request.created`/`dependency.blocked`/`dependency.resolved` to target wake channels via agent-wake)
- 2026-06-08: P4 local lease schema landed (`work_item_leases` table, `sweep_expired_leases()`, `work_items_claimable_v` excluding active leases)
- 2026-06-08: P4 CLI landed (`work-item claim`, `work-item release`, `work-item heartbeat`, `work-item requeue-expired`)
- 2026-06-08: P4 model layer landed (`WorkItemModel.claim_work_item`, `release_work_item`, `heartbeat_work_item`)
- 2026-06-08: **Tier A degrade contract landed** (`coordinator.py`, `doctor` coordination-mode check, local-lease is default safe mode)
- 2026-06-08: **Tier A migration script landed** (`migrate_breadcrumbs_to_work_items.py` — idempotent, status mapping, links conversion)
- 2026-06-08: **Tier A surface update landed** (skills updated to use `work-item` commands and `wi_*` vocabulary)
- 2026-06-08: **Tier A vocabulary fix landed** (`schema/700_work_log_kernel.sql` seeding uses dynamic CTE lookup for `default` workspace ID instead of hardcoded `workspace_id = 1`; prevents missing `wi_*` vocab entries on existing databases)
- 2026-06-08: **Tier A acceptance test passed** — full `start → file → add blocks-edge → ready/claim (local lease) → close → end` cycle completes on `coordinator-absent / local-lease` mode; cache rebuild from op-log matches live cache byte-for-state; `export_ops_jsonl` → `ingest_jsonl_ops` round-trips; verifier confirms hash-chain + signatures

**Open items (post-implementation):**
1. (P4) regista coordinator integration — atomic claim/lease/heartbeat via regista `_claims_api.py`
2. (P4) `requeue_expired` daemon — cron/systemd timer that runs `sweep_expired_leases()` periodically
**Author:** Opus 4.8 (design session with principal; reviewed by MiMo; prior-art deep dive + agentattest analysis)

---

## Definition of Done (added 2026-06-08, Opus 4.8)

This plan describes a multi-phase research arc whose *full* completion (L3 regista
coordinator, causal-stability watermark, cross-project trigger loop) is **weeks of
multi-project work and is NOT the bar for adopting the new version.** The bar for
"switch the daily driver to the kernel today" is **Tier A** below; everything in
**Tier B** attaches later as an optional layer and is explicitly out of scope for the
switch.

The line is drawn where the plan's own thesis draws it: *"Compose by optional
capability, not hard dependency. A bare work-log kernel works alone."* and **Invariant
W** — readiness and coordination state are always recomputable from the log/index;
agent-wake and the regista coordinator are best-effort accelerators carrying no
authoritative state. **Shipping without L3 loses no correctness — only race-free
concurrency at multi-writer scale**, which is not the single-/few-writer reality the
tool is adopted into today. That is what makes the cut honest rather than a corner.

### Tier A — required to switch to the new version (today's bar)

1. **Degrade contract is the DEFAULT, named, safe mode — not undefined behavior.**
   Coordinator-absent is the user's normal state (no regista coordinator running), so it
   must be a *first-class* mode: reads, progress on already-held items, and `append`/
   `file` all work freely; the **local lease table** (already landed) provides
   claim/heartbeat/release for the single-/few-writer case; with no coordinator
   configured the kernel **does not attempt — and never hangs or crashes attempting — a
   distributed claim.** `doctor` prints which coordination mode is active.
2. **Migration executed and verified** — breadcrumbs → work-item entities, `links` rows →
   typed edges, `change_log` → op-log. Run on a **backed-up** store, reusing the Plan 007
   files→DB importer; the NULL-`repo_root` decision (backfill vs default-and-flag) is
   made and recorded; **rebuild-from-log reproduces the cache byte-for-state** (the
   "cache cannot diverge-as-a-bug" invariant, actually demonstrated).
3. **The surface the user touches runs on the kernel with no behavior regression** — the
   CLI verbs, the 7 skills (`file-breadcrumb` … `end`), and the Plan 007 lifecycle hooks
   operate against work-items / the op-log. `breadcrumb` verbs keep working (alias or
   documented rename); a daily session (`start` → file/update → `end`) works end-to-end.
4. **Breadcrumb backlog reconciled** — all 11 open breadcrumbs triaged; already-fixed
   ones closed (BC-022 confirmed not-reproducible; BC-026 JSON-clean — verify and close);
   the small real ones (BC-018 dead `query` subcommand, BC-019 public pool close,
   BC-024 summary command, BC-025 reflection filename collisions) fixed **or** explicitly
   deferred with a one-line reason. No silently-stale "open" rows.
5. **Green gates** — full `pytest` green; `agent-notes doctor` all-PASS on the migrated
   store; the **P1 verifier** confirms the hash-chain + signatures over the migrated
   op-log; `export_ops_jsonl` → `ingest_jsonl_ops` round-trips.
6. **Docs are truthful about the boundary** — README/AGENTS describe the kernel as the
   live model AND state plainly that the regista coordinator is an **optional, not-yet-
   attached L3 layer** and that cross-project triggering is deferred. No write-only-
   theater claims (no "attested" language the verifier doesn't actually enforce on the
   adopted path).
7. **Tagged + reversible** — a version tag (e.g. `v1.0.0`) with a CHANGELOG entry, and a
   documented rollback (restore the pre-migration backup + previous tag).

**Tier-A acceptance test (the one command that proves "switch-ready"):** on a fresh
clone with only `AGENT_NOTES_DSN` set and **no** regista/agent-wake configured, a full
`start → file work-item → add blocks-edge → ready/claim (local lease) → close → end`
cycle completes, `doctor` is all-PASS reporting *coordinator-absent / local-lease* mode,
and the cache rebuilt from the op-log alone matches the live cache.

### Tier B — deferred to its own arc (NOT required to switch; attach later)

- **(L3) regista coordinator integration** — atomic distributed claim/lease/heartbeat via
  regista `_claims_api.py`. Only needed for race-free claims under *true* concurrent
  multi-writer load. Until attached, Tier-A's degrade contract is the contract.
- **(P3) Cross-project trigger loop** — `request`/`wait`/reverse-edge wake routing via
  agent-wake (async listener on the `agent_notes_op_log_events` NOTIFY channel). The op
  schema already carries the cross-log reference fields, so this stays purely additive.
- **(P4) Causal-stability watermark + archival truncation** — only matters at multi-
  replica scale; structural compaction (per-entity chains + cache) already keeps growth
  off the hot path.
- **(P4) `requeue_expired` as a managed daemon** — `sweep_expired_leases()` exists and is
  CLI-invokable; running it on a cron/systemd timer is ops polish. A startup + on-demand
  sweep is sufficient for the single-writer reality.
- **(P1+) Keyless / Sigstore signer** — local-key signer (already the default) covers the
  offline / air-gapped / regulated path the tool ships into.

**Sequencing note for the agents finishing this:** do Tier A in the order 2 → 3 → 1 →
4 → 5 → 6 → 7 (migration and the lived-in surface first, since they carry the real risk;
the degrade contract is mostly *guarding against* a coordinator path that isn't wired
yet, so it is small once 2–3 are solid). Do **not** start any Tier-B item until every
Tier-A box is checked and the tag is cut — a half-attached coordinator is worse than a
cleanly-absent one.

---
**Strategic role:** Completes agent-notes' *original* vision — "coordinating small bits
of work into a larger project over time" — by growing the DB-canonical memory layer
([[Plan 007]]) into an **op-CRDT work log** with dependency edges, a `ready` query,
cryptographic provenance, and cross-project / multi-agent coordination. This is the
spine that the four-project stack (agent-notes / agent-provenance / agent-wake / regista)
turns out to be four *roles* of.

> Supersedes the implicit "breadcrumbs are just records" model. Breadcrumbs become
> **work items** (beads) with `blocks` edges and a driving `ready` query. Plan 007's
> enforcement hooks remain the delivery mechanism; this plan is what they enforce.

## Thesis

- **The source-of-truth war is not an engine problem.** It is dual-authority with no
  reconciliation (files *and* Postgres). The fix is **exactly one authority; everything
  else derived.** Here the authority is an **append-only, attested operation log**
  (git-bug's op-CRDT model); the SQLite/Postgres projection is a **rebuildable cache**
  that cannot diverge-as-a-bug.
- **Provenance is "baked in" only if a consumer refuses to proceed without it.**
  Attestation with no verifier is write-only theater. The verifier is a **standalone
  CLI contract**; sf2 / CI / an auditor are interchangeable callers.
- **Compose by optional capability, not hard dependency.** A bare work-log *kernel*
  works alone. Provenance, liveness (agent-wake), and strong-consistency coordination
  (regista) **attach** as layers, each independently shippable and valuable. This is how
  "inclusive" is honoured and how it actually ships.
- **Invariant W (level-triggered truth).** Readiness and coordination state are always
  recomputable from the log/index alone. agent-wake notifications are **best-effort
  accelerators** that carry no authoritative state — a lost notification never loses
  work. This is a kernel law, not an implementation nicety.

## Build-vs-adopt (settled by the prior-art deep dive)

**BUILD** — no system combines {attested + git-native/mergeable + cross-project +
multi-agent}. Every *primitive* is proven; only the assembly is novel (the good risk
profile). We **borrow models, not dependencies**:

- **git-bug** — operation-based CRDT: per-entity op chains, Lamport clocks, deterministic
  ordering (ties broken by op-ID hash). *This is our merge primitive.*
- **Radicle COBs** — signed operations with a public-key `ActorId`; explicit CRDT math.
- **`agentattest`** (AuroraAeon, Apache-2.0) — reference implementation of our L1 stack
  (DSSE + in-toto Statement v1 + Sigstore + OPA/Rego + CUE/JSON Schema), but at
  *run→PR* granularity. **Complementary, not competing**: our op-log is the *process*
  attestation; an agentattest-style Statement is the *output* attestation, with our ops
  as its `evidence`/`materials`. Borrow the predicate design (`agent-provenance-v0`);
  do not take the dependency (Go, single-dev, v0-alpha).
- **Backstage** — Component/System/Domain catalog with YAML descriptors in VCS as a
  rebuildable cache. *This is our project-registry model.*
- **beads / gastown** — dependency types and the multi-agent shape; gastown's Wasteland
  claims are explicitly **non-atomic** ("a signal of intent, not a distributed lock") —
  that lossy behaviour is the gap we fill with atomic leases via the coordinator.

## What already exists (don't rebuild)

- `change_log` (000_core.sql) — the audit log + `NOTIFY` source. **Becomes the op-log
  substrate**; its `change_log_notify_fn` trigger + `bridge.py` is the agent-wake bridge
  we **re-point** from a DB trigger to a kernel post-commit hook.
- `links` (000_core.sql) — already carries cross-project edges
  (`from_project`/`to_project` + `relationship`). `blocks` edges are an **additive**
  migration, not new tables.
- `projects.repo_root` — the registry seed (longest-prefix resolution, Plan 007).
- Plan 007 enforcement: `init`, `orient`, `SessionStart` hook, error contract,
  files→DB importer, `resolved_via` librarian guard. **This plan is the payload those
  hooks enforce.**

## Kernel data model (L0)

- **Entities** (each = a per-entity op chain): `work-item` (the bead — breadcrumbs
  migrate here), `memory` (stays **first-class**, own chain — memories exist independent
  of work-items and are low-churn, so the "extra chain to compact" cost is moot),
  `link/edge`. **Bodies are content-addressed blobs** referenced by hash — editing
  metadata never re-logs the body (the "bloat at small scale" fix).
- **Operations** (delta, never full-state): `create`, `set-status`, `set-field`,
  `add-link`, `remove-link`, `claim`, `release`, `heartbeat`, `request`, `wait`,
  `close`, `snapshot` (seal), `merge`. Each op: `op_id` (content hash), `entity_id`
  (hash of first op), `op_type`, `lamport`, `actor_id` (pubkey), `payload`, parent refs.
- **Op authority — fix the schema at P0 even though cross-project triggering is P3.**
  An op only ever mutates an entity in **its own author's** log. The `request` op is the
  load-bearing case: it lives in the **dependent's** (A's) log as **signed evidence of a
  request**, NOT a `create` in B's log — A never forges B's identifiers. The created work
  item is a separate `create` op in **B's** log that **references A's request by
  `op_id`** (a `satisfies`/`from-request` field in its payload). So the `create` op
  payload and the `request` op must carry these cross-log reference fields **from day one**
  so the P3 intake flow is purely additive (no schema migration to enable it later).
  Same shape applies to `wait` (registered in A's log, keyed to a target `op_id`/entity).
- **Fold:** apply ops ordered by `(lamport, op_id)` → current state in the SQLite cache.
- **Status is `{open, claimed, closed, deferred}`; "blocked" is DERIVED from the edge
  graph, never stored** (storing derivable state is the anti-pattern this project exists
  to kill).
- **Status lattice (P2, hardcoded, fail-safe direction):** on *genuinely concurrent*
  conflicting status ops, **reopen/open dominates closed** — surface unfinished work
  rather than silently hide it. (Sequential close-then-reopen is already Lamport-ordered;
  the lattice only fires on true ties. Do **not** copy Radicle's `closed`-wins direction.)
  Configurable lattices are a **P3+** concern — do not build a policy engine in P2.

### `ready` / `claimable`

- `ready` = `status = open AND status != deferred AND NOT EXISTS (blocks-edge whose
  target.status IN {open, claimed})`. A one-liner against the SQLite cache.
- `claimable` = `ready AND not currently leased`. The query returns `ready`; the atomic
  `claim` op enforces unclaimed (P4).
- Lease-expired items return to `open` via a `requeue_expired` sweep (P4) — `ready` does
  not special-case leases.
- Cross-project blockers resolve via the **derived index**, never by reaching into other
  repos' logs.

## Compaction (phased — simple at P0, full at P4)

Make growth *not matter* at three levels; **build only the P0-simple case first**:

1. **Structural** — per-entity chains + the SQLite cache mean log size touches only
   *rebuild* cost, never query latency. Dormant/closed chains cost nothing on the hot path.
2. **Checkpointing** — fold a chain into a sealed, **attested** `snapshot` op; rebuild =
   snapshot + short tail. **P0: seal-on-close is trivial** (single writer, no concurrency).
3. **Archival** — size-or-time segments (seal at N ops *or* 1 month); sealed segments
   relocate to cold/deep-git storage (archive, never delete — provenance preserved).

**Causal-stability watermark** (only truncate ops all replicas have merged) is a **P4**
concern — *not needed for P0 single-writer* and must not be built upfront. The plan's
phasing makes this explicit: P0 writes the simple case; multi-agent complexity enters at P4.

## Capability layers

### L1 — Provenance (DSSE envelopes from P0; enforcement flips at P1)

- **Emit DSSE + in-toto Statement v1 envelopes from the first commit (P0)**; the
  verifier is *off*. P1 flips enforcement on. This makes "every event attested since day
  one" *true* (no grandfathering of unsigned legacy ops) — a material audit difference.
- The envelope is ~50 lines; the real P0 cost is **signing identity / key custody**.
  **Signer is an interface at P0; implement local-key first** (works offline / air-gapped
  / regulated — the workplace path from day one). Keyless/Sigstore is a **P1+** signer
  added behind the same interface without changing the envelope format.
- Predicate: custom type derived from `agentattest`'s `agent-provenance-v0`, trimmed to
  op granularity. `snapshot`/`merge` ops are themselves attested (meta-attestation:
  inputs + deterministic function + output hash) so archival/reconciliation is provable.
- This is where **agent-provenance** (the project) plugs in; its niche is sharpened by
  agentattest's coarse run→PR claim → agent-provenance owns the **fine-grained,
  append-only, coordination-integrated** attestation agentattest explicitly does not do.

### L2 — Liveness (agent-wake) + the coordination loop

- **Event emission (P0 hook point):** every committed op derives a typed event; the
  kernel runs a **post-commit hook chain over a *generic subscriber list*** (not a
  hardcoded single sink). Replaces the `change_log_notify` Postgres trigger; agent-wake
  is the first subscriber, reusing `bridge.py`'s HMAC→agent-wake-v0 mapping. **P0 logic
  stays regista-agnostic** — the kernel emits events and never reaches into claim/lease
  semantics; *what consumes events is not P0's concern*. This is why the decision-56
  reversal is clean: P0 ships an **event surface, not a coordination layer**. The
  subscriber-list version is ~1h more than single-sink and removes P4 rework.
- **Two delivery modes:** *edge* = post-commit → agent-wake POST (fire now); *level* =
  `agent-notes events --since <cursor>` replayable tail (resume after downtime). Per
  Invariant W, a missed edge is recovered from the level tail.
- **Event taxonomy:** `item.created`, `item.status_changed`, `item.closed`,
  `link.added/removed`, `dependency.blocked`, `dependency.resolved`, `request.created`,
  `claim.granted/released`, `lease.expired`.
- **Cross-project trigger loop** (P3 — needs registry + index for routing):
  1. A files a blocker: reference existing (`add-link A:BC-12 blocked-by B:REG-45`,
     edge owned by A) or **`request`** new work (`request.create target=B` in A's log;
     B's intake materializes a real B item + links back — preserves B's authority; the
     request is signed by A).
  2. **Trigger B:** wake routes `request.created`/`dependency.blocked` to B's channel.
     Live agent → woken; else durably queued, drained by B's SessionStart. "Trigger" =
     **enqueue + wake-if-listening, NOT auto-spawn** (auto-spawn is opt-in target policy).
  3. **B resolves:** `close` op → `item.closed(B:item)`.
  4. **Wake A / resume:** index reverse-edge (`B:item blocks A:BC-12`) →
     `dependency.resolved(A:BC-12)` → wake A. If A registered a **`wait`**
     (block-and-wait), that session resumes; else A's next SessionStart sees BC-12 now
     `ready`.
- **Safety rails:** idempotent delivery (events carry `op_id`; dedupe); coalesce/debounce
  wakes per target per window (reuse `bridge.py` buffering); no auto-spawn cycles
  (bounded session-resume only). Every step is a signed op → verifiable cross-project,
  cross-agent causal chain.

### L3 — Coordination (regista, online coordinator replica)

- regista provides **atomic claim / lease / heartbeat + live view** (it already
  implements this lifecycle and is event-sourced — the model matches). It is the
  **coordinator replica**, not a second SoT; the per-repo attested logs remain durable
  replicas, so portability/compliance survive.
- **Coordinator is REQUIRED for race-free new claims**, optional for reads and progress
  on already-held work. (Refines the deep-dive's "optimization, not requirement" — without
  it you ship gastown's lossy intent-signal.)
- **Degrade contract:** coordinator down → reads + progress on held items + append/file
  freely; **no new claims**. This makes offline safe by construction (forbids the only
  operation that races) and reconciles cleanly on reconnect.

## Cross-project layer (P3)

- **Registry:** Backstage-style descriptors (`project → repo_root → log location →
  wake channel`); resolution via Plan 007's longest-prefix + `resolved_via` guard — an
  unresolved upstream ref reads as **"unknown, unverified," never "not blocking."**
- **Index:** derived, rebuildable cache that ingests every repo's JSONL op-log; answers
  cross-project `ready`, holds the reverse-edge map, records per-project freshness
  (commit/offset). **Cache, never SoT.** Nearly free given JSONL is the interchange format.
- **Addressing:** `project:identifier` (Plan 007 Piece 4 sugar). Edges owned by the
  dependent.

## Phase plan

- **P0 — kernel, single-writer.** Op-CRDT model (ops + Lamport + fold), entities
  (work-item/memory/link), content-addressed bodies, `ready`/`claimable`, SQLite cache,
  **simple compaction (seal-on-close)**, **DSSE envelopes emitted (unverified)**, signer
  interface + local-key, **post-commit hook surface + `events --since` tail**. Breadcrumbs
  migrate to work-items. *Ships value alone — "beads + memory + provenance-ready."*
- **P1 — enforcement on.** Standalone **verifier CLI** (DSSE sigs + hash chain + OPA/Rego
  policy); flip P0's envelopes to verified; sf2 calls the CLI as a gate step; keyless
  signer added.
- **P2 — merge / reconcile** (the keystone). Deterministic merge, **fail-safe status
  lattice** — *prerequisite: read Radicle `issue.rs` status-merge rules first*. Enables
  replicas.
- **P3 — cross-project.** Registry + index + `project:identifier`; the cross-project
  trigger loop (request/wait/reverse-edge routing) via agent-wake.
- **P4 — full multi-agent.** regista coordinator (atomic claim/lease/heartbeat), degrade
  contract, `requeue_expired`, causal-stability watermark, wake-on-unblock at scale.

## Prerequisites & migration

- **Reverses Plan 004 decision 56** (agent-notes does not publish to regista). Deliberate,
  dated here: regista returns as the **coordinator replica** at P4, justified by the
  strong-consistency multi-agent requirement. Composition boundary: kernel emits events;
  regista coordinates claims — not a merge of the two products.
- **Migration is its OWN piece — do not interleave with kernel feature work.** It is a
  standalone PR that merges **before P0**, or P0's **first commit with its own test
  plan**. Blast radius: it touches **every existing `change_log` and `links` row**
  (breadcrumbs → work-item entities; `links` rows → typed edges; `change_log` → op-log),
  which is exactly the kind of backfill that turns a feature branch into 400 files if
  smuggled in. **Decision required:** sf2/substrate/v1 NULL-`repo_root` —
  backfill-the-real-path vs default-and-flag (see [[reference-breadcrumb-store-divergence]]).
  The files→DB importer (Plan 007) is reused here. This + `repo_root` resolution are
  prerequisites for P3 cross-project resolution.

## Open items (flagged to their phases, not blockers for the architecture)

1. **(P0/P1) Key custody model** — local-key default settled; keyless P1+. Remaining:
   actor identity establishment + key storage location across harnesses.
2. **(P1) Predicate schema** — borrow from `agentattest`'s `agent-provenance-v0`, trim to
   op grain.
3. **(P2) Radicle `issue.rs` status-merge rules** — the last hard prerequisite and the
   most policy-laden decision; copying the wrong lattice direction is expensive to
   reverse. **Must read before finalizing P2.** (Deep-dive gap #1 — not yet closed.)

## Prior art credited

git-bug (op-CRDT model) · Radicle COBs (signed ops, CRDT math) · agentattest
(DSSE+in-toto+OPA reference, complementary granularity) · Backstage (registry model) ·
beads/gastown (dependency + multi-agent shape, the non-atomic-claim gap) · in-toto/DSSE/
SLSA/Sigstore (attestation stack) · gittuf/OPA (policy-as-code verifier).
