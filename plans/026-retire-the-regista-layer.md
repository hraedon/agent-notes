# Plan 026 — Retire the regista layer and Hindsight: one native path, an enforced review gate

**Status:** Accepted 2026-10-29. The owner chose D2 option (a) and retired
Hindsight (D6). Revised the same day after two independent reviews,
GPT-6.1-sol (Codex) and Gemini 4 Argon (agy); see §8.
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

**Decided 2026-10-29: option (a), enforced lineage.** The first draft of the
rule wouldn't have enforced anything (§8 R1–R3). The revised rule follows.

**Trust boundary.** This is cooperative workflow enforcement. It stops an
agent from approving its own work by accident or out of habit. It doesn't
stop a determined operator, because actor and model identity are
self-asserted through the environment and the hooks. The docs must say this
plainly.

**D2.1 Record who did the work.** Every authored work-item op carries a
`producer` object **inside its payload**, so it's covered by the content
hash: `create`, `claim`, `set_status`, `set_field` and `close`.

```json
{"harness": "codex", "model": "gpt-6.1-sol", "lineage": "openai",
 "session_key": "shodan/codex/...", "source": "env|session|unknown"}
```

The producer is resolved in this order:

1. `AGENT_NOTES_MODEL` in the process environment. `REGISTA_PRODUCER_MODEL`
   is still read during the transition. The lineage is always derived from
   the model and is never declared separately, as implemented: a declared
   lineage that disagrees with the model would be a false identity claim.
   The repo's identity-override test already forbids
   `AGENT_NOTES_MODEL_LINEAGE`.
2. The calling session's registered model. Plan 025 D5's `resolve_session()`
   finds the session. A new `agent_sessions.model` column is filled by
   SessionStart/per-prompt hooks from the harness's own hook input. Phase 1a
   checks which harnesses supply `model`.
3. `unknown`.

**D2.2 Lineage is coarse: the vendor family.** `claude-*` → `anthropic`,
`gpt-*` and `o<digit>*` → `openai`, `gemini-*` → `google`, and likewise
`glm` → `zhipu`, `kimi` → `moonshot`, `minimax`, `nemotron` → `nvidia`,
`deepseek`, `qwen`, `longcat` → `meituan`. The mapping is data, not branching
code (AGENTS.md decision 5). Regista's finer vocabulary, where `claude-opus`
and `claude-sonnet` count as different lineages, is the wrong grain for "an
independent model looked at this". Regista lineage values still map to a
family, so old settings keep working.

**D2.3 The implementer set** is every non-system author of an authored op on
the item since its last `reject` or `request_changes` transition (or since
creation), excluding the review transitions themselves.

**D2.4 Gated transitions.** Any move out of `in_review` or
`in_human_review` to `in_human_review`, `done` or `closed` is a positive
verdict, whichever command makes it:

- `review pass` and `review accept`;
- `close`, including `close --force`;
- `update --status`, including `--force`.

Moves out of review are marked with the verdict they amount to
(`review_transition`), so they never count as implementation, and a bounce
back to `in_progress` resets the implementer set. Three more rules close the
remaining bypasses:

- an approving update can't also edit the item;
- `release` only reopens a `claimed` item;
- `kernel.reconcile_entity` refuses to reconcile an item while it's in
  review. It has no callers today, and a merge records only merged state,
  not who changed what.

Plan 025 D7 deliberately lets `close` complete an item that never entered
review, and that stays ungated: review is opt-in.

**D2.5 The rule.**

- The reviewer must have a known lineage.
- The transition is refused when the reviewer's lineage matches any known
  implementer lineage.
- When an implementer's lineage is unknown (all of today's 143 items in
  review), the reviewer's `actor_id` must differ from that implementer's.
  The op records `independence: "unverified-legacy"`.
- `human:*` reviewers skip the lineage check. They must still differ from
  every implementer's actor.
- Refusals name the matching implementer and point to the escape hatches.

**D2.6 Escape hatch, recorded on the op.** `--same-lineage-acknowledged
"<reason>"` on `review pass|accept` passes the gate. The reason is required
and is recorded on the op. `attest-gate` is unchanged: it records a waiver
after completion, for items completed before this gate existed. A separate
pre-completion waiver would only duplicate the acknowledgement.

**D2.7 Delegated reviews.** `work-item review pass|accept --attempt <id>`
takes the reviewer identity from a `review_delegation_attempts` row in
`result_validated` state for the same item and head:

- `harness`, `model_identity` → lineage;
- `identity_assurance`.

