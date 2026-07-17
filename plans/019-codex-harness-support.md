# Plan 019 — Codex harness support for the agent face

**Status:** Phase 1 landed 2026-07-17 (installer: `install-harness codex` renders
skills + ownership manifest, dry-run, uninstall, verified on-box). Phases 2 (hooks),
3.2/3.3, and 4 (live proof) remain.

## Corrections applied during implementation (2026-07-17)

- **Decision 2 corrected — skills install to `$CODEX_HOME/skills`, NOT
  `$HOME/.agents/skills`.** codex 0.144.1's own authoritative `skill-creator`
  and `skill-installer` name `$CODEX_HOME/skills` (`~/.codex/skills`) as the
  user-skill auto-discovery path; there is no `~/.agents/skills` discovery in
  this Codex version. Installing to `~/.agents/skills` would have silently placed
  skills where Codex never looks. This also keeps agent-notes consistent with the
  acb Codex adapter (both write `$CODEX_HOME/skills/<name>/SKILL.md`).
- **WI-1.3 superseded by the hardened Plan 007 (2026-07-17).** `all` stays the
  stable set (claude, opencode) and Codex is promoted into it *atomically across
  all components* only after conformance — so Codex remains an explicit target
  here, NOT part of `all`. Matches acb and the suite contract.
- **WI-1.2 "user-modified skill not overwritten" is a pre-existing cross-harness
  gap, not codex-specific.** codex reuses the existing `_install_one`
  content-compare (create/update/unchanged), identical to claude/opencode/hermes,
  which today *update* on drift. Hash-tracked preservation of a user-modified
  installed skill should be added for all harnesses uniformly; recorded, not
  faked for codex alone. Manifest + uninstall-only-recorded halves of WI-1.2 are
  done.

**Original status:** Proposed 2026-07-10.
**Author:** GPT-5.6 Sol, from the suite Codex integration audit.
**Strategic role:** Make the existing agent-notes skills and health contract
available to local Codex clients through one idempotent `install-harness`
target.

## Ground truth

- `install-harness` already owns Claude, OpenCode, and Hermes skill wiring,
  manifests, dry-run, uninstall, and tests.
- Codex discovers reusable repository/user skills in the shared
  `.agents/skills/<name>/SKILL.md` layout.
- agent-notes already resolves shared config independently of a launcher. Codex
  support therefore does not require writing DSNs, signing keys, or suite
  variables into `~/.codex/config.toml`.
- Doctor currently probes only the existing harness skill and manifest
  locations.
- Codex `SessionStart` can add developer context, while `Stop` requires valid
  JSON on successful stdout. Hooks require explicit user trust review.
- The 2026-07-10 deployment fix now includes repository-level `skills/` as
  installed wheel package data. Codex acceptance must verify that fix rather
  than recreate it.
- Scope is local Codex clients. Hosted Codex tasks do not automatically inherit
  the operator's home directory, suite network, or secret backend.

Reference: https://learn.chatgpt.com/docs/customization/overview

## Decisions

1. Reuse the canonical `SKILL.md` sources; do not fork Codex-specific copies
   unless a tested semantic difference requires one.
2. Install Codex skills under `$HOME/.agents/skills`.
3. Record ownership and hashes in
   `$CODEX_HOME/.agent-notes-harness.json` (default
   `~/.codex/.agent-notes-harness.json`).
4. Never write suite secrets or shared configuration into Codex config.
5. Uninstall only files whose recorded installed hash still matches. A
   user-modified skill is reported and preserved.
6. Doctor reports per-harness state; one green harness must not hide a requested
   Codex installation that is absent or stale.
7. agent-notes may own orientation/reconciliation lifecycle hooks, but it must
   merge alongside cairn hooks and never auto-approve Codex hook trust.

## Phase 1 — Installer

### WI-1.1 — Codex target and paths

Add `codex` to the CLI target set and model its skills root, Codex home, and
manifest path with testable environment/path overrides.

**AC:**

- `agent-notes install-harness codex --dry-run --json` emits contract-shaped
  create/update actions and writes nothing.
