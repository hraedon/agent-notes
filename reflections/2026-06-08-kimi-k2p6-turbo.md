---
model: kimi-k2p6-turbo
datetime: 2026-06-08T01:15Z
project: agent-notes
---

# Session Reflection — 2026-06-08

**Work summary:** Closed out Plan 008 P1 (verifier CLI) and P2 (merge/reconcile with status lattice). P1 added `agent-notes verify` with DSSE signature verification, hash-chain checks, built-in policy, and 21 tests. P2 added the fail-safe status lattice (`open > claimed > closed > deferred`) to `fold_work_item`, enabling deterministic merge of concurrent ops. Also fixed a NaN bug in `suggest_duplicates` (both breadcrumbs and work items) where zero-vector similarity leaked unrelated rows. Total: 257 tests pass (32 new since P0).

---

## On the project

The codebase continues to be a pleasure to work in. The test-driven, DB-canonical approach means every change is verifiable. The `WorkItemModel` -> `BreadcrumbModel` mirroring is holding up well — adding a new kind is mechanical. The one thing that feels slightly off: the CLI `__init__.py` is now importing 13+ parser modules. The registration pattern is growing linearly. A plugin system (entry points) might be needed before P3 adds more verbs.

The `op_log` / `change_log` coexistence is still the elephant in the room. Both tables exist, both emit events. The migration to make `op_log` the sole audit source is going to be a large standalone PR. The plan is explicit about this, so the tension is intentional, not a mistake.

## On the work done

**P1 verifier:** The verifier module (`verifier.py`) is clean and standalone. The `Violation` dataclass with severity levels (error vs warning) is the right primitive — it lets the verifier run in "audit mode" (warnings only) vs "gate mode" (errors fail). The `NullSigner` -> `LocalKeySigner` upgrade path is smooth: the envelope format is present from day one, and flipping to real signatures is a config change. The `cryptography` package was promoted from optional to a real dependency (pyproject.toml) — this is correct for P1.

**P2 status lattice:** The lattice implementation (`_STATUS_LATTICE` in `kernel.py`) is straightforward. The key insight is that the lattice only applies to **concurrent** ops (same lamport). Sequential ops still use last-write-wins. The grouping-by-lamport in `fold_work_item` is the cleanest way to implement this without rewriting the entire fold. The tie-breaker (lexicographically smaller `op_id`) is deterministic because `op_id` is a content hash.

**NaN bug:** The `suggest_duplicates` SQL used `1 - (embedding <=> vec) >= threshold`. PostgreSQL's `NaN >= 0.95` evaluates to `True`, so zero-vector similarity queries leaked unrelated rows. The fix was to use `embedding <=> vec <= 1 - threshold` instead, which correctly evaluates `False` for `NaN`. Both `breadcrumbs_model.py` and `work_item_model.py` were fixed.

What I'd want a second pair of eyes on:
- The `parent_op_ids` in `commit_op` is still simplistic (`[entity_id]` for updates). For true multi-writer (P4), this needs to be the actual set of unmerged ops.
- The `merge` op is implemented in the fold but there's no CLI or model helper to create one. P3 will need this.
- The `wi_status_changed_fn` trigger is still a copy of `bc_status_changed_fn`. The migration PR should deduplicate these.

## On what remains

P2 is done. The next phases are:
- **P3 — cross-project:** Registry + index + `request`/`wait` ops + cross-project trigger loop. Needs the `merge` op to be creatable via CLI.
- **P4 — full multi-agent:** regista coordinator, atomic claim/lease/heartbeat, degrade contract, `requeue_expired`, causal-stability watermark.

Not needed before P3 ships:
- Breadcrumb -> work-item migration (still its own PR)
- `op_log` replacing `change_log` as sole audit source (its own PR)

## Gaps to flag

- **Missing CLI for merge op:** `merge` is handled in `fold_work_item` but there's no `agent-notes work-item merge` command. P3 will need this.
- **Schema drift risk:** `content_blobs` stores bodies as `TEXT`. For very large bodies (e.g., 10MB logs), this could be a problem. The schema should be watched; if bodies grow, switch to `BYTEA` or a separate storage backend.
- **Dead code:** `cmd_bc_query` in `cli/breadcrumbs.py` is still dead code (BC-018). Not fixed in this session.
- **Silent failure mode:** `WorkItemModel.file_work_item` with `identifier=None` calls `allocate_work_item_identifier`, which uses `FOR UPDATE` on the sequence table. If two calls race (concurrent writers), the second might block. P0 is single-writer, so this is fine. P4 needs the coordinator for atomic claim.