It does not take it from the running process. Without this, Plan 025's
headline flow fails: the Codex implementer that receives Claude's verdict
would be refused. Brief-mode attempts (no work item) can't be used this way.

Work items don't record git heads, so the attempt can't be bound to the head
it reviewed. Instead, any content edit to the item after the attempt was
requested makes the attempt stale. Code pushed without touching the item
isn't detected; that's a judgement call, recorded here.

**D2.8 Lands with:**

- the `adversarial-review` skill, its bundled copies and the OpenCode
  reviewer agents;
- CLI help;
- `data/cli-manifest.json`;
- AGENTS.md §"Canonical v6 actor and producer identity".

The skill currently says acceptance needs regista.

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

### D5. The schema is forward-only; drop columns, not tables

There are no regista tables to drop. The regista outbox is **per-user JSONL
files** (`core/outbox.py:66-84`), not a table. Migrations 810 and 830 added
mirror columns and indexes to the live `work_items` and `memories` tables
(`regista_note_id`, `pending_sync`, and the 810 columns).

- **Phase 2:** `doctor` inspects each host's outbox files. Non-empty files
  are archived to NFS before the outbox tooling is deleted.
- **Phase 4:** one migration drops only the mirror columns and indexes,
  after `doctor` reports `pending_sync = true` on zero rows on every host.
  The rows are checked directly; there's no "unread for 14 days" heuristic.
- The 025 import marker (`imported_from_regista`) stays, because it's
  provenance.

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
- the engine selection in `core/memory_engine.py` and `cli/memory_provider.py`.
  Keep what `memory recall` and `search --federated` need natively;
- `AGENT_NOTES_MEMORY_ENGINE`, `HINDSIGHT_URL` and `HINDSIGHT_TENANT`.

`core/http_transport.py` **stays**: it's the HMAC client used by `bridge.py:35`
and `trigger_loop.py:45`. `knowledge_provider.py` **stays**: it provides live
native browsing and search, not engine selection.

A setting that names any engine other than `native` becomes a startup error,
not a silent fallback.

Plan 020's protocol idea isn't wrong. But a pluggable seam with one
implementation is overhead. If a learned engine is ever needed again, Plan 027
(memory roadmap) decides how to bring it back.

## 4. Inventory

Measured on `main` at `88db0d6`. Corrected after review (§8 R4).

| Remove | Lines | Notes |
|---|---|---|
| `core/regista_face.py` | 412 | |
| `core/work_item/_regista.py` | 506 | After D2 lands natively |
| `core/outbox.py`, `cli/outbox.py` | 708 | Also remove the callers: `cli/breadcrumbs.py:35` and lines 490–496, `scripts/doctor.py:25`, `face_factory.py:53` (`OutboxAwareFace`). Archive outbox files first (D5) |
| `core/reconcile.py` | 366 | Outbox → regista replay (Plan 009). Git reconcile is `core/git_reconcile.py`, which stays |
| `core/projection.py`, `cli/projection.py` | 344 | Callers: `core/work_item/_common.py:17` (top-level import on **every** work-item command), `scripts/doctor.py:25`, `scripts/backfill_native_op_log.py:42`. Move anything still used (e.g. `state_to_status`) first |
| regista branches in `core/note_model.py`, `core/memory_model.py`, `cli/memory.py` (`rebuild_from_regista`) | ~400 of 1,657 | Notes and memories become native-only. Confirm that `note_model`'s native path covers every write it mirrors today |
| `face_factory.get_face`, `regista_project_name`, `set_current_project`, the CLI project-schema probe (`cli/__init__.py:141-176`, `cli/common.py:106-111,333-338`) and `face_factory.py:28` (`regista._connection.validate_project_name`, reached by every `--scope vm|global` memory command) | ~120 | Keep `write_actor` and `default_actor`, moved to `core/actor.py` |
| `scripts/doctor.py` regista checks | — | Today `doctor --json` reports `degraded` when `regista_reachable` is `None` (`doctor.py:1076-1080`). Fix this **before** any `REGISTA_DSN` removal |
| `core/producer.py` | 86 | Replaced by D2.1 |
| `core/coordinator.py` | 78 | Tier B coordinator stub. `doctor` keeps reporting `local-lease` |
| `scripts/migrate_to_regista.py`, `scripts/dedup_regista_source_identifiers.py`, `cli/admin.py` | 464 | One-shot scripts |
| `scripts/backfill_native_op_log.py` | 387 | One-shot. Ran at the 025 cutover |
| `data/cli-manifest.json`, skills | — | Remove `outbox`, `projection`, `admin` and `memory-provider` entries, and the regista text in skills |
| `cli/harness.py` regista cleanup | — | Keep the code that **removes** old `REGISTA_*` entries for one more release, then delete it |
| Tests | 39 of 97 files mention regista | Delete the regista-path tests. Convert the dual-path tests to native-only |
| `regista` config block, `regista_config()` | — | Unknown keys are already ignored |
| `core/hindsight_adapter.py`, engine selection in `core/memory_engine.py` and `cli/memory_provider.py` (D6) | ~700 | Keep native recall |

