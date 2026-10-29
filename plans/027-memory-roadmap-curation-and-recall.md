# Plan 027 — Memory roadmap: measured recall, harvest, and periodic curation ("dream")

**Status:** Proposed 2026-10-29.
**Owner:** `agent-notes`.
**Supersedes:** Plan 020 (pluggable memory engine; Hindsight retired by Plan
026 D6). Also supersedes Plan 022's provider-bank mapping and signed-note
dependencies. Plan 022's scope/visibility ideas live on only where §4 below
restates them.
**Builds on:** Plan 025 (memory tiers, the dispatcher and its job allowlist,
peer-data framing) and Plan 023 (the `claude -p` runner).

---

## 1. Where memory stands (2026-10-29)

| Fact | Value | So what |
|---|---|---|
| Rows | 582: 417 reflections, 118 project, 43 reference, 4 user/feedback | Mostly episodic. Few distilled facts |
| Body size | Reflections average 5.4k characters. Project memories average 5.2k, max 95k | These are documents, not memory items. They inject badly and retrieve coarsely |
| Writes per month | 112 / 224 / 208 / 25 / 2 / 11 (May → Oct) | Agents stopped writing here |
| Harness-native memory | 185 Claude Code memory files on mvmcc02, 21 on Windows, plus a Codex `~/.codex/memories` store | **Agents still write memories, just not to the shared store** |
| Curation | None. 15 near-duplicate fact pairs (cosine distance < 0.08); 67 rows use `supersedes` | Duplicates and stale facts accumulate, unseen |
| Tiers (Plan 025) | 510 repo, 1 global, 0 VM | Shipped yesterday; nothing populates them yet |
| Measurement | None. We don't know whether injected memories help | Highest-risk gap (P1) |

## 2. What the field says

These principles come from three research sweeps (2026-10-29): first-party
harness memory, open-source memory systems, and 2025–2026 papers. Citations are
in §10. Items marked [pre] are preprints, and many 2026 results come from a
single author and haven't been replicated.

- **P1. Memory can hurt, so measure before building.**
  - In SWE-ContextBench [pre], correctly selected summaries of prior work
    help, while unfiltered or wrongly selected context gives limited or
    negative benefit.
  - In VibeMemBench [pre], 11 of 12 off-the-shelf memory/solver pairings
    failed to beat memory-off.
  - DreamBench-SWE [pre] found memory beats no memory, but no significant
    difference *between* memory systems.
- **P2. Precision beats recall when injecting.** A wrong lesson is worse than
  no lesson (SWE-ContextBench). Agents also copy retrieved records, so
  errors spread to later tasks (Xiong et al., ACL 2026).
- **P3. Curate with incremental, typed operations; never rewrite.**
  - ACE (ICLR 2026) found that monolithic rewrites cause "context collapse"
    and "brevity bias".
  - Mem0 v3 abandoned its own in-place UPDATE/DELETE in favour of add-only.
  - Anthropic's Dreams writes a *separate* output store and never modifies
    its inputs.
  - Codex consolidation uses one lease and a git-tracked directory.
  - Gemini CLI Auto Memory sends every change to an approval inbox.
- **P4. Supersede, never delete, and resolve conflicts before injection.**
  - Models used a stale stored value 92–100% of the time, and more capable
    models were harmed more (The Memory Trust Gap [pre]).
  - Embedding similarity can't tell a contradiction from a duplicate, while
    deterministic supersession in the query removes the error (MemStrata
    [pre]).
  - Graphiti and SodaMem use validity intervals plus supersede edges.
- **P5. Summaries drop specifics.** Gist compression kept 3% of temporal
  expressions; one prompt sentence raised that to 62% (*The Sleeping Agent*
  [pre]). Curation must keep dates, versions, paths and SHAs, and must link
  back to its sources.
- **P6. Don't put an LLM on the write path.**
  - Hindsight's write-time extraction is what failed here.
  - Our agents already *are* the extractor: they decide what to write.
  - LLM work happens only in batch curation.
- **P7. Import harness-native memory; don't compete with it.**
  - Claude Code's auto memory is machine-local and per-repo, and uses the
    same four types as ours (user/feedback/project/reference).
  - Codex's memory files are "generated state", per machine.
  - Letta's `/init` already ingests Claude and Codex histories.
  - Our value is *sharing across machines and harnesses*, plus curation.
- **P8. Procedural memory goes through human review.** SkillsBench (NeurIPS
  2026): curated skills add about 16 points, while self-generated skills add
  about 0, and 16 of 84 tasks got worse.
