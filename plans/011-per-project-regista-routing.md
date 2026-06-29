# Plan 011 — Per-project regista routing (flag-flip unblocker)

**Status:** Proposed 2026-06-29. Implementation in progress.
**Author:** Opus 4.8
**Strategic role:** The production converged store (see
[[reference-production-regista-store]] / dossier Plan 010 §6) is one regista
**project (schema) per software-project**. But the agent-notes write path
(`face_factory.get_face()`) is a process-wide singleton bound to a STATIC
`cfg.project`, so flipping `AGENT_NOTES_REGISTA_WRITES=1` would misroute every
project's writes into one schema. This plan makes the write path route to the
regista project matching the current software-project, so the flag-flip is safe.

## Decision

**Route via a context variable set at the CLI's project-resolution choke point**,
not by threading a project argument through the 7+ `get_face()` call sites.

- `cli/common.py:_resolve()` already resolves the current project for every
  command. After it resolves `proj_slug`, it sets a `face_factory` contextvar to
  the mapped regista project name.
- `get_face()` reads the contextvar (falling back to `cfg.project`) and returns a
  **per-project cached face** (one `Regista`/schema per project name).
- Slug→schema mapping is `slug.replace("-","_")` then `validate_project_name`
  (regista forbids hyphens), matching the migration.
- Test injection (`set_face_for_test`) installs a project-agnostic override face
  so the existing ~394 tests keep working unchanged.

## Work items

- **WI-1** — `face_factory`: per-project face cache (`dict` keyed by regista
  project name) + `regista_project_name(slug)` helper + `set_current_project()` /
  `current_project()` contextvar + test override. `reset_face()` clears all.
- **WI-2** — `cli/common.py:_resolve()` sets the routing context from the
  resolved project. Read-only commands setting it is harmless (only writes read
  the face).
- **WI-3** — tests: keep existing green via the override; add a routing test
  (two projects → two schemas/faces, no cross-contamination).

## Out of scope (follow-ups)

- dossier multi-project fronting (it's single-project today) — its own plan.
- reconcile/outbox per-project iteration — works via the default today; revisit
  if the outbox needs to drain per project.
- Actually flipping `AGENT_NOTES_REGISTA_WRITES=1` in prod — a deliberate
  operator step after this lands + is validated.