Total: roughly 4,600 lines of source, plus the tests.

**Kept:** `kernel.py`, `verifier.py`, `_native.py`, `_seal`, leases,
`_queries`, `cross_project.py` (a Phase 3 audit decides), `messaging.py`,
`http_transport.py`, `knowledge_provider.py`, the bridge, the trigger loop,
the dispatcher, review jobs, native memory and recall, and the
`invariants probe`.

**Deployed hosts:** mvmcc02, Windows (`shodan`) and `mvmhermes01` (Hermes,
Plan 025 §12), plus the Mac if agent-notes is installed there. Every phase
that changes behaviour redeploys **all** of them and restarts the
dispatchers. Mixed versions are a bypass: an older install neither stamps
producers nor enforces D2.

## 5. Phases

| Phase | Content | Exit |
|---|---|---|
| **1a** | Check what each harness's hook input supplies for `model`: Claude, Codex, OpenCode, agy, Hermes | A matrix in §9. A harness with no model falls back to env or `unknown` |
| **1** | D2.1–D2.8. Migration `860`: `agent_sessions.model`, plus the `model_lineages` vocabulary table. Real-Postgres tests cover: same lineage refused at pass, accept, close and update→done; reviewer lineage unknown refused; legacy implementer with a different actor allowed and recorded; same actor refused; human exempt from lineage but not actor; acknowledgement and waiver recorded; `--attempt` path; `reject` doesn't make the reviewer an implementer | Same-lineage completion is refused on every path. Plan 025's headline flow, a Claude review of a Codex item applied by the Codex session with `--attempt`, succeeds. Skills and docs updated. All hosts redeployed |
| **2** | D1: delete the write-through (§4, everything except library vendoring), including the doctor fix and caller cleanup. `AGENT_NOTES_REGISTA_WRITES=1` → error | `make test` green. `grep -rnE "get_face\|_regista\b\|regista_face" src` is empty. Fresh-venv smoke run of the CLI, every hook entry point, `agent-notes-bridge`, `agent-notes-dispatch` and `doctor --json`. Then a week with `REGISTA_DSN` unset on all hosts, `doctor` clean |
| **2b** | D6: delete the Hindsight engine code. Independent of Phase 2 | `grep -rni hindsight src` is empty. `memory recall` and `search --federated` still pass on native |
| **3** | D3: vendor, drop the dependency, retire `SUITE.lock`. Audit `cross_project` usage (rows in `cross_project_*` and `request`/`wait` ops in the last 90 days) and record a keep/remove decision here | `uv pip install -e .` works in a fresh venv with no regista. Error-envelope JSON is byte-identical for one sample error per `ErrorCode` |
| **4** | D5: drop the mirror columns and indexes. Delete the harness cleanup code. Rewrite the README and AGENTS.md identity sections around the native model | Migration applied with a backup taken first. Docs no longer describe v6 signing as live |

Phases 1 and 2 can be reviewed together, but Phase 1 must merge first, so
there's never a release where `producer.py` is gone and nothing replaces it.

## 6. Risks

- **Hidden regista reads.** Some path may still read through the face
  without the gate being on. Phase 2's grep, the fresh-venv smoke run and
  the week with `REGISTA_DSN` unset are the checks.
- **Error-code drift** breaking skills or hooks that match on `code` values
  (Phase 3 exit).
- **D2 friction.**
  - Sessions with no resolvable model can't pass or accept.
  - The refusal says how to fix it: set `AGENT_NOTES_MODEL`, or use
    `--attempt`.
  - `doctor` lists registered sessions with no model.
- **Self-asserted identity.** See the D2 trust boundary. If that's ever not
  enough, the fix is a per-host DB role, with the trigger stamping the host
  (Plan 025 §8), not more client logic.

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

## 8. Review record (2026-10-29)

Reviewers: GPT-6.1-sol (`codex exec`, read-only) and Gemini 4 Argon medium
(`agy`, sandboxed print mode). Their verdicts on the first draft: Plan 026
"rework" and "approve with changes". Raw reviews are on mvmcc02 under
`~/reviews/plans-026-027/`. Adopted:

