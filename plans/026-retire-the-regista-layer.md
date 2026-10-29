# Plan 026 — Retire the regista layer and Hindsight: one native path, an enforced review gate

**Status:** Accepted 2026-10-29. The owner chose D2 option (a) and retired
Hindsight (D6).
**Owner:** `agent-notes`.
**Follows:** Plan 025 D7, which turned regista write-through off and made
native Postgres the work-item authority, but left the code in place.
**Retires:** regista write-through and library dependency; the Hindsight
memory engine.
**Out of scope:** `regista` itself, `dossier` (still built on regista, last
commit 2026-08-31), and the op-log kernel, which is the live native store.

---

## 1. Why

Plan 025 retired regista write-through. Every write now takes the native
op-log path, and production has run that way since the 025 cutover. The
regista path is still in the tree, though. Every `WorkItemModel` method
still branches on `face_factory.get_face()`, and the package still pins
`regista-hraedon>=0.7.0,<0.8`. AOS and agent-suite are parked, and regista is
parking after one final 0.8.0 release.

What that costs:

- Every change to the work-item path has two branches to reason about, and
  one of them is unsupported.
- agent-notes is pinned to a parked library, so a Python upgrade, or a
  regista 0.8 that we don't take, becomes our problem.
- The docs describe guarantees the live path no longer gives (F1).

Goal: **the native path is the only path, and agent-notes has no runtime
dependency on parked suite infrastructure.**

## 2. Findings (2026-10-29)

**F1. The native review gate doesn't check reviewer independence.**
`_native.review_transition` records `actor_id` and moves the status. Nothing
stops an implementer from running `accept` on its own work.
`require_reviewer_model_lineage` (`core/producer.py`) is called only from
`_regista.py:323`. AGENTS.md §"Canonical v6 actor and producer identity" still
says a producer with no model lineage "is refused … from casting a review
verdict". That stopped being true on the native path at the 025 cutover.
Production has 84 items `in_review` and 59 `in_human_review`, so the gate is
in heavy use.

**F2. Native ops carry placeholder signatures.** `kernel.commit_op` defaults
to `NullSigner`. The DSSE envelope and the verifier's `signature` rule do no
work on the live path; the hash chain is the real integrity check.
`~/.config/agent-notes/signing.key` exists on mvmcc02, but nothing on the
native path reads it.

**F3. regista is still imported as a library**, outside the write-through:

| Use | Where | Live? |
|---|---|---|
| `RegistaError` / `ErrorCode`: the CLI error envelope | `cli/__init__.py:18`, `cli/work_items.py:7`, `core/reconcile.py:17` | Yes, every CLI error |
| `validate_principal_id`: actor grammar | `core/actor.py:151` | Yes, every write |
| `parse_action_delegation` | `core/actor.py:180` | Only with `AGENT_NOTES_ACTION_DELEGATION_PATHS` |
| `regista.secrets`: DSN-or-ref resolution | `core/secrets.py:134` | Only when a DSN is a secret ref |
| `resolve_producer`, `MODEL_LINEAGE_FAMILIES` | `core/producer.py:57` | Regista path only (F1) |

**F4. Outside contracts.**
- `agent-suite`'s genesis gate consumes `agent-notes invariants probe --json`.
  agent-suite is inactive (last commit 2026-08-23), but the probe is cheap
  and keeps working with the native actor, so leave it alone.
- Nothing outside this repo imports `agent_notes.core.*regista*`.

## 3. Decisions

### D1. Delete the regista write path; don't just leave it off

