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
  choice here. **Chosen (2026-10-29): demote by 0.07** (`--reflections
  demote|mixed|exclude`, default `demote`). Excluding them regressed hit@5
  from 0.967 to 0.867; see §10.
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

Checked 2026-10-29, read-only, on the Windows workstation, mvmcc02 and
mvmhermes01, against agent-notes 1.1.1 at `b332466`. No hook was run, because
every hook writes an `agent_sessions` row. Sizes come from running the same
read-only `orient` commands the hooks run, from the installed configs, and
from the lengths (only) of recent Codex transcripts on Windows. "B" means
bytes; other sizes are characters.

**Shared hook budgets** (`hooks/common.py:35-37`): peer messages 8,000; one
injection in total 9,500; orient text 5,000. `orient_text` runs `orient
--path <cwd> --reconcile` (`common.py:474-493`). Orient lists memory **names,
types and ages only** (`cli/orient.py:214-219`): 5 repo, 3 VM and 3 global
memories, plus the latest reflection's name. Measured orient text is
1,511–1,849 B in registered repos, 321–326 B in an unregistered directory,
and 3.6–4.7 KB with `--json`.

| Harness | When | agent-notes injects | Size | Native memory injects | Size |
|---|---|---|---|---|---|
| Claude Code | SessionStart | Orient text, session-key line and claimed messages (`hooks/claude.py:79-116`). The hook skips its own orient when a settings file already runs `agent-notes orient`. | orient ≤5,000 (1.5–1.8 KB typical) + key ~200; total ≤9,500 | `MEMORY.md` of `~/.claude/projects/<cwd>/memory/` (first 200 lines); topic files only when read; repo `CLAUDE.md`/`AGENTS.md` | see host notes |
| Claude Code | per prompt | Claimed peer messages only (`claude.py:119-130`) | 0 when empty; ≤8,000 | — | — |
| Claude Code | Stop | One wake message plus a fixed hint (`claude.py:133-142`) | ≤8,000 + ~200 | — | — |
| Codex | SessionStart (startup, resume, clear, compact) | Work-item ids, status, severity, age; **counts** of changes and memories, no memory names; latest handoff time; drift ids; key line; messages (`hooks/codex.py:56-83`, `codex_lifecycle.py:122-219`, cap 7,000 at `:28`) | measured 933 incl. key line (Windows transcript); computed 545–731 + ~200 on mvmcc02 | **Windows:** `use_memories = true`; a `## Memory` developer block embeds `~/.codex/memories/memory_summary.md`. **mvmcc02:** memories not configured, nothing injected. Global `~/.codex/AGENTS.md` (Windows 0 B, mvmcc02 1,749 B) plus repo `AGENTS.md`. | Windows: measured 16,879 chars / 235 lines in each of the 3 latest sessions |
| Codex | per prompt / Stop | Messages only / wake message (`codex.py:86-109`) | ≤8,000 | — | — |
| OpenCode | first model request | `orient --path <dir> --json` formatted as project, work-item ids and titles, memory names and types; no `--reconcile`, **no cap**; nothing in an unregistered directory (`integrations/opencode/index.js:117-148`, `:309-343`) | 893 chars / 18 lines for agent-notes | Repo `AGENTS.md`. No global `~/.config/opencode/AGENTS.md`, no `instructions` key, no native memory store on either host. | repo `AGENTS.md` only |
| OpenCode | per prompt | `msg inbox --claim --json --limit 20`, spliced before the last user turn (`index.js:218-307`) | 0 when empty; **no budget** | — | — |
| OpenCode | compaction | Reconciliation checklist (`index.js:345-365`) | 493 | — | — |
| agy (mvmcc02 only) | SessionStart | Orient text and key line as `systemMessage`; never peer content (`hooks/agy.py:77-90`) | ~1.5–1.9 KB + ~200; ≤9,500 | No `GEMINI.md` under `~/.gemini` or at `/projects/*` roots; settings have no memory keys. 10 agent-notes skills in `~/.gemini/config/skills` (2.1–8.1 KB each). | none found |
| agy | every model call / Stop | Claimed messages as `ephemeralMessage` / fixed wake text (`agy.py:93-129`) | ≤8,000 / ~250 | — | — |
| Hermes (mvmhermes01, v0.22.5) | `pre_llm_call`, first turn | Orient text, key line and messages, **appended to the user message** (`hooks/hermes.py:72-102`) | ~1.2–1.9 KB + ~200; ≤9,500 | Frozen system-prompt snapshot: `memories/MEMORY.md` 2,197 chars (13 entries; limit 2,200), `USER.md` 1,359 (limit 1,375), `SOUL.md` 663; context files capped at 20,000; `memory.provider: ''` (built-in only) | ~4.2 KB + repo context |
| Hermes | later turns | Messages only; no Stop hook (`hermes.py:94-99`) | ≤8,000 | — | — |