- **P9. Cross-harness memory is a prompt-injection path.**
  - MINJA, AgentPoison and MPBench show memory poisoning through ordinary
    use, and prompt-injection defences don't cover it.
  - Screening caught 0 of 360 plain false statements (*Utility Under Attack*
    [pre]).
  - Defences that hold up: provenance, injecting memory as data, per-source
    caps on injected slots, and review for wide-scope promotion.

## 3. Decisions

- **D1. Measurement gates curation.** No curation result is auto-applied, and
  orient doesn't switch to digests (§7), until the Phase 0 evaluation shows a
  baseline and the change doesn't regress it.
- **D2. The writing agent is the extractor; the dream job is the curator.**
  Writes stay explicit, and an LLM runs only in the batch job. No write-time
  LLM, no learned-memory service, no graph database. `links` plus recursive
  CTEs handle relations at our scale (low thousands of rows).
- **D3. Rows are never edited in place by automation.**
  - Curation creates new rows and flips `active`, tagged with a
    `curation_run_id`, so a whole run can be rolled back.
  - Humans and agents keep `memory update`; it already writes `change_log`.
- **D4. Supersession and validity are enforced in SQL, not left to the
  model.**
  - Default recall and injection exclude superseded or expired rows.
  - Two rows linked `contradicts` are never injected together. The newer one
    is injected with a one-line note that an older, conflicting memory
    exists.
- **D5. Reflections are the episode archive, not injection material.**
  - Once curation has distilled a reflection, it leaves default recall and
    orient.
  - It stays searchable with `--include-episodes`, and it's never deleted.
- **D6. Harness-native memory is harvested read-only into proposals.**
  - agent-notes never writes into `~/.claude`, `~/.codex` or other harness
    directories.
  - Harvested items arrive as `proposed`, and they're deduplicated against
    the store before anyone reviews them.
- **D7. The dream job is a dispatcher job type, not a new service.**
  - Add `curate` to the dispatcher's job allowlist, next to `review`
    (`dispatcher/__init__.py:100`).
  - A systemd timer on mvmcc02 inserts the `request` rows.
  - An advisory lock ensures one curation run per workspace at a time.

## 4. Schema changes (Phase 1, migration 860)

```sql
ALTER TABLE memories
  ADD COLUMN valid_from  TIMESTAMPTZ NOT NULL DEFAULT now(),  -- backfill = created_at
  ADD COLUMN valid_to    TIMESTAMPTZ,              -- set when superseded or retired
  ADD COLUMN origin      TEXT NOT NULL DEFAULT 'agent'
             CHECK (origin IN ('agent','human','harvest','curation')),
  ADD COLUMN source      JSONB NOT NULL DEFAULT '{}',
             -- {harness, session_key, vm, path, sha256, curation_run_id}
  ADD COLUMN review_state TEXT
             CHECK (review_state IN ('proposed','rejected')),  -- proposed rows have active = false
  ADD COLUMN summary     TEXT CHECK (length(summary) <= 280);  -- injection line for long bodies

CREATE TABLE memory_recalls (          -- append-only; avoids hot-row updates
  id          BIGSERIAL PRIMARY KEY,
  memory_id   BIGINT NOT NULL REFERENCES memories(id),
  session_key TEXT,
  surface     TEXT NOT NULL CHECK (surface IN ('orient','recall','search','get','prompt-hook')),
  rank        INTEGER,
  query_hash  TEXT,
  at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE curation_runs (
  id          BIGSERIAL PRIMARY KEY,
  workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
  project_id  INTEGER REFERENCES projects(id),
  started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  ended_at    TIMESTAMPTZ,
  model       TEXT, model_lineage TEXT,
  input_watermark TIMESTAMPTZ,
  usage       JSONB NOT NULL DEFAULT '{}',
  status      TEXT NOT NULL CHECK (status IN ('running','completed','failed','rolled_back'))
);

CREATE TABLE curation_ops (
  id          BIGSERIAL PRIMARY KEY,
  run_id      BIGINT NOT NULL REFERENCES curation_runs(id),
  op          TEXT NOT NULL CHECK (op IN ('merge','supersede','distill','contradiction',
                                          'retire','promote_scope','digest','skill_candidate')),
  source_ids  BIGINT[] NOT NULL,       -- memories the op is derived from (required, non-empty)
  result_id   BIGINT REFERENCES memories(id),
  rationale   TEXT NOT NULL,
  state       TEXT NOT NULL DEFAULT 'proposed'
              CHECK (state IN ('proposed','applied','rejected','rolled_back')),
  decided_by  TEXT, decided_at TIMESTAMPTZ
);
```