- **R1.** `close`, and `update --status done` from review, bypassed a check
  placed only on `accept`; `adversarial_pass` was unchecked (both reviewers,
  verified at `_native.py:195-214, 295-339`). → D2.4.
- **R2.** No op records lineage. Stamping only review ops can't recover who
  implemented; `reject`/`request_changes` write `in_progress` and would make
  the reviewer the "implementer" (both reviewers). → D2.1 and D2.3.
- **R3.** Identity is self-asserted. A human exemption via the env is
  trivially reachable. Producer evidence must be inside the hashed payload
  (GPT). → D2 trust boundary and D2.1. Delegated review attempts never write
  to the op-log, so the headline flow would be refused (Argon). → D2.7.
  A shared host-level actor ID would block review between harnesses on the
  same host (Argon). → the D2.5 rule is lineage-first.
- **R4.** Inventory missed live callers (`_common.py:17`, `breadcrumbs.py:35`,
  `doctor.py:25`, `face_factory.py:28`, the CLI manifest, skills). It wrongly
  listed `http_transport.py`, and `knowledge_provider.py` has no engine seam
  (both reviewers, verified). → §4.
- **R5.** The outbox is JSONL files, not tables; 810/830 added columns to
  live tables (GPT). → D5.
- **R6.** `mvmhermes01` is a deployed host, and skills must change with D2
  (GPT). → §4 deployed hosts, D2.8.
- **R7.** `rg` isn't installed on mvmcc02 (Argon). → exits use `grep`.

**My own finding during revision:** no host sets `REGISTA_PRODUCER_*`, and
regista's lineage vocabulary splits Claude Opus from Claude Sonnet and has
no Gemini family. → D2.1's session-model source and D2.2's coarse vendor
lineage.

## 9. Harness model matrix (Phase 1a, 2026-10-29)

| Harness | Model source in hook input | Notes |
|---|---|---|
| Codex | `model` on **every** event | From the official hooks docs ("Codex-specific extension. Active model slug.") |
| agy (Antigravity) | `modelName` on every event | From the Plan 025 Phase 0 spike (`docs/spikes/025-phase0.md`) |
| Hermes | `extra.model` on `pre_llm_call` | `hooks/hermes.py` contract notes |
| Claude Code | `model` on **SessionStart only**, "not always" | Model switches arrive as `PostModelSwitch.to_model`, which isn't wired. There's no model environment variable. A Claude session whose SessionStart omitted the model, or that switched models, needs `AGENT_NOTES_MODEL` to review |
| OpenCode 2 | Not wired | The plugin could pass the model from its chat hooks. Until then, OpenCode reviewers set `AGENT_NOTES_MODEL` (their agent definitions say so) |

## 10. Implementation log

### 2026-10-29: Phase 1 (D2)

- **Migration 860.** Adds `agent_sessions.model` and the `model_lineages`
  vocabulary (10 vendor families).
- **`core/work_item/_independence.py`.** Covers producer resolution,
  implementer derivation, the check, and delegated attempts.
- **Stamping.** Native `create`, `set_field`, `set_status`, `claim`, `close`
  and the gate waiver stamp `producer` into the hashed payload.
- **Gated paths.** `review pass/accept`, `close` from review, and `update`
  into `in_human_review`/`done` from review. A gated move made through
  `update` or `close` is marked with the equivalent `review_transition`, so
  it's review bookkeeping, not implementation.
- **Delegated attempts.** `--attempt` requires a `result_validated` attempt
  for the same item, with an `accept` verdict and non-degraded identity
  assurance. It then moves the attempt to `transition_applied` with the
  native op ID in the same transaction, so the attempt can be used once.
- **D2.6 as built.** `--same-lineage-acknowledged` now takes a REASON, which
  is required and recorded. `attest-gate` is unchanged and still applies only
  after completion; the acknowledgement covers the pre-completion case.
- **Hooks.** Hooks register the session model (Codex, agy, Hermes, Claude
  SessionStart). `messaging` returns `model` in session rows.
- **Docs.** Updated the skill, its bundled copy, the four OpenCode reviewer
  agents, the file-breadcrumb skill, README and AGENTS.md.
  - `adversarial-reviewer-minimax-m3.md` actually runs
    `ollama-cloud/nemotron-3-ultra`. Its identity line now declares that
    real model, and the file name mismatch is left for the owner.