**Host notes**

- **mvmcc02, Claude Code:** 49 project directories, 19 with `memory/` (9
  empty); 185 `.md` files (176 topic files and 9 `MEMORY.md`), 1,193,694 B in
  total. Topic types: 88 reference, 40 project, 40 feedback, 6 user. The
  largest topic files are 68, 110 and 114 KB. The largest `MEMORY.md` is
  `-projects` (cwd `/projects`): 170 lines / 25,398 B indexing 158 files. Next
  is `-home-itadmin-projects-homelab` (48 lines / 2,957 B); the rest are 23
  lines or fewer. No `MEMORY.md` exceeds 200 lines; no `~/.claude/CLAUDE.md`.
  SessionStart runs a legacy uncapped `agent-notes orient --reconcile`, then
  `gitea-orient` (0 B in every case measured), then `agent-notes-hook claude
  session-start`.
- **Windows, Claude Code:** 4 project directories, 2 with memory. `C--projects`
  has a 20-line / 3,152 B `MEMORY.md` over 21 files (45,603 B);
  `C--projects-agent-notes` has 2 lines / 260 B over 3 files. Hooks:
  `agent-notes-hook` only.
- **Windows, Codex:** 63 of the 84 substantive lines of `memory_summary.md`
  (13,275 B) appear in the injected block. Not injected: `memories/MEMORY.md`
  (139,452 B), `raw_memories.md` (183,102 B), 43 rollout summaries, and
  `memories_v2/` (152,045 B). `memories_1.sqlite` holds 39 `stage1_outputs`.
  The agent-notes Codex plugin is `enabled = false` on both hosts, so hooks
  come from `~/.codex/hooks.json` alone.
- **Repo `AGENTS.md`:** agent-notes is 16,422 B. About 110 checkouts on
  mvmcc02 range from 807 B to 46,007 B, mostly 5–12 KB.

**Findings that matter for this plan**

1. The largest automatic memory injection anywhere is Windows Codex's native
   `## Memory` block (~16.9k chars, every project on the machine), about 9x
   what agent-notes injects. agent-notes itself pushes a table of contents
   (names only), never bodies.
2. A NetBox-token-shaped string is in
   `~/.claude/projects/-home-itadmin-projects-homelab/memory/MEMORY.md` on
   mvmcc02, which Claude Code auto-loads in that directory (the checkout no
   longer exists, so it is dormant). A pattern scan also flagged 8 files under
   `-projects/memory/`, including its `MEMORY.md`; not inspected, possibly
   false positives. Feeds WI-4.3 and §6.
3. On mvmcc02 the legacy user-level `agent-notes orient` hook makes the capped
   hook skip its own orient, so orient arrives uncapped; in the agent-notes
   repo, the tracked `.claude/settings.json` adds a second orient
   (`--path /projects/agent-notes`). On Windows that tracked hook resolves to
   no project and also suppresses the hook's own orient.
4. The Hermes hook is registered but all 5 logged runs (2026-10-29
   15:42–15:44Z) failed with `ModuleNotFoundError: No module named
   'rpds.rpds'`; it fails open, so those turns got nothing from agent-notes.
5. OpenCode has no budget on the mailbox splice or its orient call.
6. `agent-notes orient --reconcile` crashes with `UnicodeEncodeError` on a
   cp1252 Windows console when drift exists (the ⚠ glyph at
   `cli/orient.py:367`). Hooks are unaffected because they capture stdout.

**Unverified:** whether Claude Code also caps `MEMORY.md` by bytes (~25 KB;
`-projects` sits at that edge); whether Claude Code on Linux loads
`AGENTS.md`; live Claude, agy and Hermes SessionStart payloads (no transcripts
captured); Codex `## Memory` composition (inferred by line matching, not from
Codex source) and its `project_doc` byte cap; whether agy loads
`AGENTS.md`/`GEMINI.md` or injects skill bodies; what OpenCode 2 auto-loads;
orient size on mvmhermes01; whether the Hermes failure persists; real mailbox
payload sizes; why `gitea-orient` printed nothing.

## 10. Implementation log