Provenance edges also go in `links` (`derived_from`, `contradicts`,
`mentions`), so the existing link tooling and audits see them.

## 5. Phases

### Phase 0: Measure (before any curation)

- **WI-0.1 Recall telemetry.**
  - orient, the per-prompt hook, `memory recall/search/get` and `search
    --federated` write a `memory_recalls` row for each memory they return or
    inject.
  - Writes are best-effort and fail open, like the hooks.
- **WI-0.2 Retrieval precision set.**
  - Sample 40 real session starts across 6 or more projects, from the
    `agent_sessions` cwd/project plus the first user prompt.
  - Record what orient injected, and have Claude and Codex label each item
    for relevance, with a human resolving disagreements.
  - Metrics: precision@k for orient, and the stale-injection rate (injected
    items that are superseded, or wrong as of today).
  - Stored as a fixture in `tests/memory_eval/`.
- **WI-0.3 Replay A/B.**
  - 20 closed work items that have a fix commit and a test, spread across
    repos.
  - The dispatcher runs each one headless at the parent commit, once with
    agent-notes orient and memory hooks on and once with them off.
  - Score test pass, turns and tokens. Expensive, so run it monthly and
    before any change to injection policy, not per commit.
- **WI-0.4 Detail-retention check.** A deterministic validator for curation
  outputs:
  - extract SHAs, paths, version strings, dates and WI/BC identifiers from
    each op's sources;
  - report the share that survive in the result;
  - flag any op below its threshold, and refuse to auto-apply it.

**Exit:** a baseline report for precision@k, stale rate and A/B is committed
under `docs/memory-eval/`.

### Phase 1: Store hygiene, no LLM

- **WI-1.1** Migration 860 (§4). Backfill `valid_from = created_at`, and set
  `valid_to` from existing `supersedes` chains.
- **WI-1.2 Default recall respects validity.**
  - `active AND valid_to IS NULL AND review_state IS NULL` everywhere.
  - Reflections are excluded from orient. Recall includes them only when
    they haven't been distilled (D5).
- **WI-1.3 Write-time dedup with the agent as judge.**
  - `memory add` runs a composed hybrid search over the same tier and its
    parent tiers, and returns the top 5 candidates with similarity scores.
  - Above a threshold, it requires `--supersedes NAME`, `--related NAME`
    (writes a link) or `--distinct`.
  - The skill text explains the choice. This is the Mem0 update phase
    without the second LLM call (P6).
- **WI-1.4 Size discipline.**
  - `project`, `user` and `feedback` bodies over 2,000 characters are
    refused unless `--summary` is given. `reference` may be long but always
    needs a summary.
  - orient injects the `summary` line, falling back to the first 280
    characters, never the whole body.
  - Large existing rows get summaries through the Phase 3 backfill op.
