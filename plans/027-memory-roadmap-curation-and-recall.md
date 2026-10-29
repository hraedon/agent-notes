# Plan 027 — Memory roadmap: measure, keep the store clean, curate periodically

**Status:** Proposed 2026-10-29; revised the same day after two independent
reviews (GPT-6.1-sol and Gemini 4 Argon; see §8). Both reviewers said
"rework", and both made the same point: the first draft built AOS-style
machinery (staging tables, a typed-op executor, rollback, telemetry) before
there was any evidence it would help. This revision keeps the owner's goal,
periodic curation, but as a **report that sessions act on with existing
commands**.
**Owner:** `agent-notes`.
**Supersedes:** Plan 020 (pluggable engine) and Plan 022 (shared and personal
memory), except where this plan restates their ideas.
**Builds on:** Plan 025 (memory tiers, the mailbox and inbox notices, the
dispatcher) and Plan 026 (native-only memory).

---

## 1. Where memory stands (2026-10-29)

| Fact | Value |
|---|---|
| Rows | 582: 417 reflections (5.4k characters on average), 118 project (5.2k on average, max 95k), 43 reference, 4 user/feedback |
| Writes per month | 112 / 224 / 208 / 25 / 2 / 11 (May → Oct) |
| Near-duplicate fact pairs | 15 (cosine distance < 0.08); 67 rows use `supersedes` |
| Tiers | 510 repo, 1 global, 0 VM |
| Harness-native memory | About 176 Claude Code memory items on mvmcc02 and 21 files on Windows, plus a Codex memory store per machine |

**How memory actually reaches agents today.** This is corrected from the
first draft (§8 R1).

- `orient` (session start) injects memory **names, types and ages only**, not
  bodies (`cli/orient.py:214`).
- Codex's lifecycle hook narrows that to counts (`codex_lifecycle.py:122-202`).
- The per-prompt hooks deliver mailbox messages, not memories.
- Memory bodies reach an agent only when it runs `memory get`, `memory
  recall`, `memory search` or `search` itself.
- What harnesses **inject automatically** is their own native memory, for
  example the first 200 lines of Claude Code's `MEMORY.md`.

So agent-notes memory is a *pull* resource with a *push* table of contents.
Agents mostly write to harness-native memory instead, and that's
machine-local.

## 2. What the field says

These are summarised from three research sweeps on 2026-10-29. The citations
are in the git history of this file at `d3d9f5f`. Only the items marked ✓
were checked against the primary source.

- **Memory can hurt.**
  - Wrongly selected prior context gives limited or negative benefit
    (SWE-ContextBench ✓).
  - Most off-the-shelf memory/solver pairings failed to beat having no
    memory (VibeMemBench).
  - **Measure before building.**
- **Don't rewrite memory; supersede it.**
  - Monolithic rewrites cause "context collapse" (ACE, ICLR 2026).
  - Mem0 v3 dropped its own in-place UPDATE/DELETE (✓).
  - Anthropic's Dreams never modifies its input store (✓).
- **Stale facts get over-trusted** (The Memory Trust Gap ✓). Resolve
  supersession before an agent sees a memory, not afterwards.
- **No LLM on the write path.** That's exactly what failed with Hindsight.
  The writing agent already decides what is worth keeping.
- **Self-generated skills don't help, but curated ones do** (SkillsBench).
  Anything promoted to procedural memory needs a human.
- **Memory shared across harnesses is a prompt-injection path** (MINJA,
  MPBench). Frame memory as data, keep provenance, and redact secrets.

## 3. Decisions

- **D1. Measure first, and keep it cheap.** Phase 0 is a static evaluation
  script and a fixture: no telemetry tables, no replay A/B. Ranking or
  injection changes ship only if they don't regress it.
- **D2. The lifecycle stays `active` + `supersedes`.**
  - No validity columns and no proposal state. `memory_model` already
    deactivates the predecessor on supersede.
  - Provenance goes in the existing `attributes` JSONB: `attributes.source =
    {origin, harness, session_key, vm, path, sha256}`.
  - That needs no migration.
- **D3. The writing agent is the judge.** `memory add` shows near-duplicate
  candidates and asks the agent to choose. Nothing calls an LLM at write time.
- **D4. Periodic curation ("dream") is a report, not an executor.**
  - A scheduled job finds problems and posts a curation notice to the
    project's inbox (Plan 025 mailbox).
  - The next session there, or a human, acts on it with existing commands:
    `memory add --supersedes`, `memory update` and `memory delete`. Those
    already write `change_log`.
  - No staging tables, no auto-apply, no new rollback machinery.
  - An optional LLM drafting step (Phase 3) only adds suggested wording.
- **D5. Harvest is a one-shot, reviewed import.** It's a command with
  `--dry-run`, not a daemon. It redacts secrets before anything is written.
  agent-notes never writes into harness directories.