**2026-10-29, Phase 0 (Claude, uncommitted working tree):**

- **WI-0.1:** §9 filled in from code plus read-only inspection of the
  Windows workstation, mvmcc02 and mvmhermes01.
- **WI-0.2:**
  - `tests/memory_eval/fixture.jsonl` has 33 queries over 18 projects,
    labelled `claude-draft`. Three of them have no current answer and test
    stale-only behaviour.
  - `tests/memory_eval/names-snapshot-2026-10-29.json` freezes the 59
    `(project, name, memory_type)` triples the fixture uses. All were checked
    active against production with SELECT only.
- **WI-0.3:** `scripts/eval_memory_recall.py` (stdlib only) calls `agent-notes
  memory search|recall --json` in a subprocess and refuses any other
  subcommand.
- **Baseline:** `docs/memory-eval/baseline-2026-10-29.md`.
  - Scores: nP@5 0.92, MRR 0.86, hit@5 0.97, stale-slot rate 0.045.
  - A labelled stale memory is in the top 5 for 6/8 queries.
  - A reflection outranks a relevant fact in 3/26.
  - Reflections fill 61% of top-5 slots.
  - `search` and `recall` rank identically.
- **Inputs to Phase 1:**
  - Stale facts with no superseder rank first (supports WI-1.2 and WI-1.5).
  - Reflection crowding (WI-1.3; re-run this fixture to choose between
    down-ranking and `--no-episodes`).
  - The single global memory pads small projects because there is no score
    floor.
- **Open:**
  - Owner review of the labels.
  - An independent Codex labeling pass.
  - Fixing the §9 findings: the dormant token in a Claude `MEMORY.md`, the
    broken Hermes hook, the duplicate/uncapped orient on mvmcc02, and the
    Windows `orient --reconcile` encoding crash. None are fixed here.

**2026-10-29, Phase 1 (Claude, branch `work/027-p1-hygiene`, uncommitted):**

- **WI-1.1 `memory add` dedup gate.**
  - Before writing, it compares the body embedding with the facts (never
    reflections) in the target tier and its parent tiers (repo > vm > global).
    It returns the nearest five, plus any same-name row in a parent tier, as
    `dedup.candidates`.
  - A candidate blocks when its similarity is ≥ **0.85**, or when the new
    memory would shadow it by name. A blocked add exits 4 with
    `POSSIBLE_DUPLICATE` (a new `ErrorCode`), and the candidates are in
    `error.detail`.
  - The writer answers per candidate with `--supersedes NAME` (same tier;
    deactivates it), `--related NAME` (`relates_to` link, same transaction) or
    `--distinct`.
  - Re-adding the same name in the same tier is still a plain revision.
    Reflections skip the gate. A refused add writes nothing, not even a new
    vm/global tier row.
  - **Calibration on production:**
    - Embedding just the lead sentence of a fact scores ≥ 0.85 against the full
      fact in 23 of 25 samples (median 0.91).
    - 7 of 55 short facts (≤ 2,000 characters) have a neighbour ≥ 0.84.
    - None of the 11 facts written since 2026-08 has a neighbour above 0.75.
    - The plan's 15 pairs under distance 0.08 are four identical
      `smoke-test-*` rows plus long substrate/sf2 documents. That's why the
      write gate is looser (0.85) than the hygiene report (0.92).
- **WI-1.2 `contradicts`.**
  - `memory link A B --kind contradicts [--to-scope ...]` writes the link;
    `memory unlink` removes it. No schema change: `links.relationship` is free
    text; the CLI bounds it to `relates_to`/`contradicts`.
  - A contradiction is open while both ends are active.
  - `memory get`, `search` and `recall` add `contradictions` to the affected
    rows and a one-line `warnings` entry.
- **WI-1.3:** reflections are demoted by 0.07. Applied in SQL for both `search`
  and `recall`, so they still rank identically. `orient`, the top-level
  `search` and the knowledge provider keep pure similarity (`mixed`). See
  `docs/memory-eval/phase1-2026-10-29.md`.
- **WI-1.4:** `memory add` and `memory update` add a `warnings` entry over
  2,000 characters for `project`/`user`/`feedback`. They never refuse.
