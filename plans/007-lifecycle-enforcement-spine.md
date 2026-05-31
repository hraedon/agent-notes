# Plan 007 — Lifecycle enforcement spine + cross-harness instantiation

**Status:** proposed 2026-05-31 — **HIGH priority** (the load-bearing one)
**Author:** Opus 4.8 (GR-055 review session, with principal)
**Strategic role:** Makes the "memory layer" claim *true* instead of aspirational.
agent-notes is DB-canonical and dogfooded daily; what it lacks is a reason the
lifecycle actually gets used. md breadcrumbs drifted because keeping them current
was optional. The fix is not a better sync — it's making read/write
**non-optional**, instantiable per project in one command, and tier-one on **both**
Claude Code and opencode.

## Thesis

- **DB is the single source of truth.** md-as-source is retired (see sf2 BC-228).
  There is no file↔store sync to drift, because there is no second store.
- **Lifecycle is enforced by the harness, not chosen by the model.** The only
  mechanism that makes agent behavior non-optional is harness hooks (Claude Code
  `settings.json`) / plugin events (opencode). Skills alone can't — the model
  still decides whether to invoke them. That optionality is the original disease.
- **opencode is tier-one, not a follow-up.** In sf2 the implementers, test
  authors, and integrators run on opencode — that is where the artifact work and
  the drift happen; Claude Code is mostly the cross-family reviewer. Enforcement
  that exists only on the Claude Code side guards the *least* drift-prone surface.
  Both harnesses ship together or the goal isn't met.

## What already exists (don't rebuild)

- DB store (breadcrumbs/memories/reflections), local embeddings, `change_log`.
- Skills cross-harness: `install-skills --target claude|opencode` already emits
  start/end/file-breadcrumb/find-breadcrumb/add-memory/reflect/update-breadcrumb
  to `~/.claude/skills` and `~/.config/opencode/command`.
- `init` registers a project (repo_root → slug) by longest-prefix match.

The gap is everything below: the **enforcement layer is greenfield on both
harnesses**, and `init` stops at registration.

## Pieces

### 0. Error-contract hardening — GATING PREREQUISITE

Every command, in `--json`, returns a structured result *or* a structured
`{"error","code"}` on stdout — never empty-as-failure. (Partially done: the
find/query/search silent-failure fix shipped on `main`; finish the audit across
all commands and the `_resolve` message-preservation path.) This is a
prerequisite, not polish: the enforcement hooks shell out to the CLI and parse
JSON, and a hook **cannot** distinguish "no results" from "the call failed" if
failure is empty. Build this first.

### 1. `init` as full instantiation

`agent-notes init <repo>` does the whole onboarding, idempotently:
- register the project;
- verify/repair DSN + PATH discoverability (the breadcrumb-010 failure: CLI not
  on PATH, `AGENT_NOTES_DSN` unset, project unregistered — all silent today);
- install skills for **both** harnesses;
- install the enforcement hooks/plugins for **both** harnesses (piece 2);
- run `doctor` and print a definitive "set up / here's what's missing".
Success criterion: a new project joins the system — enforcement included — in one
command.

### 2. Enforcement layer — built twice over the shared CLI

The CLI is the shared substrate; each harness's hook just shells out to it.
- **Claude Code** (`settings.json` hooks):
  - `SessionStart` → inject orientation: open breadcrumbs + relevant memories +
    a "what changed since last session" digest (from `change_log`).
  - `Stop` / `PreCompact` → reconciliation gate: prompt to file/close breadcrumbs
    and record a reflection for what changed. **The gate must accept "nothing
    durable changed"** — enforce the *consideration*, not a quota, or agents learn
    to file noise to pass it.
- **opencode** (plugin events):
  - map the equivalents (session-start orientation; session-idle / pre-exit
    reconciliation) via the opencode plugin API.
  - respect the known loader quirks: no named exports (crashes the loader), use
    `promptAsync` not `prompt`, and the SDK does not throw on 4xx (check status
    explicitly). See the harness-wiring reference.

### 3. Librarian project at `/projects`

Register an agent-notes project rooted at `/projects` that acts as the librarian:
- holds workspace-level / cross-cutting memory and an index of registered
  projects;
- by longest-prefix resolution it is the **fallback** for any path not under a
  more-specific project, so specific projects resolve to themselves and
  everything else lands at the librarian — cross-project discovery for free.
- **Required guard:** when resolution lands on the librarian *fallback* rather
  than an exact project, the CLI must say so (e.g. `resolved_via: "librarian-fallback"`
  in JSON). Otherwise "you forgot to `init` this project" looks identical to "the
  global view" — the silent-misread failure mode, reintroduced.

### 4. Cross-project reference ergonomics

Surface what mostly exists: reference another project's item as
`project:identifier` in `get`/`find`; ensure the links/wikilinks graph spans
projects; keep `--scope workspace|global`. This is the "easy to reference from
other projects" requirement; it's largely exposure, not new machinery.

## Risks (honest)

- **Enforcement only works if it's cheap and high-signal.** A slow `SessionStart`
  or a nagging `Stop` gets disabled within a week, and then lifecycle is optional
  again with extra steps. **Keep the embedding model off the hot path** — the
  `--skip-embed` flag we added to `doctor` is the tell that it's a ~30s cold-load
  drag. Orientation hooks use structured filters + recency, not vector search.
- **Hook reliability depends on piece 0.** See above — JSON error contract is
  load-bearing for hook correctness.
- **opencode parity is the hard part and the most important.** Budget for it
  explicitly; do not let it slip to "phase 2" or the drift-heavy surface stays
  unguarded.
- **Reconciliation rubber-stamping.** Covered in piece 2 — accept "no change."

## Sequence

1. Error contract (piece 0) — gating.
2. `init` full instantiation (piece 1).
3. Enforcement hooks — **Claude Code and opencode together** (piece 2).
4. Librarian project (piece 3) + cross-project ergonomics (piece 4).
5. Migrate sf2 to DB-canonical and retire its md files (sf2 BC-228); fold in
   `substrate` and `v1` (also null `repo_root`).

## Downstream / out of scope here

- **regista boundary.** agent-notes' `change_log` is a private event log living
  next to regista's. Decide whether it *becomes* regista events (making
  agent-notes the first real consumer of regista's model) — separate decision,
  not this plan's MVP.
- **Provenance emission on mutations** (the agent-provenance tie-in) — layers on
  once the lifecycle spine exists; tracked separately.
- **Hybrid search** (`plans/006`) — explicitly deferred / optional.