- **Implementation review (GPT-6.1-sol, read-only, "rework").** It found
  eleven issues. All but one were fixed, each with a test:
  - forced moves and the `closed` alias bypassed the gate;
  - `release` could take an item out of review;
  - `reconcile_entity` could merge an approval in;
  - an approving update could also add new work;
  - delegated attempts weren't checked for staleness;
  - every `service:*` author was ignored, even one reporting a model;
  - seal stubs made the first toucher an implementer;
  - bookkeeping detection was too broad;
  - plain-update bounces didn't reset the implementer set;
  - producer lookups could abort a write (now run in a savepoint).

  The pre-completion waiver was resolved as a spec change (D2.6).
- **Round 2 (same reviewer, "rework": 7 fixed, 4 partial, 3 new).** All
  addressed, each with a test:
  - `reconcile_entity` reads the folded local status, not the last
    `set_status` op.
  - Model-less `service:*` authors count, with an unknown lineage, under the
    legacy actor rule.
  - The pre-D2 review-notes adjacency heuristic applies only to ops without a
    `producer` block. Every op since D2 has one, and marks review notes
    explicitly.
  - Delegated-attempt staleness looks at the item's whole content history.
    It compares op lamports against a new `item_lamport` watermark, which
    `plan_attempt` records in migration 860, instead of transaction-start
    timestamps.
- **Round 3 ("rework": two remaining edges, both fixed with tests).**
  - Reconcile is refused for items in review, and `merge` ops count as
    authored content.
  - Attempts with no `item_lamport` (planned before migration 860) are
    refused as unverifiable rather than judged by timestamp.
  - The reviewer found no other blocker or major issue.
- **Deployed 2026-10-29 (`d6da05f`).**
  - Database: backed up to
    `/mnt/bulk/agent-notes-backups/agent_notes-pre-860-20261029T194546Z.dump`,
    then migration 860 was applied.
  - mvmcc02, Windows (`shodan`) and `mvmhermes01` reinstalled from `main`.
    - Windows now installs from `C:\projects\agent-notes`, mvmcc02 from
      `/projects/agent-notes`, and mvmhermes01 from `~/agent-notes-025`
      (moved to `main`).
    - mvmhermes01 needed `uv tool install --reinstall`. With an unchanged path
      and version, uv reuses its cached build.
    - On Windows, `Stop-ScheduledTask` doesn't stop the dispatcher's wrapper
      script, and the running dispatcher locked the tool directory. The first
      reinstall failed partway; it took about 2 minutes to recover.
  - Skills refreshed on every harness.
    - agy was deliberately **not** newly wired on Windows: whether agy hooks
      work there is still unverified (Plan 025 Phase 0).
  - Hindsight configuration was also still live on mvmhermes01, which the
    D6 audit missed. It's now retired there too (same backups suffix).
  - Dispatchers restarted.
  - **Production checks.**
    - A Codex session's hook registered `gpt-6.1-sol` in `agent_sessions.model`.
    - Smoke item WI-083:
      - filed by a GPT implementer;
      - a same-family pass was refused with `VALIDATION_FAILED`, naming the
        implementer;
      - a Claude pass moved it to `in_human_review`;
      - the item was then deleted.
    - `doctor` is healthy on mvmcc02 and mvmhermes01.
    - Windows showed the doctor's Codex probe failing with
      `FileNotFoundError` (an npm `.cmd` shim launched by bare name). That is
      fixed in the commit after the deploy.
- **Latent bug surfaced and fixed.** `update`'s `set_status` payload was just
  `{"status": ...}`. Revisiting a status, for example resubmitting for review
  after a bounce, by the same actor collided on the content-addressed op ID.
  It now carries `changed_at`, as `close` already carried `closed_at`.
  `claim` and `release` had the same flaw for an actor who claims again, and
  now carry `claimed_at` and `released_at`.
- **Bug found by the tests.** `kernel._get_entity_ops` doesn't select
  `actor_id`, so a first version of the check saw no implementers and passed
  everything as `no-recorded-implementers`. `_independence` now has its own
  query, and the tests pin the implementer set's contents.

### 2026-10-29: Phases 2 and 2b (D1, D6), deployed

- **Deleted.** `core/regista_face.py`, `core/work_item/_regista.py`,
  `core/outbox.py`, `cli/outbox.py`, `core/reconcile.py`, `core/projection.py`,
  `cli/projection.py`, `core/producer.py`, `core/coordinator.py`,
  `core/note_model.py`, `core/face_factory.py`, `cli/admin.py`, the three
  one-shot scripts, `core/hindsight_adapter.py` and `cli/memory_provider.py`,
  plus the regista-only tests. `memory rebuild-from-regista` and
  `memory check-drift` went with `note_model`: memories were already
  native-only whenever the face was off.
