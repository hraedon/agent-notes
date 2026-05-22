# Plan 002 — Onboarding Ergonomics, Bug Cleanup, and Deferred Curation

Status: Revised v0.2 (2026-05-22). v0.1 bundled three orthogonal mini-features; v0.2 re-prioritizes around what real production use has actually demanded, and slots in the active-breadcrumb bug cleanup as a precondition. Provenance and curation move to "do when triggered" instead of "do next."
Scope: Targeted follow-on to Plan 001. Adds path-based project resolution, an onboarding CLI, and a queued-but-not-blocked backlog. Does NOT touch the storage substrate or the projection question (that lives in Plan 003).
Consumers: same as Plan 001.
Supersedes: §1–§9 of Plan 002 v0.1 (the structure is preserved; the prioritization changed).

## 1. Why now

Plan 001 closed. Five hours of production use surfaced:

1. **Bug cluster around projection + project registration** (BC-006, BC-007). The "search before filing" workflow is broken cross-project. Projections silently divert to `/tmp/` when `breadcrumbs_dir` is unset. Both are foundational — they invalidate the dedup story other features lean on.
2. **"Where am I?" friction.** Every kind tool takes `(workspace, project)`. Agents know their cwd. Bridging that gap is the highest-payoff single change visible in production traffic.
3. ~~"Who wrote this?"~~ — Real but speculative. Today's only writer is Claude Code. Provenance is cheap to add and the migration cost grows with row count, but it doesn't unblock anything visible right now. Demoted from §2 of v0.1 to §6 of v0.2.

Plan 003 takes up the larger architectural question (drop markdown projection, add web frontend) that BC-006 hints at. Plan 002 stays small and operational.

## 2. Design decisions

Numbered to extend Plan 001's sequence (decisions 1–26 reserved). v0.1's decisions 27–34 are kept where they survived re-prioritization; their numbers are preserved for traceability.

32. **`agent-notes init <path>` is a first-class CLI.** (Was decision 32 in v0.1, unchanged.) Walks up to find the git root; defaults workspace=`default`, project=dirname, `breadcrumbs_dir`=`breadcrumbs` relative to repo root, `repo_root`=absolute path. Idempotent upsert. Reason: registering a project today requires a Python one-liner against `core.db`. One command, one path, done.

33. **Path-based project resolution is a core helper, not per-kind logic.** (Was decision 33 in v0.1, unchanged.) New core tool `resolve_project(path)` returns `{workspace, project, repo_root}` by longest-prefix match against `projects.repo_root`. Kind tools accept either `(workspace, project)` *or* `path`, with `path` taking precedence when both are passed.

35. **Bug cleanup blocks the rest of this plan.** BC-006 and BC-007 are in scope for Phase 7a as preconditions, not as a separate phase. Reason: 7a's `agent-notes init` populates `breadcrumbs_dir` (which makes BC-006's silent /tmp fallback impossible) and `resolve_project` is the same code path that needs to participate in `find_breadcrumbs`' WHERE-clause assembly (BC-007). Fixing them together avoids touching the same code twice.

