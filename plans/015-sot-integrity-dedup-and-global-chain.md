# Plan 015 — SoT integrity: deduplicate work-items + repair the global chain

**Status:** Proposed 2026-06-30. **WI-1 (import idempotency) IMPLEMENTED + tested
(576 tests green).** WI-2 (dedup) and WI-3 (global-chain repair) are BLOCKED
pending decisions below.
**Author:** opus-4.8.
**Strategic role:** The convergence thesis is "one authoritative store." Two
pre-existing integrity defects in the production regista store
([[reference-production-regista-store]]) violate that and block finalizing the
agent-notes transition. Both trace to a single event: a non-idempotent,
concurrent breadcrumb file-import storm on 2026-06-29 22:27–22:39 (actor
`agent-notes`, ~434 events in 12 min).

## 1. Findings (evidence)

**F1 — ~52% duplicate work-items (regista schema).** 661 work-items, 321 distinct
normalized `source_identifier`s → 238 duplicate groups, 340 duplicate rows. Many
groups have divergent states (e.g. `[done, open]`), so the open backlog
double-counts and resolved work shows as open. The same logical breadcrumb appears
under two identifier formats (`050` and `BC-050`).

**Root cause (F1):** `WorkItemModel._file_work_item_regista` called
`face.create_breadcrumb` *unconditionally*. The only dedup guard was in the
caller (`bc_files.sync_breadcrumbs_from_dir` → `get_work_item`), which checks the
**local projection**, not the remote SoT. When the local projection is stale
(fresh session / reset local DB / per-project routing), a re-import is routed as
a "create" and mints a duplicate in regista. The migration's
`_find_existing_regista_id` had a second, independent bug: it scanned only the
first page (`page_size=100`) and matched the raw identifier exactly, so re-running
on a >100-item project (or across the two identifier formats) re-created
everything.

**F2 — Per-schema global hash chain broken across the ENTIRE store.** A read-only
replay sweep of all 15 schemas (2026-06-30): **every schema except `patina`
(1 event) has a broken global chain**, with **`replayed_drift=0` and `halted=0`
everywhere** (projections match the log; all signatures and per-item chains
verify). Counts: regista 192 broken (range 1205..1778), cert_watch 430, sf2 330,
gpo_lens 79, agent_notes 45, agent_wake 44, agent_provenance 37, adcs_lens 35,
usage_dashboard 16, dossier/sluice/acb/substrate/acme 7–8 each.

**Two distinct patterns — so this is NOT just tonight's storm:**
- **`regista` is partially broken in a window** (1205..1778; events 1..1204 and
  1779+ verify). That window matches the 22:27–22:39 import storm (actor
  `agent-notes`) — consistent with a concurrency race (`global_seq` assigned by
  `nextval()` outside the `event_chain_head FOR UPDATE` lock, so seq order ≠
  commit order; the sequence reached 64301 for 1077 events = ~63k rolled-back
  retries).
- **Every other schema is broken from `global_seq=2`** — i.e. essentially every
  link, including in 9-event low-traffic schemas. This is systematic, not a race:
  it points to the **convergence bulk-migration** having inserted events without a
  replay-verifiable `prev_global_event_hash`, **or** a replay/append mismatch in
  how the global chain is computed for migrated data. Root-causing this is a
  regista investigation.

**Benign (no tampering): the actual provenance content is intact everywhere
(drift=0, halted=0, signatures valid); only the cross-event ordering anchor is
unverifiable.** Earlier-noted detail (now contextualized):
`prev_global_event_hash` links are simply inconsistent.

**Root cause (F2):** the same concurrent import storm. The `global_seq` chain and
`event_chain_head` are **per-schema** (each project schema has its own
`events_global_seq_seq` starting at 1; regista 1..64301, agent_notes 1..1001,
dossier 1..401). The append path locks `event_chain_head FOR UPDATE`
(`_events.py:_lock_global_chain_head`), so within-schema appends *should*
serialize — yet regista's chain broke. The smoking gun: regista's sequence reached
**64301 for only 1077 committed events** (~63k consumed-but-rolled-back values =
massive retry/conflict activity during the storm). The likely mechanism is that
`global_seq` is assigned via `nextval()` **before/outside** the chain-head lock, so
the seq-assignment order can differ from the lock/commit order; replay orders by
`global_seq` and then sees `prev_global_event_hash` links that don't match. This
needs root-causing **in regista** — the merged WI-002/003 fixes address
witness/hook TOCTOU, not necessarily this global_seq-vs-lock ordering. **The
breakage is benign (no tampering): per-item provenance and all signatures verify;
only the per-schema ordering anchor is broken.**