`AGENT_NOTES_REGISTA_WRITES=1` stops being an unsupported option and becomes
an error. Its text points to `docs/spikes/025-regista-cutover.md`.
`WorkItemModel` methods call `_native` directly. Once the facade is a
one-liner per method, collapse it. The Plan 016 rule ("don't add logic to the
facade") becomes moot.

### D2. Enforce reviewer independence natively, before `producer.py` goes

**Decided 2026-10-29: option (a).** The other options are kept for the record.

| Option | Rule | Cost |
|---|---|---|
| **a. Enforced lineage** (recommended) | Each review op records the reviewer's producer (`harness`, `model`, `model_lineage`) from the process environment. `accept` is refused when the reviewer's lineage equals the lineage of the most recent `claim`/`set_status → in_progress` actor. Same refusal if the reviewer is the same `actor_id`. `human:*` reviewers are exempt. `attest_gate_waiver` stays the escape hatch | About 150 lines plus tests. Implementers must declare a lineage, otherwise their items can be accepted by anyone, and `doctor` warns. The 025 dispatcher's review jobs already know their model, so they stamp it reliably |
| b. Same actor only | Refuse `accept` only when the reviewer's `actor_id` equals the implementer's | About 40 lines. Weak, because two Claude sessions with different actor IDs pass |
| c. Honour system | Document that the gate records but doesn't enforce | Docs only. Matches today's behaviour |

Whichever option is chosen, fix the AGENTS.md text in the same change.
Lineage values: keep regista's closed vocabulary by vendoring the
`MODEL_LINEAGE_FAMILIES` tuple (D3), so existing `REGISTA_PRODUCER_MODEL_LINEAGE`
settings stay valid.

### D3. Vendor the small library pieces, then drop the dependency

- **Principal grammar.** Move it into `core/actor.py` as a regex plus a closed
  kind set, and test it against regista 0.7's own fixtures.
- **Error envelope.** Move it into `core/envelope.py` or `cli/common.py` as a
  local `ErrorCode` enum with the same string values. The JSON contract
  (`code` values) must not change, because skills and hooks match on them.
- **Secret-ref DSN resolution.** Check whether any machine's DSN is a ref
  (`doctor` already knows). If none is, drop ref support with a clear error.
  If one is, vendor the one provider in use.
- **Action delegation.** Check whether `AGENT_NOTES_ACTION_DELEGATION_PATHS`
  is set anywhere. If not, delete the feature.
- Then remove `regista-hraedon` from `pyproject.toml`, retire `SUITE.lock`
  and the `DEV_AGAINST` machinery in `make dev`, and remove the
  `regista-hraedon` editable source mapping.

Environment variable **names** stay as they are (`REGISTA_PRINCIPAL_ID`,
`REGISTA_PRODUCER_*`). Renaming them would touch every installed harness
config for no behavioural gain, and the repo's no-aliases rule would make it
a breaking flag day.

### D4. The op-log kernel stays as it is

It is the live store: 5,334 ops, verified fold, leases, seal. This plan
doesn't touch op hashing, envelopes, or the verifier. Dropping the
placeholder envelope (F2) is a possible follow-up. It needs its own check
that `op_id` hashing is unaffected (the envelope is outside the hash per
`kernel.py:122`) and that the verifier keeps accepting historical rows.

### D5. The schema is forward-only; drop tables last

No migration in Phases 1–3. Phase 4 adds one migration that drops the
outbox/regista-mirror tables, only after `doctor` confirms they've been empty
or unread on both machines for 14 days. Keep the 025 import marker
(`imported_from_regista`) because it's provenance.

### D6. Retire the Hindsight memory engine

The 2026-10-29 audit found the following.

- **agent-notes never writes to Hindsight.** `HindsightEngine.ingest` and
  `forget` have no callers outside the tests.
- **Recall finds nothing.** Recall queries banks named after the project slug
  (`agent_notes`, `global`). Those banks don't exist, so it returns 0 facts.
- **No successful store since 2026-08-04.** The MCP-connected harnesses on
  mvmcc02 stored 488 facts in the `default` bank between 2026-07-16 and
  2026-08-04. After that, 40 of 160 operations failed at fact extraction,
  because LM Studio on the Mac Studio had no model loaded or timed out.
- **The API pod is unstable.** It has restarted 29 times.
- **The API has no authentication.** It is reachable on the internal ingress.
- **The Plan 020 evaluation never ran.** Neither the Phase 0 bake-off nor the
  WI-4.1 shadow evaluation happened.

Decision: native is the only memory engine. Operations-side retirement is in
§7. In code, delete:

- `core/hindsight_adapter.py`;
- `core/http_transport.py`, if nothing else uses it;
- the engine-selection seam in `core/memory_engine.py`, `knowledge_provider.py`
  and `cli/memory_provider.py`, keeping only what `memory recall` and
  `search --federated` need natively;
- `AGENT_NOTES_MEMORY_ENGINE`, `HINDSIGHT_URL` and `HINDSIGHT_TENANT`.

A setting that names any engine other than `native` becomes a startup error,
not a silent fallback.

Plan 020's protocol idea isn't wrong. But a pluggable seam with one
implementation is overhead. If a learned engine is ever needed again, Plan 027
(memory roadmap) decides how to bring it back.

## 4. Inventory

Measured on `main` at `88db0d6`.

| Remove | Lines | Notes |
|---|---|---|
| `core/regista_face.py` | 412 | |
| `core/work_item/_regista.py` | 506 | After D2 lands natively |
| `core/outbox.py`, `cli/outbox.py` | 708 | Orient and Codex-Stop callers were already removed in 025 |
| `core/reconcile.py` | 366 | Outbox → regista replay (Plan 009). Git reconcile is `core/git_reconcile.py`, which stays |
| `core/projection.py`, `cli/projection.py` | 344 | Only `rebuild_from_regista` |
| regista branches in `core/note_model.py`, `core/memory_model.py`, `cli/memory.py` (`rebuild_from_regista`) | ~400 of 1,657 | Notes and memories become native-only. Confirm that `note_model`'s native path covers every write it mirrors today |
| `face_factory.get_face`, `regista_project_name`, `set_current_project` and the CLI project-schema probe (`cli/__init__.py:141-176`, `cli/common.py:106-111,333-338`) | ~120 | Keep `write_actor` and `default_actor`, moved to `core/actor.py` |
| `core/producer.py` | 86 | Replaced by the D2 native check |
| `core/coordinator.py` | 78 | Tier B coordinator stub. `doctor` keeps reporting `local-lease` |
| `scripts/migrate_to_regista.py`, `scripts/dedup_regista_source_identifiers.py`, `cli/admin.py` | 464 | One-shot scripts |
| `scripts/backfill_native_op_log.py` | 387 | One-shot. Ran at the 025 cutover |
| `cli/harness.py` regista cleanup | — | Keep the code that **removes** old `REGISTA_*` entries for one more release, then delete it |
| Tests | 47 of 101 files mention regista | Delete the regista-path tests. Convert the dual-path tests to native-only |
| `regista` config block, `regista_config()` | — | Unknown keys are already ignored |
| `core/hindsight_adapter.py`, engine selection in `core/memory_engine.py`, `knowledge_provider.py`, `cli/memory_provider.py` (D6) | ~800 of 1,995 | Keep native recall. Check `http_transport.py` for other users before deleting it |

Total: roughly 4,700 lines of source, plus the tests.

**Kept:** `kernel.py`, `verifier.py`, `_native.py`, `_seal`, leases,
`_queries`, `cross_project.py` (a Phase 3 audit decides; see below),
`messaging.py`, the dispatcher, review jobs, native memory and recall, and
the `invariants probe`.

## 5. Phases

| Phase | Content | Exit |
|---|---|---|
| **1** | D2 per the owner's decision: native independence check with tests on real Postgres (same actor, same lineage, human exempt, waiver), plus the AGENTS.md fix | A same-lineage `accept` is refused natively. The dispatcher's Claude review of a Codex-implemented item is accepted |
| **2** | D1: delete the write-through (§4, everything except library vendoring). `AGENT_NOTES_REGISTA_WRITES=1` → error | `make test` green. `rg -n "get_face\|_regista\|regista_face" src` returns nothing. Deploy to mvmcc02 and Windows; `doctor` clean on both |
| **2b** | D6: delete the Hindsight engine code. Independent of Phase 2 | `rg -in hindsight src` returns nothing. `memory recall` and `search --federated` still pass on native |
| **3** | D3: vendor, drop the dependency, retire `SUITE.lock`. Audit `cross_project` usage (rows in `cross_project_*` and `request`/`wait` ops in the last 90 days) and record a keep/remove decision here | `uv pip install -e .` in a fresh venv with no regista. Error-envelope JSON is byte-identical for one sample error per `ErrorCode` |
| **4** | D5: drop the dead tables after the 14-day window. Delete the harness cleanup code. Rewrite the README and AGENTS.md identity sections around the native model | Migration applied with a backup taken first. Docs no longer describe v6 signing as live |

Phases 1 and 2 can be reviewed together, but Phase 1 must merge first, so
there's never a release where `producer.py` is gone and nothing replaces it.

## 6. Risks

- **Hidden regista reads.** Some path may still read through the face
  without the gate being on. Phase 2's `rg` exit, and running a deployed
  week with `REGISTA_DSN` unset on both machines, are the checks.
- **Error-code drift** breaking skills or hooks that match on `code` values
  (Phase 3 exit).
- **D2 friction.** Sessions that don't declare a lineage leave their items
  accepted by anyone. Option (a) logs that case, and `doctor` lists sessions
  without a lineage, so the gap is visible rather than silent.

## 7. Hindsight operations retirement (2026-10-29)

1. **Done 2026-10-29:** removed the Hindsight MCP entries from mvmcc02's Claude,
   Codex and OpenCode configs, and set `AGENT_NOTES_MEMORY_ENGINE=native`.
   Backups of each file sit next to it with the suffix `.bak-20261029-hindsight`.
2. **Done 2026-10-29:** exported to `/mnt/bulk/hindsight-retirement-20261029/`
   (mode 700):
   - per bank: `memories.jsonl` (488 / 11 / 2 / 0 facts), plus documents,
     tags, config, profile and stats;
   - a full `pg_dump -Fc` of the `hindsight` database
     (`hindsight-db-20261029.dump`, 21 tables with data);
   - `SHA256SUMS`.

   The API's `/export` endpoint returns only `{"version":"1"}`, so the
   paginated `memories/list` is the API-level export. This archive is for
   later review, not for re-import.
3. **Done 2026-10-29:** scaled `hindsight-api` and `hindsight-control-plane` to
   0 replicas; the API now answers 503. Afterwards, `agent-notes
   memory-provider doctor` reports `native`/`healthy`. A recall for "release
   flow" returns 10 results; the same query against Hindsight returned 0.
4. **After 2026-11-12, if nothing broke:**
   - `helm uninstall` the release and delete the namespace and its model-cache
     PVC;
   - drop the `hindsight` database and role on mvmpostgres01; the dump in
     step 2 is the final copy;
   - remove the ingress DNS name.