36. **Provenance, staleness, and `suggest_duplicates` are deferred to "when triggered."** (Replaces v0.1's Phase 7b and 7c as the next steps.) The schema migrations are pre-designed (§4 below) so the trigger event is "second harness starts writing" / "stale memory bites" / "duplicate cluster bites" — at which point the corresponding sub-phase ships in a session, not a month.

37. **Decisions 27–31 from v0.1 stand as-is, just not next.** The schema delta and tool surface in v0.1 §3–§4 for provenance and staleness are still the correct shape when the trigger fires. Captured here so future-me doesn't redesign them.

38. **Plan 002 gets one round of peer review, not five.** (Procedural.) Plan 001's five-round discipline was load-bearing for greenfield architecture. Plan 002 is additive ergonomics; one round is enough.

## 3. Tool surface delta (Phase 7a only)

- **`resolve_project(path)`** — new core tool. Returns `{workspace, project, repo_root}` or a structured `PROJECT_NOT_REGISTERED` error.
- **All kind tools that take `(workspace, project)`** — gain optional `path` argument; `path` wins when both are passed.
- **`find_breadcrumbs`** — WHERE-clause assembly fixed (BC-007). Parametric test covers (`project` set/unset) × (additional filters set/unset).
- **`query_breadcrumbs`** — `is_open` status mapping documented in the tool description (BC-007 follow-up).
- **`file_breadcrumb`** — refuses to project when `breadcrumbs_dir` is unset; returns `PROJECT_NOT_CONFIGURED` with the remediation in the error (BC-006). DB row is still written. *Note: if Plan 003 lands first, this whole branch goes away — see Plan 003 §5 for the ordering question.*
- **`agent-notes init <path>`** — CLI (not MCP tool). Idempotent.

## 4. Schema delta (deferred phases only — kept for traceability)

When provenance ships (decision 36 trigger fires):

```sql
ALTER TABLE change_log
    ADD COLUMN agent_lineage TEXT,
    ADD COLUMN harness       TEXT,
    ADD COLUMN environment   TEXT,
    ADD COLUMN provenance    JSONB NOT NULL DEFAULT '{}';
CREATE INDEX idx_change_log_lineage ON change_log (agent_lineage, changed_at DESC);
```

When staleness ships:

```sql
ALTER TABLE memories
    ADD COLUMN stale        BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN stale_reason TEXT,
    ADD COLUMN stale_at     TIMESTAMPTZ;
CREATE INDEX idx_memories_active_fresh
    ON memories (project_id, name) WHERE active = true AND stale = false;
```

No changes to `breadcrumbs`, `links`, `workspaces`, `projects`, or vocabularies for Phase 7a.

## 5. Phased implementation

### Phase 7a — Onboarding ergonomics + active-bug cleanup (one session)

| # | Task | Outcome |
|---|---|---|
| 7a.0 | Confirm omnibus + doctor in-flight changes are committed and `tests/test_omnibus.py` is green (carries forward BC-001/003/004 resolution) | No drift between active resolved/ dir and main; preconditions clear |
| 7a.1 | Fix BC-007: `find_breadcrumbs` WHERE-clause / parameter list assembly; `query_breadcrumbs` `is_open` documented | Parametric test exercises all four arg combinations; both tools work cross-project |
| 7a.2 | Fix BC-006: `file_breadcrumb` refuses to write projection when `breadcrumbs_dir` is unset; error names the remediation | No more silent /tmp writes; DB row still written; close 006 |
| 7a.3 | `agent-notes init <path>` CLI (decision 32) | New project registered idempotently with one command |
| 7a.4 | Core helper `resolve_project(path)`; register as MCP tool on every kind server (decision 33) | Path-based resolution available to all kinds |
| 7a.5 | Extend kind tools' input schemas to accept `path` alongside `(workspace, project)`; precedence path > explicit args | Existing call sites unchanged; new call sites can pass `path` |
| 7a.6 | Tests + AGENTS.md update | `make test` green; convention documented |

### Phase 7b — Writer provenance (deferred, ships when triggered)

Trigger: second harness (agy, OpenCode, or other) starts writing into the shared DB, OR the user explicitly asks to debug attribution.

Tasks unchanged from v0.1 §5 (7b.1–7b.6). Schema delta in §4. Estimated one session when triggered.

### Phase 7c — Curation primitives (deferred, ships when triggered)

Trigger: memory table grows past ~500 rows AND search noise becomes a complaint, OR a duplicate cluster bites and a human asks for the dupe-finder.

Tasks unchanged from v0.1 §5 (7c.1–7c.5). Schema delta in §4. Estimated one session when triggered.

### Phase 7d — Removed

v0.1's "wire harness configs" phase was mechanical and lives outside the repo. It ships alongside 7b naturally; no plan slot needed.

## 6. Considered and rejected

Inherited from v0.1 §6 (R1–R6) and still rejected for the same reasons. Two new entries:

| # | Proposal | Source | Rejection rationale |
|---|---|---|---|
| R7 | Add `projects:` array argument to `find_breadcrumbs` / `search_memory` for cross-project search | Kimi 2026-05-22 | Plan 001 decision 10 split kind-local from cross-kind for performance. `search_all_notes` already exists. The friction is that agents don't know it exists; the fix is a better tool description and `/start`-skill orientation, not widening the kind tools. |
| R8 | `file_breadcrumbs` (plural) batch tool | Kimi 2026-05-22 | /end files 2–3 breadcrumbs per session. 10× round-trip reduction on tiny numbers is YAGNI. Revisit when a workflow files >20 at once. |

## 7. Risks and mitigations

- **BC-006 fix interacts with Plan 003.** If Plan 003 drops projection entirely, BC-006's `PROJECT_NOT_CONFIGURED` error becomes vestigial. Mitigation: see Plan 003 §5 ordering question. The most conservative path is "fix BC-006 inside Phase 7a, then have Plan 003 delete the projection branch wholesale" — slightly more code churn, but it lets 7a ship without waiting for Plan 003 review.
- **Path resolution ambiguity** (kept from v0.1): longest-prefix match on `projects.repo_root`; no-match returns a structured error, never a guess.
- **`agent-notes init` running outside a git repo**: detect and either use the path as-is or refuse — pick at implementation time, document either way.

## 8. Open questions

1. **7a-vs-Plan-003 ordering.** Discussed in Plan 003 §5. Default: 7a first because it unblocks bug fixes; Plan 003 deletes obsoleted code afterward.
2. **`agent-notes init` and existing on-disk breadcrumbs** (substrate's case): should `init` ingest existing `breadcrumbs/*.md` if it finds them, or stay agnostic? Probably ingest (idempotent, one-line invocation), but mark for confirmation during 7a.

## 9. Status

Revised v0.2; ready for one round of peer review (decision 38). Plan 001's "rejections need new evidence" discipline still applies.
