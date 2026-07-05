# Plan 012 — Go-live cutover: regista as the agent SoT (cross-harness, file cleanup, AGENTS.md)

**Status:** In progress 2026-06-29. Claude harness is LIVE; remainder for team execution.
**Author:** Opus 4.8
**Strategic role:** Activate the converged store ([[reference-production-regista-store]])
as the single source of truth for project issues from an agent perspective, and
retire the old physical artifacts + document the new system. The store, migration,
and per-project routing are done (agent-notes Plan 011); this is the operational
cutover.

> **Progress 2026-06-29 (Track A, opencode session):** WI-1 and WI-3 are
> **complete and adversarial-reviewed**; WI-2 done for 4/6 repos (2 deferred
> with gates — see D5). See "Decisions 2026-06-29" below for the one design
> deviation from the literal WI-1 wording and why.

## Already done (2026-06-29)

- **Store + data:** `regista` DB on the shared Postgres host, 16 per-project schemas,
  canonical workflow, 865 items migrated + verified (hash chains replay clean).
- **Routing:** agent-notes per-project write routing (Plan 011), CI-green,
  live-validated.
- **Claude harness LIVE:** `AGENT_NOTES_REGISTA_DSN` / `_HMAC_KEY_PATH` /
  `_WRITES=1` added to `~/.claude/settings.json` `env` (alongside the existing
  `AGENT_NOTES_DSN`). Takes effect next Claude session. Live CLI write verified
  (a breadcrumb routed to the `agent_notes` schema + mirrored to the native
  projection).

So: **new agent work via the Claude harness now writes to regista** (the SoT),
mirrored to the native `work_items` projection that the CLI reads. The native
op_log store remains as read-only history.

## WI-1 — Wire the remaining harnesses (the rest of "go live")

Every harness that runs `agent-notes` must inject the same three env vars (regista
config is env-only — `core/config.py`). Pattern from `~/.claude/settings.json`:

- `AGENT_NOTES_REGISTA_DSN` (from `~/.config/regista/regista.env`)
- `AGENT_NOTES_REGISTA_HMAC_KEY_PATH=~/.config/regista/keys.json`
- `AGENT_NOTES_REGISTA_WRITES=1`

**opencode** especially (non-interactive launch skips `.bashrc`; see
[[reference-harness-wiring-wake-notes.md]]): inject via its config, not a shell
profile. Verify with a real `breadcrumb file` per harness (confirm `pending_sync:
false` + the item in the right schema). **Do NOT set `AGENT_NOTES_REGISTA_PROJECT`**
— per-project routing derives it from the resolved project.

Optional hardening: `AGENT_NOTES_OUTBOX=1` for never-fail writes (local append +
reconcile) if regista availability becomes a concern. MVP runs fail-fast (off).

## WI-2 — Remove physical breadcrumb files

The file-based breadcrumbs predate the DB and are now stale/duplicative (the CLI
no longer reads them; see [[reference-breadcrumb-store-divergence]]). Per repo:

1. Discover: `breadcrumbs/`, `breadcrumbs/active/`, `OPEN_BREADCRUMBS.txt`,
   `*.breadcrumb.md`, file-breadcrumb dirs (sf2 is the heaviest; cert-watch /
   usage-dashboard have stale ones).
2. **Before deleting, reconcile against regista** — anything open in a file but
   not represented in that project's regista schema should be filed first (don't
   silently drop live work). The migration covered the DB projection, not loose
   files.
3. Remove the files in a dedicated commit per repo ("retire file breadcrumbs;
   regista is SoT"). Drop any tooling/hooks that wrote them.

## WI-3 — Update AGENTS.md across repos

Each project's `AGENTS.md` should describe the new system so agents file/track
issues the right way:
- regista is the SoT for work-items; the agent face is the `agent-notes` CLI/skills
  (`breadcrumb file/update/find`), which write to the project's regista schema.
- The human face is dossier (when [[dossier Plan 011]] lands).
- Do NOT create physical breadcrumb files.
- Note the slug→schema mapping is automatic (hyphens→underscores).

**Copy-paste-ready template:** `plans/templates/AGENTS-worktracking-section.md`
(the block between the `BEGIN/END: work-tracking section` markers). It is
project-agnostic — drop it into each repo's `AGENTS.md`, replacing any older
breadcrumb/file-based instructions. Commands in it were verified against the live
store.

## WI-4 — Validate + safety

- After each harness flip: file + read-back a real item; confirm regista + native
  agree.
- **Rollback:** remove the three env vars (or set `AGENT_NOTES_REGISTA_WRITES=0`)
  → writes revert to the native op_log path; nothing lost. `~/.claude/
  settings.json.bak-pre-regista` is the backup.
- Durable secrets: the regista DB password + HMAC key are 0600 local files and in
  Vault (`kv/homelab/regista`). Back up the HMAC key (loss = unverifiable chain).

## Sequencing

WI-1 (finish go-live) → WI-2/WI-3 (cleanup + docs) can run per-repo in parallel →
dossier Plan 011 (human face) lands separately. Tonight's MVP (agent SoT via the
Claude harness) is met by the "already done" section above.