## 4. Phases

### Phase 0: Measure

- **WI-0.1 Exposure inventory.** Per harness, record what reaches the model
  at session start and per prompt: agent-notes and native memory, with sizes.
  It's a table in this plan (§9), checked on each host.
- **WI-0.2 Recall fixture.**
  - 30 queries, hand-captured from real work across 6 or more projects. Each
    has the memory names a good answer would include, and the names that
    are stale or wrong.
  - Stored in `tests/memory_eval/fixture.jsonl`, with all expected items
    taken from a frozen snapshot of names.
- **WI-0.3 `scripts/eval_memory_recall.py`.**
  - Runs the fixture against the live DB, read-only.
  - Reports precision@5, MRR, the stale-hit rate (superseded or known-wrong
    items returned), and how often a reflection outranks a fact.
  - About 100 lines, with no schema changes.

**Exit:** a committed baseline under `docs/memory-eval/`.

### Phase 1: Store hygiene (no LLM)

- **WI-1.1 Write-time dedup.**
  - `memory add` runs a composed hybrid search over the target tier and its
    parent tiers, and prints the top-5 candidates with scores.
  - Above a threshold, it requires `--supersedes NAME`, `--related NAME`
    (writes a link) or `--distinct`.
  - The `add-memory` skill explains the choice.
- **WI-1.2 `contradicts` link kind.**
  - `memory link A B --kind contradicts`.
  - `memory get` and `recall` show a one-line warning when a returned memory
    has an open contradiction.
- **WI-1.3 Reflections are episodes.** Default `memory recall` and `search`
  rank `memory_type = 'reflection'` below facts, or exclude them with
  `--no-episodes`. Choose whichever WI-0.3 shows is better, and record the
  choice here.
- **WI-1.4 Size guidance.** `memory add` and `memory update` warn when a
  `project`, `user` or `feedback` body is over 2,000 characters, suggesting a
  `reference` or a split. Warn only; never refuse.
- **WI-1.5 `memory hygiene [--project P | --all] [--json]`.** Deterministic,
  read-only. It reports:
  - near-duplicate pairs;
  - oversized fact bodies;
  - the same name across tiers, where one shadows another;
  - open `contradicts` links;
  - facts not updated in 180 days that mention paths or versions;
  - reflections with no follow-up fact.

**Exit:** WI-0.3 doesn't regress. `memory hygiene --all` runs in under 5
seconds.

### Phase 2: Periodic curation v1 (deterministic)

- **WI-2.1 Schedule.**
  - A nightly timer on mvmcc02 runs `memory hygiene --all --json`.
  - Each project with actionable findings gets one Plan 025 `notice` in its
    inbox. Subject: "memory curation: N items". The body is a short list,
    capped at 2 KB, with `memory hygiene --project P` for the full report.
  - An unchanged report doesn't repost.
- **WI-2.2 Skill `curate-memory`.** Tells a session how to act on the notice
  with existing commands:
  - merge with `--supersedes`;
  - split oversized bodies;
  - resolve or annotate contradictions;
  - distil a reflection cluster into a fact, keeping dates, paths, SHAs and
    IDs verbatim.
  - Promotions to global scope and edits to `user`/`feedback` memories need
    the human's go-ahead.

**Exit:** after a month, measure whether notices get acted on. If not, cut
it.

### Phase 3: Periodic curation v2, LLM drafting (gated)

Starts only if Phase 2 notices are being acted on **and** Phase 0 has a
baseline.

- **WI-3.1** Add a `curate` job type to the dispatcher allowlist, next to
  `review`.
  - It runs `claude -p` with **no tools** and a hard timeout and token cap.
  - Its input is the hygiene report plus the bodies of the flagged memories,
    framed as data (Plan 025 D6), with secrets redacted.
  - Its output is **suggested text**: a merged body, a distilled lesson, or
    a contradiction summary, each citing source memory names. That text is
    appended to the curation notice.
  - It never writes memories itself.
- **WI-3.2** Check that every SHA, path, version string and WI/BC ID in the
  sources appears in the suggestion, or is listed as deliberately dropped.
  Suggestions that fail are dropped from the notice.

**Exit:** sessions adopt at least half of the suggestions without
rewriting them, and WI-0.3 doesn't regress.

### Phase 4: Harvest of harness-native memory (one-shot)

This is independent of Phases 1–3, but needs D5's redaction.

- **WI-4.1 `memory harvest --harness claude [--dry-run|--apply NAMES]`.**
  - **Claude Code only** at first. Codex's store on mvmcc02 is SQLite
    (`~/.codex/memories_1.sqlite`), not files, so it's out of scope until a
    fixture exists.
  - Read `~/.claude/projects/*/memory/*.md`, skipping `MEMORY.md` index
    files, except where `MEMORY.md` is the only file and holds the content.
  - Parse `type` from either top-level frontmatter or `metadata.type`.
    Files with no frontmatter go to the report as "needs typing", not
    imported.