Both findings are the same incident: the non-idempotent path under concurrency
created duplicates (F1) and raced the global chain (F2).

## 2. WI-1 — Import idempotency (DONE)

Make the regista write path idempotent on a **normalized** `source_identifier`,
so a re-import updates rather than duplicates — and the concurrent-storm pattern
cannot recur. **Implemented:**
- `regista_face.normalize_source_identifier()` — strips a leading `BC-`/`bc_`
  (separator required, so `BCD-1`/`WI-005` are untouched); `BC-050` == `050`.
- `RegistaFace.find_by_source_identifier()` — pages through all items via
  `query_work_items(custom_field_filters={"source_identifier": norm})` (JSONB
  `@>`, matches in-memory and Postgres backends identically).
- `_file_work_item_regista` — looks up by normalized id before creating; on hit,
  amends in place (no status re-drive — live lifecycle state wins, avoids gate
  violations). Stores the normalized id.
- `migrate_to_regista._find_existing_regista_id` — uses the paged normalized
  lookup; stores normalized ids.
- Tests: `tests/test_idempotent_filing.py` (unit + face-level) and a DB-backed
  regression in `tests/test_regista_write_through.py`
  (`test_refile_with_stale_local_projection_does_not_duplicate`) that reproduces
  the exact bug (drop local row → re-file under the other format → assert ONE
  item). Full suite: 576 passed.

## 3. WI-2 — Deduplicate existing work-items (BLOCKED on F2)

Script written: `scripts/dedup_regista_source_identifiers.py` (default dry-run).
Dry-run result: 338 losers to retire (185 open, 150 done, 3 deferred); 1
resolution-loss group (`298`) auto-flagged + skipped. regista is event-sourced —
no deletion; losers are driven to terminal-as-duplicate via signed events
(`close_from_open`, the gate-exempt dismissal; deferred → resume → close; done →
comment marker), validated against the canonical workflow.

**Winner rule:** richest history wins (most events, tie-break recency) — keeps the
most-worked copy and leaves losers in cleanly-terminable states. Any group where a
*done* loser would lose to a *non-done* winner is flagged and skipped (1 group).

**BLOCKED:** do not run against a store whose global chain is already broken (F2).
Adding 338 events would extend the broken chain and entangle the two repairs.
**WI-2 runs only after WI-3.**

## 4. WI-3 — Repair the global chain (BLOCKED on decision)

The global chain is broken but the breakage is benign (signatures + per-item
chains intact). Options:
- **A — Deterministic recompute (recommended).** Per affected schema, recompute
  `prev_global_event_hash` in `global_seq` order and reset `event_chain_head`.
  Legitimate because the events themselves are signed and unaltered; only the
  ordering anchor is being rebuilt. This belongs in **regista** (it owns the chain)
  as an admin/repair command, with a before/after replay proving
  `global_chain_broken == 0`. Needs the user's go — it rewrites a tamper-evidence
  structure, so it should be auditable (record the recompute as an attested event
  or a logged operation). **Prerequisite: fix the regista global_seq-vs-lock
  ordering bug first, else a recomputed chain re-breaks on the next concurrent
  write.**
- **B — Re-anchor forward only.** Leave history as-is, mark the broken window as a
  known benign incident (signed attestation), and ensure future appends are
  serialized (regista WI-002/003). Cheaper, honest, but leaves replay noisy.
- **C — Do nothing.** Not acceptable for a "provenance instrument" SoT.

**Open question for the user:** is global-chain repair a regista feature to build
first (then run), or a one-off operator script? And: confirm whether the merged
WI-002/003 fixes actually serialize the global-chain append, or if a further
regista fix is needed before any new writes (otherwise the storm can recur even
with idempotent imports, under concurrent *distinct* writes).

## 5. Scope check (other schemas)

The chain is per-schema, so F2 is per-schema too — but any schema that took
concurrent writes during the storm is suspect. Before WI-3, replay every schema to
size the repair (regista confirmed broken; agent_notes/dossier et al. unchecked).
F1 (duplicates) should also be re-scanned per schema — the same import path serves
all projects (the dry-run script takes `--project`).

## 6. Sequencing

WI-1 (done) → **WI-3 global-chain repair** (decision + likely a regista feature) →
WI-2 dedup (now safe) → re-verify (replay: 0 broken, 0 drift) → then resume Plan
012 WI-2 (remove the breadcrumb files, now that re-import is idempotent and the
store is clean). Plan 014 (degrade-mode gate parity) is independent and unaffected.