- **WI-1.5 `memory hygiene [--all] [--stale-days N] [--json]`.**
  - Read-only transaction, no embedding model. It reports:
    - near-duplicates (≥ 0.92; same-name pairs and already-linked pairs
      excluded);
    - oversized facts;
    - shadowed names;
    - open contradictions;
    - stale facts (180 days and a path or version regex);
    - reflections with no later or linking fact;
    - possible secrets (`core/secret_patterns.py`; labels only, active and
      superseded rows).
  - Production, `--all`: 0.8 s wall time when idle, 1.8 s under load. 15
    near-duplicate pairs, 68 oversized, 0 shadowed, 0 contradictions, 0 stale,
    225 reflections without a follow-up (19 projects), and 2 possible secrets
    (`url_credentials` in `substrate-agents-guide` and
    `reference-mcp-servers`; owner to check).
- **Eval (exit check),** before → after: nP@5 0.923 → 0.940, MRR 0.861 →
  0.915, hit@5 0.967 → 1.0, stale-slot rate 0.045 → 0.039, reflection share
  0.61 → 0.487, reflection outranks fact 0.115 → 0. `search` and `recall` are
  still identical.
- **Score floor: not adopted.** Every floor that removes the global padding
  also drops q13's correct answer (score 0.38) or another relevant name.
