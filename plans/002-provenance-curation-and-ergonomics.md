# Plan 002 — Writer Provenance, Curation Primitives, and Onboarding Ergonomics

Status: Proposed (v0.1, drafted 2026-05-21 by Claude Opus 4.7 in conversation with plm; awaiting peer review against Plan 001's discipline)
Scope: Targeted follow-on to Plan 001. Adds writer attribution, low-cost memory curation, and onboarding/path-resolution ergonomics. Does NOT touch the storage substrate, the transport model, or the omnibus/per-kind binary split — those calls in Plan 001 stand.
Consumers: same as Plan 001 (sf2, substrate, sf1, plus Claude Code / agy / OpenCode / Gemini harnesses).
Reference: spawned from a Gemini-authored `generalization_proposals.md`; the rejected branches (pluggable storage, CRDT federation, multi-transport hub, autonomous LLM synthesis) are recorded in §6 with rejection rationale so the next reviewer doesn't re-litigate them.

## 1. Why now

Plan 001 closed Phase 6 and the new server is in production. Five hours of real usage surfaced one bug bundle (BC-001..005) and two recurring friction points:

1. **"Who wrote this?"** — `change_log.actor` is a TEXT column that nothing populates today. As more agents (Claude lineages, agy, OpenCode) write into one DB, attribution becomes load-bearing for trust, debugging, and selective ignoring.
2. **"Where am I?"** — every kind tool requires `workspace` + `project` slugs as args. Agents don't know them on first call; the human or a `list_projects` round-trip has to bridge. Project creation is currently a Python one-liner against `core.db` — not user-facing.

Curation (Gemini's #4, distilled) is a smaller motivation but a worthwhile companion phase since the schema changes overlap.

## 2. Design decisions

Numbered to extend Plan 001's sequence (decisions 1–26 are reserved).

27. **Writer provenance is first-class columns on `change_log`, not JSONB.** Extends decision 23 (load-bearing attrs → columns). Add `agent_lineage`, `harness`, and `environment` as nullable TEXT columns. Reason: any future filter ("show me memories written by claude-opus-4-7 from antigravity-cli") or audit ("which lineage produced the bogus run of memories?") must run as cheap indexed predicates, not JSONB path queries. JSONB stays for genuinely unbounded provenance metadata (`git_context`, `model_temperature`, etc.) under a single `provenance JSONB` column.

28. **Provenance is set by the harness, not invented by the server.** The MCP server reads three env vars (`AGENT_NOTES_AGENT_LINEAGE`, `AGENT_NOTES_HARNESS`, `AGENT_NOTES_ENV`) on each tool call and stamps `change_log` with their values. The kind tools never guess. If a harness doesn't set them, the columns are NULL — which is honest and queryable, not a fabricated default.

29. **Provenance is recorded at `change_log` only, not duplicated on kind rows.** Kind tables stay minimal. Anyone who wants "who wrote BC-184?" runs the existing `history` tool, which already filters `change_log` by `(kind, identifier)` — provenance falls out for free. Reason: kind tables are the read-hot path; bloating them with attribution columns we'd hit in <5% of queries is a poor trade.

30. **`mark_stale` is a column flip on memories, not a deletion.** Add `stale BOOLEAN NOT NULL DEFAULT FALSE` and `stale_reason TEXT` to `memories`. `search_memory` filters them out by default (`stale=false`), with an `include_stale: true` opt-in. Reason: deletion is irreversible; staleness preserves the audit trail and lets a future reflection ask "what did we believe in March that turned out wrong?" The decision to mark stale is made by an agent or human; the server doesn't infer staleness from age.

31. **`suggest_duplicates` extends to memories.** Breadcrumbs already have this tool. Re-implement the same cosine-similarity-over-embeddings logic for memories, surfaced via `memory.suggest_duplicates`. The output is a *list of candidates with scores*, not auto-merges. Reason: the cheap half of Gemini's "synthesis" pipeline is the dupe finder; the expensive half (LLM merge) ships failure modes — confident-but-wrong canon. Stop at the candidate list and let a human or session decide.

32. **`agent-notes init <path>` is a first-class CLI.** Replaces the Python one-liner currently needed to register a project. Argument: a filesystem path. Behavior:
    - Walk up to find the git root (or use the path as-is); use the dir name as the project slug unless `--project` overrides.
    - Default workspace = `default`; `--workspace` overrides.
    - `breadcrumbs_dir` defaults to `breadcrumbs` relative to repo root; `repo_root` is the absolute path.
    - Idempotent: re-running upserts.
    Reason: every new project today requires touching three things (workspace, project, breadcrumbs_dir). One command, one path, done.

33. **Path-based project resolution is a core helper, not per-kind logic.** New core tool `resolve_project(path)` returns `{workspace, project}` by matching `path` against `projects.repo_root` (longest-prefix wins). Kind tools accept either `(workspace, project)` *or* `path`, with `path` taking precedence when both are passed. Reason: agents know where they're working (cwd, an open file); making them know workspace+project slug names is friction that shows up in every tool call. The MCP server already has the path→project mapping; expose it.

34. **No new transports, no new substrates, no auto-synthesis.** Stated for the record (§6 explains why each was considered and rejected). This plan is additive, not architectural.

## 3. Schema delta

```sql
-- Decision 27: first-class provenance columns on change_log
ALTER TABLE change_log
    ADD COLUMN agent_lineage TEXT,     -- e.g. 'claude-opus-4-7', 'gemini-2.5-flash'
    ADD COLUMN harness       TEXT,     -- e.g. 'claude-code', 'antigravity-cli', 'opencode'
    ADD COLUMN environment   TEXT,     -- e.g. 'local', 'sandbox', 'ci'
    ADD COLUMN provenance    JSONB NOT NULL DEFAULT '{}';

CREATE INDEX idx_change_log_lineage
    ON change_log (agent_lineage, changed_at DESC);

-- (Existing `actor TEXT` column from Plan 001 is retained for human/user identity;
-- agent_lineage is its agent-side counterpart. Both can be populated when known.)

-- Decision 30: staleness on memories
ALTER TABLE memories
    ADD COLUMN stale        BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN stale_reason TEXT,
    ADD COLUMN stale_at     TIMESTAMPTZ;

CREATE INDEX idx_memories_active_fresh
    ON memories (project_id, name)
    WHERE active = true AND stale = false;
-- Search default path: WHERE active AND NOT stale. Existing partial unique
-- index on (project_id, name) WHERE active still holds; add this one alongside.
```

No changes to `breadcrumbs`, `links`, `workspaces`, `projects`, or vocabularies.

## 4. Tool surface delta

New / extended tools:

- **`resolve_project(path)`** (core helper, decision 33): returns `{workspace, project, repo_root}` for the longest-prefix match against `projects.repo_root`. Available to every kind server.
- **All kind tools that currently take `(workspace, project)`**: gain an optional `path` parameter; when set, it's resolved via `resolve_project` and overrides explicit args.
- **`memory.mark_stale(name, reason)`** (decision 30): flips the staleness flag with a required human-readable reason. Stamps `change_log` with `event='marked_stale'`.
- **`memory.suggest_duplicates(project, threshold=0.85, limit=20)`** (decision 31): returns ranked candidate pairs above threshold. Output is *informational* — no merges, no mutations.
- **`search_memory`**: gains `include_stale: boolean = false` flag.

New / extended CLI:

- **`agent-notes init <path>`** (decision 32): registers workspace + project + repo_root + breadcrumbs_dir in one call. Idempotent.
- **`agent-notes-doctor`**: gains a "provenance env vars" section showing what the current shell would stamp.

## 5. Phased implementation

Each phase is one Sonnet dispatch (4–8h). Sub-phases parallelize where noted.

### Phase 7a — Onboarding ergonomics (low risk, high signal)

| # | Task | Outcome |
|---|---|---|
| 7a.1 | `agent-notes init <path>` CLI (decision 32) | New project registered with one command; idempotent re-runs upsert |
| 7a.2 | Core helper `resolve_project(path)` (decision 33); register as MCP tool on every kind server | Path-based resolution available to all kinds |
| 7a.3 | Extend kind tools' input schemas to accept `path` alongside `(workspace, project)`; precedence: path > explicit args | Existing call sites keep working; new path-based calls work too |
| 7a.4 | Tests + AGENTS.md update | `make test` green; convention documented |

### Phase 7b — Writer provenance

| # | Task | Outcome |
|---|---|---|
| 7b.1 | Schema migration for `change_log` provenance columns + index (decision 27) | Migration runs idempotently against existing data (existing rows have NULL provenance, which is correct — we didn't track it before) |
| 7b.2 | Read provenance env vars in `core.server`; thread through all `change_log.write_change` call sites | Every new `change_log` row carries provenance when the harness provides it |
| 7b.3 | `agent-notes-doctor` shows current env's provenance values (decision 28) | New users see what their session would stamp; missing values are obvious |
| 7b.4 | Update `history` tool output to surface provenance columns | `history` answers "who wrote this?" with no extra query |
| 7b.5 | Add `agent_lineage` / `harness` filters to `changes_since` | "Show me what claude-opus wrote since yesterday" works |
| 7b.6 | Document the three env vars in AGENTS.md and the per-harness README sections | Harness operators (including the user's MCP configs) know what to set |

### Phase 7c — Curation primitives (depends on 7b for `change_log` write-site changes)

| # | Task | Outcome |
|---|---|---|
| 7c.1 | Schema migration for `memories.stale` / `stale_reason` / `stale_at` + filtered index (decision 30) | Migration runs idempotently |
| 7c.2 | `memory.mark_stale` tool; writes `event='marked_stale'` to change_log | Memories can be retired without deletion |
| 7c.3 | `search_memory` filters `stale=false` by default; `include_stale: true` opt-in | Token noise reduced; opt-in for retrospection |
| 7c.4 | `memory.suggest_duplicates` tool (decision 31) | Candidate pairs surface; no autonomous merging |
| 7c.5 | Tests covering both tools | `make test` green |

### Phase 7d (optional) — Wire harness configs

| # | Task | Outcome |
|---|---|---|
| 7d.1 | Update Claude Code `claude mcp` registrations to set provenance env vars | All three agent-notes servers stamp `agent_lineage=claude-opus-4-7`, `harness=claude-code` |
| 7d.2 | Same for agy's `~/.gemini/config/mcp_config.json` and any OpenCode config | Cross-agent attribution works end-to-end |

7d is mechanical and lives outside the repo, but the plan is incomplete without it landing.

## 6. Considered and rejected

These items came from Gemini's `generalization_proposals.md` (2026-05-21). They were considered, rejected, and recorded here so the next reviewer doesn't re-open them without new evidence.

| # | Proposal | Rejection rationale |
|---|---|---|
| R1 | Pluggable storage adapters (SQLite, Git-JSON) | Plan 001 decisions 1, 3, 22 committed to one Postgres database. `pgvector` + HNSW + `change_log` triggers + `NOTIFY` are load-bearing; a SQLite adapter rebuilds the stack on a weaker substrate, a Git-JSON adapter loses vector search. The portability problem we actually have is RAM, mitigated by omnibus mode (Plan 001 decision 12). No friction has emerged that justifies the duplication. |
| R2 | Provenance in `change_log.platform_metadata JSONB` | Reshaped, not rejected. Plan 001 decision 23 explicitly says trigger-/query-load-bearing attributes belong in columns; only unbounded extras stay in JSONB. Decision 27 splits this into both halves. |
| R3 | Multi-transport HTTP/SSE/WebSocket hub | Current consumers (sf2, substrate, sf1) all share one homelab Postgres. Stdio works. "Multi-node swarms" is a speculative use case, not a real one. The "central hub" is the existing DB. |
| R4 | CRDT federation of memory state across nodes | Vector embeddings, supersedes chains, and the `links` 9-column composite PK have no natural CRDT representation. Research project, not a feature. |
| R5 | Autonomous LLM-driven memory synthesis ("merge these 10 dupes into a guideline") | Ships confident-but-wrong canon. The decision-grade output that matters is the dupe candidate list (decision 31 keeps that); the synthesis step is the unsafe half and removed. |
| R6 | Half-life / time-based memory decay | "Old = wrong" is a vibes-based policy. Staleness is a flip with a *reason* (decision 30), not a clock. Time-based decay can be added later if a concrete pattern of stale-by-age emerges that an agent or human couldn't catch. |

## 7. Risks and mitigations

- **Provenance NULL backfill**: existing `change_log` rows have NULL `agent_lineage` / `harness` / `environment`. Acceptable — we didn't track these before; pretending we did is dishonest. Mitigation: NULL is a queryable value; "show me unattributed history" is itself a useful query.
- **Harness env vars not set everywhere**: agy / OpenCode / future harnesses may not all stamp the vars. Mitigation: doctor surfaces what's set (Phase 7b.3); documentation includes a copy-pasteable env block for each harness; columns are nullable so missing values don't break writes.
- **Path-based resolution ambiguity**: a path under `/projects/substrate/lib/foo` should resolve to `substrate`, not to a hypothetical `substrate-lib` project. Mitigation: longest-prefix-match on `projects.repo_root` (decision 33). Edge case (path doesn't match any project) returns an error, not a guess.
- **`stale=true` is human-meaningful but search-invisible by default**: users may write memories and never see them again if they get marked stale by another agent. Mitigation: `mark_stale` always writes a `change_log` event; `history` and `changes_since` surface it; `include_stale` opt-in keeps the data discoverable.
- **`suggest_duplicates` on memories at scale**: cosine-over-embeddings is O(n²) for pair generation. Mitigation: use the existing HNSW index path (KNN per row, threshold filter); cap output at `limit`. Same shape as the working breadcrumbs implementation.

## 8. Open questions

1. **Should `breadcrumbs` get the same `stale` flag?** Arguably yes (terminal statuses like `resolved`/`wont_fix` already cover it, but a `stale-after-shipped` semantic differs). Deferred — bring up in Phase 7c review.
2. **Should `resolve_project` cache the path → project map in-process?** `projects` is small (4 rows today) and read-mostly; probably no, but if `tools/list` round-trip becomes a hot path we'll measure.
3. **Provenance on `breadcrumbs.body` updates that happen via `update_breadcrumb`**: confirmed yes — `change_log` already records `event='updated'`; provenance lands there.

## 9. Status

Proposed; ready for peer review (sonnet / kimi / deepseek round). Plan 001's review discipline applies: rejections welcome, but each must come with new evidence, not a restatement of the proposal it's rejecting.