- Real install creates canonical skill directories and an ownership manifest.
- Re-run is a no-op.
- Paths work on Linux and Windows without hard-coded separators.

### WI-1.2 — Safe update and uninstall

Use the existing content comparison for install/update and extend the manifest
with source and installed hashes.

**AC:**

- A changed upstream skill updates deterministically.
- A locally modified installed skill is not silently overwritten or removed;
  JSON and human output name the conflict.
- Uninstall removes only unchanged agent-notes-owned files and prunes no
  unrelated directories.
- A shared skill installed by another suite operation is not deleted without
  matching ownership evidence.

### WI-1.3 — `all` semantics

Align with agent-suite Plan 007: stable `all` expands to Claude, OpenCode, and
Codex. Keep Hermes available as an explicit target until the suite formally
promotes it.

**AC:** tests assert the exact expansion and prevent a future target from
silently changing `all`.

## Phase 2 — Lifecycle integration

### WI-2.1 — Session orientation

Install an owned `SessionStart` hook that resolves the session cwd and emits
the existing agent-notes orientation as additional developer context. Keep the
adapter narrow and deterministic.

**AC:**

- Startup/resume in a registered project receives the expected orientation.
- An unregistered project or unavailable store degrades without blocking Codex.
- Hook output contains no secret and obeys Codex's documented output schema.
- The hook command works from subdirectories and on Windows.

### WI-2.2 — Stop-time reconciliation

Add a small Codex hook adapter that consumes Stop JSON, runs the existing safe
reconciliation/outbox operation for the session cwd, and always emits a valid
non-continuation JSON response. Do not use PreCompact as a checklist-injection
substitute; its output semantics do not provide that contract.

**AC:**

- Success, no-op, malformed input, unavailable store, and reconciliation failure
  all produce valid Stop output and never create a continuation loop.
- The manifest owns exact agent-notes hook entries; install/uninstall preserves
  cairn and user hooks.
- Doctor distinguishes installed-but-untrusted from actively running hooks.

## Phase 3 — Health and user experience

### WI-3.1 — Codex-aware doctor

Refactor the hard-coded probe lists into typed harness descriptors and add the
Codex skills/manifest locations.

**AC:**

- Doctor reports `codex: wired`, `codex: stale`, or `codex: absent` when
  Codex is requested/configured.
- A stale manifest or missing skill is named.
- Existing Claude/OpenCode/Hermes findings and exit semantics do not regress.

### WI-3.2 — Skill compatibility pass

Run every installed agent-notes skill in a Codex session against fixture or
isolated test data. Remove assumptions about harness-specific slash-command
syntax, tool names, or environment injection.

**AC:**

- Each skill is discoverable and can reach its documented CLI operation.
- Skills refer to `AGENTS.md`, shell commands, and file links in
  harness-neutral terms.
- Any intentionally unsupported skill is excluded with a named reason rather
  than installed broken.
- Harness-specific prose is normalized: Codex invocation and GPT lineage are
  documented without removing valid Claude/OpenCode guidance.

### WI-3.3 — Distribution completeness regression proof

Verify the newly landed package-data/resource loading through a built wheel and
keep an actionable failure if required assets are ever absent.

**AC:** a clean wheel install can run `install-harness codex` without depending
on the source checkout; a regression fails before mutation with a documented
remediation.

## Phase 4 — Proof and docs

### WI-4.1 — Recorded local proof

From a clean temporary home, install Codex support and use a live local Codex
session to file/search one note and operate one work item through regista.

**AC:** doctor is green, records carry the configured principal/model lineage,
and uninstall returns the temporary profile to its pre-install state.

### WI-4.2 — Documentation

Update README/AGENTS/CHANGELOG/install docs with Codex paths, local-vs-cloud
scope, configuration resolution, hook trust (`/hooks`), conflict handling, and
verification commands.

**AC:** the documented flow contains no manual copy step and no plaintext
secret injection.

## Out of scope

- Codex provenance hooks (agent-provenance 011).
- Capability/MCP reconciliation (acb 007).
- External wake delivery (agent-wake 006).
- Changing agent-notes storage or identity semantics.