- **`face_factory` was moved, not shimmed.** `write_actor` and `default_actor`
  live in `core/actor.py`; callers import `actor` directly.
  `RegistaConfig`/`regista_config()` are gone too: nothing read them once the
  face was deleted. `REGISTA_DSN`, `REGISTA_KEY_PATH` and
  `REGISTA_REQUIRE_SSL` are no longer read.
- **Retired settings are errors.** `AGENT_NOTES_REGISTA_WRITES` or
  `regista.writes_enabled` raises `RetiredSettingError`
  (`RETIRED_SETTING`, pointing at `docs/spikes/025-regista-cutover.md`) from
  `actor.write_actor` and from memory add/update/delete, so every write path
  refuses; reads are unaffected. `AGENT_NOTES_MEMORY_ENGINE` other than
  `native` is a `ValueError` from `get_engine`; `memory recall` reports
  `ENGINE_UNAVAILABLE`, and `search --federated` and `orient --recall` name it
  in their warnings.
- **Doctor.** No `regista` block, no `regista_reachable`, `regista_face` or
  `secrets_backend` checks, and no `degraded` status (`degraded` stays in the
  JSON, always `false`). `coordination_mode` is a constant `local-lease`. New
  `retired_settings` check fails while the writes gate is on;
  `memory_provider` fails on a non-native engine setting.
- **D5 outbox check.** New read-only `legacy_outbox` check. It looks at the old
  queue directory (`AGENT_NOTES_OUTBOX_DIR`, else
  `platformdirs.user_state_dir("regista")/outbox`) and fails on any non-empty
  `*.jsonl` file under it, saying to archive the directory. It only lists
  files and checks sizes; it never reads or replays ops.
  - **Evidence, 2026-10-29 (coordinator).** No outbox directory on any of the
    three deployed hosts, so nothing needed archiving. Checked
    `/home/itadmin/.local/state/regista/outbox` on mvmcc02 and mvmhermes01,
    and
    `C:\Users\merrittp\AppData\Local\regista\regista\outbox`
    on Windows, where the regista state dir holds only `keys.json`.
- **Native seal coverage.** The seal and lazy-seal tests from the deleted
  backfill test file moved to `tests/test_native_seal.py`, with chainless
  rows built by direct SQL. Only the backfill's dry-run, report and
  import-unmirrored cases, and its own race test, were dropped.
- **Kept on purpose.** A hidden `agent-notes outbox ...` no-op for the
  per-repo Claude `Stop` hooks that still called it (removed in Phase 3, once
  those hooks were cleared). The protocol in `core/memory_engine.py` stays,
  because `native_engine`, `orient` and `search` use its types.

### 2026-10-29: Phases 2/2b deployed (`f5762a6`), with UTC log stamps (`f033221`)

- **Before deploying.**
  - No host had a legacy outbox, so there was nothing to archive (D5).
  - `AGENT_NOTES_REGISTA_WRITES=1` was still set, alongside `REGISTA_DSN`
    and `REGISTA_KEY_PATH`, in mvmhermes01's `~/.claude/settings.json` env.
    It would have made every write there fail with `RETIRED_SETTING`.
    Removed; the backup is `settings.json.bak-20261029-regista`.
    `install-harness claude` was **not** run there: it would have newly
    installed the whole Claude harness on that host.
- **Deploy.** Reinstalled with `--reinstall` on mvmcc02, Windows and
  mvmhermes01, refreshed the harnesses, and restarted the dispatchers. On
  Windows, the wrapper and dispatcher processes were stopped first.
- **Checks.**
  - `doctor`: `healthy`, `degraded: false` on mvmcc02 and mvmhermes01.
  - Windows fails only `codex_harness`. `codex plugin list` itself fails
    there, because two configured Codex marketplaces point at directories
    with no manifest; that's local config.
  - Dispatcher logs now read `2026-10-29T20:48:10.071Z`.
  - Production smoke test: a work-item file/update/delete cycle (WI-084), a
    memory add/delete cycle, and `memory recall`, which used the native
    engine and returned 10 results.
  - The Hermes `pre_llm_call` hook returns orientation with Hermes's own
    `PYTHONPATH` set.
- **Still open from Phase 2's exit:**
  - the week with `REGISTA_DSN` unset (no code reads it any more).

### 2026-10-29: Phase 3 (D3), not yet deployed

