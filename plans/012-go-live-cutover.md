# Plan 012 — Go-live cutover: regista as the agent SoT (cross-harness, file cleanup, AGENTS.md)

**Status:** In progress 2026-06-29. Claude harness is LIVE; remainder for team execution.
**Author:** Opus 4.8
**Strategic role:** Activate the converged store ([[reference-production-regista-store]])
as the single source of truth for project issues from an agent perspective, and
retire the old physical artifacts + document the new system. The store, migration,
and per-project routing are done (agent-notes Plan 011); this is the operational
cutover.

## Already done (2026-06-29)

- **Store + data:** `regista` DB on mvmpostgres01, 16 per-project schemas,
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