- **WI-4.2 Project mapping.**
  - Encode known checkout paths forward (`/projects/foo-bar` →
    `-projects-foo-bar`) from Plan 025's per-VM checkout map. Never decode
    directory names backward.
  - `-projects` (the workspace root) and unmapped directories go to the VM
    tier.
- **WI-4.3 Redaction.**
  - Run the hook redactor (`hooks/common.py` `redact`), plus token patterns
    (bearer tokens, `nbt_`, `ghp_`, `sk-`, and so on).
  - Any file with a hit is imported only with the secret replaced, and is
    listed in the report so the owner can rotate it.
- **WI-4.4 Idempotency.** Key on `attributes.source = {vm, harness, path,
  sha256}`. A changed file is shown in `--dry-run` as a proposed supersede,
  never applied silently.

**Exit:** both machines are dry-run reviewed, and the chosen items are
imported with provenance.

## 5. Deferred, with the evidence that would bring each back

| Deferred | Revisit when |
|---|---|
| Per-project digests injected at session start | Phase 0 shows agents miss relevant memories that a digest would have surfaced |
| Recall telemetry table and usage-weighted ranking | Retrieval frequency can be linked to usefulness, not just counted |
| Replay A/B on historical tasks | Isolated execution, hidden tests and history-filtered memory snapshots exist. Without them, replay leaks the answers |
| Validity columns (`valid_from`/`valid_to`) | `active` + `supersedes` proves insufficient for a real query |
| Auto-applied curation, staging tables, rollback | Phase 3 suggestions are adopted unchanged at a high rate for months |
| Entity-mention links and ranking | The fixture shows misses that identifier overlap would fix |
| Skill-promotion pipeline | Already covered: Phase 2's skill says a human writes skills |

## 6. Security

- Provenance (`attributes.source`) on every harvested or curated write.
- Memory and curation input are always framed as data from a named origin.
- Secrets:
  - Harvest redacts (WI-4.3). On 2026-10-29 the Argon review found a live
    NetBox token in a Claude Code `MEMORY.md` on mvmcc02.
  - Hygiene adds a "possible secret" check to the existing memories.
- `agent-notes-web` is unauthenticated (decision 43) and shows memory bodies
  on the LAN. That's acceptable only while memories hold no secrets, which is
  one more reason for the redaction check.

## 7. Sequencing

| Phase | Depends on | Size |
|---|---|---|
| 0 | none | S |
| 1 | Plan 026 Phase 2b (native-only engine) | S–M |
| 2 | 1, plus the Plan 025 mailbox (deployed) | S |
| 3 | 0, plus Phase 2 being used | M |
| 4 | Redaction (WI-4.3) | S–M |

## 8. Review record (2026-10-29)

GPT-6.1-sol: "rework". Gemini 4 Argon: "rework". Raw reviews are on mvmcc02
under `~/reviews/plans-026-027/`. Adopted:

- **R1.** orient injects names, types and ages, not bodies. Codex gets only
  counts. Per-prompt hooks carry messages, not memories (GPT, verified at
  `cli/orient.py:214`). → §1, and digests deferred.
- **R2.** The schema conflicted with `memories_name_active_unique` and the
  partial indexes. `valid_to` duplicated `active`. A single `result_id`
  couldn't represent 1→N distillation (both reviewers). → D2: no new
  lifecycle columns.
- **R3.** Rollback lacked before-state, and links key on names, not revisions
  (GPT). → D4: no rollback machinery; existing `change_log` and `supersedes`.
- **R4.** Auto-merges can launder poisoned or negated claims, and the
  identifier gate isn't a secret scanner (GPT). → D4: nothing auto-applies;
  §6.
- **R5.** Harvest assumptions were wrong on real files:
  - Claude's `metadata.type`;
  - files with no frontmatter;
  - `MEMORY.md` used as content;
  - lossy directory encoding;
  - Codex memories stored in SQLite;
  - a plaintext NetBox token (Argon; the counts were confirmed by GPT).
  
  → Phase 4.
- **R6.** Phase 0 claimed no dependencies but needed Phase 1's table, the
  `agent_sessions` table has no first prompt, the runner is a reviewer
  rather than a task solver, and replay leaks answers (GPT). Recall logging
  turns read paths into writers (Argon). → Phase 0 is a static fixture.
- **Cuts both reviewers recommended:** the nightly LLM curator, digests,
  telemetry, usage ranking, entity links, skill promotion and replay A/B.
  - All are deferred (§5), except that the owner asked for periodic
    curation, which is kept as D4.
  - It is deterministic first; LLM drafting is gated.
  - Argon proposed exactly this deterministic hygiene report in place of
    Phase 3.

## 9. Exposure inventory (Phase 0, WI-0.1)

To be filled in.