- **WI-1.5 Injection as data, with byte budgets and per-source caps.**
  - orient tiers move from counts (5/3/3) to byte budgets: project 3 KB,
    VM 1 KB, global 1.5 KB.
  - At most 2 injected items may come from the same `source.session_key`.
  - Memory content gets Plan 025's peer-data framing (`[agent-notes memory
    from <origin>/<harness> …]`).
- **WI-1.6 Ranking.**
  - Hybrid score × recency decay per type: half-life 30 days for
    reflections, 180 days for project facts, none for user/feedback.
  - Multiplied by a usage boost from `memory_recalls`, with a log damper.
  - There is no hard deletion anywhere.

**Exit:**
- Phase 0 metrics show no regression.
- `doctor` reports rows over budget, missing summaries and near-duplicate
  pairs.

### Phase 2: Harvest harness-native memory

- **WI-2.1 `agent-notes memory harvest [--harness claude|codex|hermes|all] [--dry-run]`.**
  It runs per machine, because the files are machine-local. Sources:
  - Claude Code: `~/.claude/projects/<dir>/memory/*.md`. The frontmatter
    `type` maps 1:1, and `name`/`description` become `name`/`summary`.
  - Codex: the sections of `~/.codex/memories/MEMORY.md`. Use
    `memory_summary.md` only as evidence, never as items.
  - Hermes: `MEMORY.md`.
- **WI-2.2 Project mapping.** Map the harness directory to an agent-notes
  project through Plan 025's per-VM checkout map (WI-2.6). Unmapped items
  land in the VM tier as proposals.
- **WI-2.3 Idempotency.** `source.path` + `source.sha256`. A changed file
  produces a new proposal that supersedes the earlier harvested row. An
  unchanged file is a no-op.
- **WI-2.4 Schedule.** A per-machine timer (systemd on Linux, launchd on the
  Mac, a logon task on Windows), daily. It can also run after
  `install-harness`.
- **Never auto-activated.** Harvested items wait for review (Phase 3 can
  propose merges for them).

**Exit:** both machines are harvested; proposals are deduplicated against
the store; a reviewer can accept or reject them with `memory review`.

### Phase 3: Curation ("dream")

- **WI-3.1 Job.**
  - Job type `curate` on the dispatcher, with payload `{project | tier,
    since_watermark, max_ops}`.
  - Runs `claude -p` through Plan 023's runner in safe mode: read-only
    tools, no network, `--json-schema` output.
  - The job builds the prompt input; the model gets no database access.
  - Default model: whatever reviewer model the dispatcher already holds a
    credential for. The run records its model and lineage.
- **WI-3.2 Triggers.**
  - A nightly timer on mvmcc02 for projects with new input since their
    watermark.
  - Earlier when a project gains 10 or more undistilled reflections or
    harvested proposals.
  - Limits per run: projects processed, tokens, and ops. A
    `pg_try_advisory_lock` per workspace.
- **WI-3.3 Input.** Per project, presented as data with peer-data framing:
  - active facts, with summaries and usage counts from `memory_recalls`;
  - reflections not yet distilled;
  - harvested proposals;
  - open `contradicts` links.
  - Raw transcripts are never included, which follows Codex's v2 rule and
    avoids leaking secrets.
- **WI-3.4 Operations.** The model emits typed ops only (§4 `curation_ops`).
  Each op must list non-empty `source_ids` and a rationale.

  | Op | Effect when applied |
  |---|---|
  | `merge` | New row from N near-duplicates; sources get `valid_to` and `derived_from` links |
  | `supersede` | Newer claim wins; the old row gets `valid_to` |
  | `distill` | Reflections → subtask-level lessons, **including failures and dead ends** (ReasoningBank, Memento); reflections marked distilled (D5) |
  | `contradiction` | Writes a `contradicts` link; if one side is clearly newer and confirmed, also proposes `supersede` |
  | `retire` | `valid_to` set; only when every source is gone or the claim is refuted |
  | `promote_scope` | repo → VM/global for facts seen in 3+ projects; **always human-reviewed** |
  | `digest` | Regenerates a per-project digest (≤ 2 KB) from active facts (sleep-time precompute) |
  | `skill_candidate` | Lesson reinforced across ≥ 3 sessions → suggested SKILL.md text; **never auto-applied** (P8) |
- **WI-3.5 Apply policy.**
  - Auto-applied: `merge` of near-identical rows (cosine distance < 0.03 and
    the retention check passes), `digest`, and summary backfill.
  - Everything else is proposed.
  - Ops touching `user`/`feedback` or the global tier always need a human.
- **WI-3.6 Review surface.**
  - `agent-notes memory review [list|show|accept|reject] [--run ID]`.
  - The web UI gets a read-only "pending curation" page.
  - `memory curation rollback RUN` reverts every applied op of a run.
- **WI-3.7 Prompt contract.** The prompt instructs the model to:
  - keep dates, versions, file paths, SHAs and WI/BC IDs verbatim (P5);
  - prefer leaving an item alone over rewriting it;
  - emit no op that has no source.
  - The WI-0.4 validator enforces this mechanically.

**Exit:**
- One month of nightly runs on two projects (agent-notes plus one other).
- Reviewer acceptance ≥ 70% of proposals.
- The detail-retention validator passes on all auto-applied ops.
- Phase 0 precision@k and stale rate improve, or at least don't regress.

### Phase 4: Use the curated store

- **WI-4.1 Digest injection trial.** orient injects the project digest instead
  of the top-k memories, in an A/B against Phase 0. It is adopted only if
  precision and A/B results improve (D1).
- **WI-4.2 Contradiction-aware injection (D4).**
- **WI-4.3 Entity mentions.** Regex-extract paths, WI/BC IDs, repos and tools
  into `mentions` links at write time. Use entity overlap with the current
  prompt or cwd as a ranking feature.
- **WI-4.4 Skill promotion flow.** Accepted `skill_candidate` ops open a
  work item that has a draft SKILL.md attached. A human edits and commits
  the skill, and the source lessons link to it.

## 6. Security (P9)

- Every row carries `origin` and `source` (harness, session, VM).
  Unattributed writes are refused.
- Injected memory is always framed as data from a named origin, never as
  operator instructions.
- Per-source caps on injected slots (WI-1.5).
- Curation input is framed the same way. The curation model has no tools
  that write, and its output is a schema-validated list of ops.
- Promotion to global, `user`/`feedback` changes, and skill candidates need a
  human.
- No secrets in memory. Harvest skips files that match the existing
  identifier-gate denylist patterns, and the curation prompt input is
  scanned with the same gate.

## 7. Sequencing and cost

| Phase | Depends on | Size |
|---|---|---|
| 0 | none (WI-0.3 needs the dispatcher, already deployed) | M |
| 1 | Plan 026 Phase 2b (native-only engine) | M |
| 2 | 1 | S–M |
| 3 | 0, 1 (2 recommended) | L |
| 4 | 3 | M |

Phases 0 and 1 can run in parallel. LLM spend runs only in Phase 0's
labelling and A/B, and in Phase 3's nightly runs, which are capped per run
and reported in `curation_runs.usage`.

## 8. Non-goals

- An LLM on the write path, a learned-memory service, or a graph database
  (P6, D2).
- Hard automatic forgetting. Ranking decays instead, and nothing is deleted
  by automation.
- Writing back into harness-native memory directories.
- Chasing LoCoMo or LongMemEval scores. Our measure is Phase 0's evaluation
  on our own sessions.
- Auto-generated skills.

## 9. Open questions

1. **Digest vs top-k at session start.** Decided by WI-4.1's evaluation, not
   up front.
2. **Curation model.**
   - Option: the dispatcher's existing Claude credential.
   - Alternative: alternate Claude and Codex by night, so neither model
     family alone shapes the store. Same cost, more diversity.
   - Decide after Phase 3 begins.
3. **Should the Codex `memory_summary.md` and Claude `MEMORY.md` index lines
   be re-injected by agent-notes at all?** Each harness already injects its
   own, so the default is no; harvested content enters the shared tiers only
   after review.

## 10. Sources

**Harness and product documentation:**
- Claude Code memory: https://code.claude.com/docs/en/memory
- Anthropic Dreams (research preview, verified 2026-10-29): https://platform.claude.com/docs/en/managed-agents/dreams
- Managed Agents memory stores: https://platform.claude.com/docs/en/managed-agents/memory
- Codex memories:
  - pipeline source: https://github.com/openai/codex/blob/main/codex-rs/memories/README.md
  - consolidation prompt: https://github.com/openai/codex/blob/main/codex-rs/memories/write/templates/memories/consolidation.md
- Gemini CLI auto memory: https://geminicli.com/docs/cli/auto-memory.md
- Letta Code memory: https://docs.letta.com/letta-code/memory
- Hermes memory: https://hermes-agent.nousresearch.com/docs/user-guide/features/memory
- Mem0 v3 add-only (verified 2026-10-29): https://docs.mem0.ai/migration/oss-v2-to-v3

**Open-source systems:**
- Zep/Graphiti: arXiv 2501.13956
- A-MEM: arXiv 2502.12110
- MemOS: arXiv 2507.03724
- MIRIX: arXiv 2507.07957
- Hindsight: arXiv 2512.12818
- LangMem: https://langchain-ai.github.io/langmem/
- Basic Memory: https://docs.basicmemory.com/
- mcp-memory-service: https://github.com/doobidoo/mcp-memory-service
- SodaMem: arXiv 2608.08055

**Papers:**
- ACE: arXiv 2510.04618 (ICLR 2026)
- ReasoningBank: arXiv 2509.25140
- Agent Workflow Memory: arXiv 2409.07429
- Sleep-time compute: arXiv 2504.13171
- Memento: arXiv 2508.16153
- *The Sleeping Agent*: arXiv 2608.11775
- RSPM: arXiv 2610.25561
- SWE-ContextBench: arXiv 2602.08316 (verified 2026-10-29)
- VibeMemBench: arXiv 2609.23570
- Subtask-level memory: arXiv 2602.21611
- SkillsBench: arXiv 2602.12670
- Memory management and error propagation: arXiv 2505.16067
- The Memory Trust Gap: arXiv 2609.01852 (verified 2026-10-29)
- MemStrata: arXiv 2606.26511
- MemoryAgentBench: arXiv 2507.05257
- DreamBench-SWE: arXiv 2608.20664
- Beyond Downstream QA: arXiv 2610.22209
- MINJA: arXiv 2503.03704
- AgentPoison: arXiv 2407.12784
- MPBench: arXiv 2606.04329
- Utility Under Attack: arXiv 2608.21230
- Surveys: arXiv 2512.13564; arXiv 2606.30306

Except where marked verified, these citations were gathered by research
subagents from abstracts and documentation. Check the figures before quoting
them elsewhere.