- **Vendored.**
  - **Principal grammar** → `core/actor.py`: `PRINCIPAL_KINDS` (closed set),
    the subject regex, `principal_id_problem` (non-raising) and
    `validate_principal_id`. It copies regista 0.7.2's `_principals` rules,
    reasons, codes, messages and detail exactly. The messages still cite
    `TRUST-DOMAIN.md` and the alias path; they were kept verbatim so the
    envelope doesn't drift, and Phase 4's docs rewrite can revisit them.
    - regista's own grammar cases are ported in
      `tests/test_principal_grammar.py`.
    - Before uninstalling regista, a differential run compared 3,403 values
      (fuzzed kinds and subjects, length edges, non-ASCII, non-strings). Verdict,
      code, message and detail matched on every one.
  - **Error envelope** → new `core/errors.py`: `ErrorCode` (`StrEnum`, the 28
    codes agent-notes emits, value = name) and `AgentNotesError` (regista's
    `RegistaError` shape: `code`, `message`, `detail`, `[CODE] message`).
    `core/envelope.py` is the op-log DSSE module, so the vocabulary went to a new
    file. `emit_error` call sites keep their string literals; a member and its
    literal serialize identically.
    - `tests/fixtures/cli_error_envelopes.json` holds the `--json` stdout
      captured at `bfff823` with regista installed, one sample per code. The
      four identity and retired-setting codes were captured end to end through
      `_dispatch` and the real actor path.
    - `tests/test_error_envelope.py` compares post-change output byte for byte.
      It also scans `src/` with `ast` and fails if a string code passed to
      `emit_error`/`ScopeError`/`_CheckoutError`, or a `code =` on an
      `*Error` class, is missing from `ErrorCode`.
  - `test_lifecycle_consistency.py` read regista's `canonical_workflow_yaml()`.
    Its 14 non-amend transitions (workflow `canonical`, version 3) are now
    frozen in the test.
- **Deleted, with host evidence** (read-only, 2026-10-29: mvmcc02 and
  mvmhermes01 over SSH, plus Windows `shodan`; suite.env, `/etc/agent-suite`,
  Claude, Codex, OpenCode and Hermes configs, agent-notes `config.json`, and the
  Windows user and machine environment).
  - **Action delegation.** `AGENT_NOTES_ACTION_DELEGATION_PATHS` appears on no
    host, and since Phase 2 nothing consumed the loaded credentials. The loader,
    `DelegationConfigurationError` and `Actor.action_delegation_credentials` are
    gone; the setting is ignored.
  - **Secret-backend refs.** Every DSN is a literal `postgresql://` URL: the
    `dsn` in `config.json` on all three hosts, and `AGENT_NOTES_DSN` in
    mvmhermes01's suite.env (not set elsewhere). `core/secrets.py` and its
    tests are deleted. A DSN of the form `env:`, `file:`, `literal:`, `vault:`
    or `azure:` now raises `RetiredSettingError` (`RETIRED_SETTING`), naming
    the prefix but not the value. It's a `RuntimeError`, so callers that
    already handled DSN errors still do. `materialize_key_manifest` had no
    caller left.
  - The structlog-to-stderr setup in `cli.main`: structlog came only with
    regista.
  - `SUITE.lock`, `scripts/dev-install.py`, `scripts/suite_lock.py`,
    `scripts/check_suite_lock.py`, `docs/develop-against-lock.md`,
    `tests/test_develop_against_lock.py`, the `[tool.uv.sources]` mapping, the
    `packaging` test extra, and the `DEV_AGAINST`/`check-suite-lock` Makefile
    targets. `make dev` is `uv pip install -e ".[test]"`, and both CI lanes
    `pip install` the same way.
  - `uv.lock` regenerated: it drops regista, structlog and jsonschema's
    stack. uv 0.12 also rewrote the lock format (revision 3 → 5).
  - `PyYAML` added as a declared dependency. `core/bc_files.py` imports it at
    module level but got it only transitively.
- **The `outbox` stub is removed.** All 51 per-repo Claude `Stop` hooks that
  ran `agent-notes outbox reconcile` were cleared on 2026-10-29: the source fix
  was pushed in 6 repos, and the rest were edited locally. A fresh scan of
  mvmcc02, mvmhermes01 and Windows finds none, and no harness config, unit,
  crontab or shipped hook, plugin, skill or integration calls `outbox`.
  `agent-notes outbox ...` now exits 2 like any unknown subcommand.
  `init`'s `_remove_claude_stop_reconcile_hook` and doctor's `legacy_outbox`
  check stay as safety nets. Follow-ups:
  - openbia, sluice and switchboard are archived on GitHub, so their history
    keeps the hook; they were edited locally only.
  - diagnostics-architect-curriculum (git.hraedon.com) was edited locally but
    not pushed: mvmcc02 has no credentials for it.