## Decisions 2026-06-29 (Track A execution)

**D1 — WI-1 wiring via config-file fallback, not per-harness env injection.**
The plan's WI-1 literally says "inject the same three env vars" into each
harness. For opencode that is fragile and empirically broken: opencode.json has
no top-level `env` field (only per-MCP `env` and a `shell.env` plugin hook), and
a non-interactive opencode launch skips `~/.bashrc` — confirmed live (the session
that did this work had **zero** regista env vars despite Claude settings.json
having them). Rather than per-harness env surgery, we mirrored the **exact
pattern that already fixed this class of bug for the native DSN** on 2026-05-31:
`RegistaConfig` (`core/config.py`) now reads a `regista` block from
`~/.config/agent-notes/config.json` as a fallback **beneath** the env var (env
stays top priority, so Claude settings.json is byte-for-byte unaffected). One
change wires opencode **and every future non-interactive launcher** with zero
opencode.json edit and no restart. The operator's `~/.config/agent-notes/config.json`
(0600, already held the native DSN password) gained a `regista` block sourced
from `~/.config/regista/regista.env`; backup at `config.json.bak-pre-regista`.
Adversarial review (kimi) caught 3 real defects (file-bool parsing `"false"`→True,
empty-env DSN, non-dict JSON raise) — all fixed; 7 coverage tests added.

**D2 — hermetic test isolation.** Giving `RegistaConfig` a file fallback meant
DB write-path tests would read the operator's host config and route test writes
to **prod** regista. Added a session-scoped autouse `_hermetic_config` fixture in
`tests/conftest.py` that pins `AGENT_NOTES_CONFIG` to an empty `{}` temp file and
clears host regista env, so regista stays off unless a test opts in. Full suite
green (415 tests).

**D3 — WI-3 rollout scope.** 5 active repos had physical-file breadcrumb
instructions replaced with the canonical work-tracking section: **regista**,
**gpo-lens**, **agent-provenance** (also fixed a stale MCP-tool reference),
**software-factory-2**, and **cert-watch** (surgical). Skipped: `software-factory`
(retired v1), `usage-dashboard` (already migrated), `agent-notes` (is the tool).
The canonical template had an invalid `urgent` severity (valid set is
low/medium/high/critical) — fixed in the template and all 4 full-swap repos.
Adversarial review (kimi) caught leftover `breadcrumbs/README.md` references in
sf2 (orientation, known-issues) + a stale cross-ref + wording nits — all fixed.

**D4 — sf2 go-live blocker (stale `repo_root`).** Dogfooding the wired CLI to
file the sf2 preflight work item surfaced `PROJECT_NOT_REGISTERED` for
`/projects/software-factory-2`: sf2's registered `repo_root` was the stale
`/projects/sf2` (which no longer exists). Fixed via `UPDATE projects SET
repo_root='/projects/software-factory-2' WHERE slug='sf2'` (keeps slug/schema
`sf2`). This was silently breaking sf2 agents' entire CLI surface — every other
repo's `repo_root` was already correct. The golden-run preflight
(`scripts/agent_golden_run.py`) still reads `breadcrumbs/README.md`; migrating it
to `agent-notes breadcrumb find --severity critical` is a **prerequisite** to
removing sf2's files (WI-2) and is tracked as **sf2 `WI-001`**.

**Rollback (WI-4):** set `AGENT_NOTES_REGISTA_WRITES=0` (env) or remove the
`regista` block from `~/.config/agent-notes/config.json` → writes revert to the
native op_log; nothing lost. The config-file fallback is additive.

**D5 — WI-2 outcome (reconcile-first guardrail applied per repo).** Physical
breadcrumb files removed from **4 repos** (working-tree, uncommitted — one
commit per repo pending): **usage-dashboard** (empty scaffold), **cert-watch**
(107 files; 3 open items verified in regista first), **gpo-lens** (32 files; 4
open items **filed into regista first** — they had no DB link), **agent-provenance**
(9 files; 5 live items — AP-001/002/003/004/005 — **filed first**; 4 resolved
were history). Two repos **deferred with gates**: **sf2** — golden-run preflight
still reads `breadcrumbs/README.md`; migrate it first (sf2 `WI-001`); and
**regista** — its 315 design breadcrumbs are **the only copy** (regista's own
schema had just 1 item — the 865-item migration skipped regista's own project),
so they are not stale/duplicative; BC-312/313 (proposed bugs) were re-filed and
the bulk migration is tracked as **regista `WI-004`**. Lesson: the "files are
stale/duplicative" premise must be **verified per repo**, not assumed — regista
broke it.
