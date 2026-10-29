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