- **`cross_project` audit** (production, SELECT-only, read-only transaction):
  - `cross_project_ops`, `cross_project_work_items`, `cross_project_links`,
    `cross_project_freshness`: **0 rows each**. The view
    `cross_project_reverse_edges_v` is empty too.
  - `op_log` holds 5,344 ops, from 2026-06-08 to 2026-10-29: `create` 2,296,
    `snapshot` 2,189, `close` 440, `set_field` 223, `set_status` 189,
    `claim` 6, `heartbeat` 1. There are **no `request` or `wait` ops**,
    in the last 90 days or ever.
  - **Recommendation: remove**, in its own phase, after Phase 4. The P3
    surface has never been used in production. It is `core/cross_project.py`
    (517 lines), `core/work_item/_cross_project.py`, the
    `work-item request|wait|link-cross|export-ops|ingest-ops` commands, the
    cross-project leg of `ready`, and its references in `doctor`,
    `trigger_loop` and `links`, plus `tests/test_cross_project.py` (781 lines).
    The removal is not done here. It needs its own caller audit, a CLI-manifest
    and skills update, and a forward migration that drops the four tables and
    the view after a backup (D5's rule).
- **Exit evidence.**
  - Fresh venv (`.venv-fresh`, never had regista): `uv pip install -e ".[test]"`
    succeeds; `regista` and `structlog` are not importable. 89 of 90 modules
    import. The exception is `scripts/import_reflections`, which needs
    `python-frontmatter`; that gap predates this phase and is the same with
    regista installed.
  - `--help` works for every console script. `agent-notes-bridge` and
    `agent-notes-trigger-loop` exit 1 on missing configuration with or without
    regista. `orient --json` with a `vault:` DSN prints the `RETIRED_SETTING`
    envelope.
  - `grep -rn regista src/` finds no import. What's left is environment
    variable names, comments, docs strings, `regista.*` config-key cleanup in
    `cli/harness.py`, and agent-notes' own `reject_regista_writes` /
    `regista_writes_requested` helpers.
  - Full suite in the fresh venv (`HOME=/tmp/emptyhome`): **1555 passed**,
    2 skipped, and 1 failed: the known
    `test_codex_doctor.py::test_plugin_probe_limits_query_to_configured_sources`
    (7m43s). The baseline was 1501 passed. The difference is the new grammar,
    envelope and DSN-ref tests, less the deleted secrets, lock and delegation
    tests.
  - `ruff check src tests` is clean, and `uv lock --check` passes.
- **Deploy (not done).** mvmcc02's uv receipt records
  `requirements = [agent-notes-hraedon (directory), regista-hraedon==0.7.2]`
  and `no-sources = true`; `--no-sources` was only needed to bypass the
  `../regista` editable mapping. From this commit on, install with
  `uv tool install --reinstall <checkout>`, with no `--with` and no
  `--no-sources`. A new `uv tool install` replaces the receipt's requirements.
  `--reinstall` is needed because uv reuses its cached build when the path and
  version are unchanged. Check with `uv tool list --show-with`: there should be
  no `[with: regista-hraedon==0.7.2]`. Restart the dispatchers as usual. Mixed
  versions are harmless here: no deployed host relies on anything Phase 3
  removes.

### 2026-10-29: Phase 3 deployed (`359c14d`)

- **Install command.** On all three hosts it is now
  `uv tool install --force --reinstall <checkout>`, with no `--no-sources` and
  no `--with regista-hraedon`.
- **regista is gone.** It can't be imported in any host's tool environment.
- **`doctor`.** `healthy` on mvmcc02 and mvmhermes01. Windows fails only the
  known `codex_harness` (two local Codex marketplace paths with no manifest).
- **Production smoke test.**
  - A write succeeded (WI-085, then deleted).
  - A malformed `AGENT_NOTES_ACTOR_ID` returns the same
    `PRINCIPAL_ID_UNGRAMMATICAL` envelope as before.
  - `agent-notes outbox reconcile` now exits 2, as intended.
- **Outbox Stop hooks.** All 51 were cleared before the stub was removed.
  - **Pushed at source** to 6 repos: ad-steward, agent-suite, frontier-lag,
    modern-moo2, socratic-specification and vitrine.
  - **Edited locally only:**
    - openbia, sluice and switchboard, which are archived on GitHub and
      read-only;
    - diagnostics-architect-curriculum (on git.hraedon.com), because mvmcc02
      has no credentials for it;
    - every remaining worktree and untracked copy.