- **Review (GPT-6.1-sol, "rework"; all seven fixed, each with a regression
  test in `tests/test_memory_hygiene.py`):**
  1. Hygiene could echo a secret held in a memory *name*. Every section now
     carries the row ID, and names, workspace slugs or scope keys that contain
     a secret become `[redacted: memory id N]`.
  2. Recall's exact-name recovery reinstated a repo reflection under
     `--reflections exclude`. It now applies the policy, and so do search's
     `shadows`.
  3. A same-name add in the same tier is a plain revision again: candidates
     are shown, never blocking. The blanket `distinct=True` in
     `test_memory_tiers.py` is gone. The tier fixtures got non-colliding fake
     embeddings, and the one deliberate repo override passes `--distinct`.
  4. Choices bind to the resolved row (`id`), not the bare name;
     `--related TIER:NAME` picks a tier.
  5. A revision combined with `--supersedes OTHER` is refused before any
     write, in both the CLI and the model.
  6. Secret patterns:
     - `Bearer` is case-insensitive;
     - new `secret_assignment` (`*_TOKEN=`, `*_PASSWORD:`, ...) and
       `secret_json` (`"token": "..."`) patterns;
     - legacy 40-hex NetBox tokens are matched near the word NetBox;
     - placeholders such as `<token>`, `changeme` and `${VAR}` are skipped.

     (Superseded by round 2's stricter value rule, below.)
  7. The gate compares similarity at full precision and rounds only for
     display.
- **Review round 2 (GPT-6.1-sol): 2–7 confirmed fixed; four more, each with a
  regression test.**
  1. Every string in the hygiene output now goes through the same redaction,
     JSON and text: `memory_type`, slugs, and the CLI's top-level `scope`.
  2. The secret patterns are linear:
     - bounded quantifiers;
     - context patterns start only at a token boundary;
     - no overlapping quantified groups.

     Each pattern is timed on 21 adversarial 200 KB inputs (< 0.2 s each; the
     worst measured is about 0.05 s).
  3. False positives:
     - The key must be a whole credential word (`token`, `db_password`,
       `api_key`; not `tokenizer` or `max_tokens`).
     - The value must look like a credential: ≥ 12 characters, letters *and*
       digits, no spaces or brackets, and not a placeholder.
     - The negative corpus has 34 doc, config and code lines.

     Trade-off: an all-letter secret such as `some-long-passphrase` is no
     longer flagged by the context patterns. Production possible secrets are
     back to 2 (the PostgreSQL URLs); the cert-watch `CERT_WATCH_AUTH_SECRET`
     value has no digits and now passes.
  4. For both `--related` and `--supersedes`, a memory literally named
     `global:foo` wins over the tier prefix; otherwise `TIER:NAME` selects a
     tier. `--supersedes` still never leaves the target tier.
- **Open:**
  - `memory update` still does not re-embed a changed body (pre-existing).
  - The two `url_credentials` hits (and the cert-watch
    `CERT_WATCH_AUTH_SECRET` line, now unflagged) need an owner look.
    Checked 2026-10-29 without printing the values:
    - ids 36 and 50 are the throwaway test-DB fixture
      `substrate_test@localhost`, whose password equals its username;
    - id 241's value contains a test-fixture word, and the surrounding text
      is about tests.

    So none is believed live, though the cert-watch owner should confirm.
- **Round 3 (GPT-6.1-sol).**
  - All four round-2 fixes were confirmed, and the letters-only trade-off was
    accepted.
  - It found one more quadratic regex: the stale-path detector's relative-path
    alternative re-tried an extension-less path from every `/`.
  - It's now a token match plus an extension check in code
    (`_mentions_path`). `"a/" * 8000` dropped from 1.46 s to 1.5 ms, and
    200 KB takes 17 ms.
  - Added linearity and behaviour tests (`test_r3_mentions_path_*`).

### 2026-10-29: Phase 1 deployed (`b040efb`)

- **Hosts.** Deployed to mvmcc02, Windows (`shodan`) and mvmhermes01, and
  refreshed the harnesses, because the `add-memory` skill changed.
- **Production checks.**
  - `memory hygiene --all` took 1.2 s and found:
    - 15 near-duplicate pairs;
    - 68 oversized facts;
    - 0 shadowed names;
    - 0 contradictions;
    - 0 stale facts;
    - reflections without a follow-up fact in 19 projects;
    - 2 possible secrets, both the test-DB fixture.
  - Re-adding an existing project memory's text under a new name was refused
    with `POSSIBLE_DUPLICATE`, and nothing was written.
  - `memory recall` returned results.


### 2026-10-30: Phase 2 built, not deployed (Claude, worktree branch)

- **WI-2.1 `memory hygiene --notify [--dry-run]`.** Works with `--all` (the
  nightly run) or one project. `core/memory_curation.py` splits the report
  per repo project and posts one `notice` through `messaging.send_message`:
  - subject `memory curation: N items`, sender `payload.sender =
    "memory-curation"`, expiry 30 days;
  - body at most 2,048 bytes: one line per section with up to three examples
    (shrinking to one, then none, if needed), ending with `agent-notes memory
    hygiene --workspace W --project P`. `--workspace` is included because
    `--project` alone only resolves from inside the checkout.
  - Pair findings go to every repo project they touch. Findings with no repo
    memory (vm or global tier only) have no project inbox; they are counted
    under `unrouted` in the output and left to the full report.
  - Reflection backlogs under 3 reflections are not notice-worthy (they are
    normal between facts); the full report still lists them.
  - `--dry-run` prints each notice that would be posted or retired and writes
    nothing; it needs no VM ID.
- **Decision: the dedupe digest lives in the notice itself**
  (`payload.curation.digest`); no table, no migration. The job reads the
  latest curation notice for the project, live or expired. Same digest and
  not retired: nothing is posted, so an unchanged report never reposts, even
  after its notice expires. A changed digest posts a new notice and retires
  the older live one; a project whose findings are gone has its live notice
  retired. Retiring sets `expires_at` to now and `payload.curation.retired`,
  so the same findings coming back later post again. Only `msg prune`
  deleting an expired notice makes an unchanged report post once more.
- **Digest keys** are scope and name per finding (row ID for possible
  secrets), never scores or sizes, and a reflection backlog is keyed by its
  oldest reflection. Revising a still-oversized fact, or one more reflection
  on an existing backlog, is not a change.
- **Deploy artifacts:** `deploy/agent-notes-memory-curation.service` and
  `.timer` (systemd user units, nightly 03:30 with a 10-minute random delay,
  `Persistent=true`), following the dispatcher unit's convention. Not
  installed anywhere.
- **WI-2.2 skill `curate-memory`** (canonical under `skills/`, byte-equal copy
  in `plugins/agent-notes`). One finding in writing it: Phase 1's revision
  rule means `--supersedes` can't merge into an *existing* name. The skill
  gives two merges: revise the keeper and delete the other, or add under a
  new name with `--supersedes A --related B` and delete B.
- `data/cli-manifest.json`: `memory hygiene` is now `mutating` (with
  `--notify`), as `breadcrumb reconcile` is for `--apply`.
- **Open:**
  - ~~Notices only reach a session through `msg inbox` and the OpenCode
    plugin.~~ Closed the same day: `orient` now lists live notices for the
    project and VM (Plan 025 D4), so every SessionStart hook surfaces them.
    The text and the Codex block show generated metadata only (id, age), and
    point at `agent-notes msg inbox`. Subjects, bodies and senders are peer content,
    and on agy the orient text is a system-level channel (Plan 025 D6).
    `orient --json` carries the subject for tools. A failed notice query is
    reported as `notices_error`, never fatal.
  - Deploy on mvmcc02 after the usual upgrade: copy both units to
    `~/.config/systemd/user/`, `systemctl --user daemon-reload`, run
    `agent-notes memory hygiene --all --notify --dry-run` once and read it,
    then `systemctl --user enable --now agent-notes-memory-curation.timer`.
