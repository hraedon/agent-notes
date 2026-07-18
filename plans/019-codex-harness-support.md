# Plan 019 — Codex harness support for the agent face

**Status:** Phases 1, 2, 3.1, and 3.3 landed by 2026-07-18. The direct installer
renders skills plus hash-owned lifecycle groups with dry-run and surgical
uninstall; the component plugin bundles the same canonical definitions; doctor
reports plugin/direct/duplicate/stale/absent and trust-unverified states; and a
built-wheel install was verified independently of the source checkout. Phase
3.2 (live skill compatibility) and Phase 4 remain.

## Corrections applied during implementation (2026-07-17)

- **WI-2.1 / WI-2.2 landed (2026-07-18).** A narrow `agent-notes codex-hook`
  adapter consumes only event name, cwd, and Stop's loop flag. SessionStart
  renders the existing orientation query into a 7,000-character,
  metadata-only developer context: note bodies, memory names, work-item titles,
  git subjects, learned recall, transcript, prompts, assistant messages, model
  values, and credential configuration are never emitted. Stop resolves the cwd
  to its registered project, attempts the
  existing outbox reconciliation, skips repeated Stop passes, and always emits
  `{\"continue\":true}`. Malformed input, unavailable stores, unregistered
  projects, and reconciliation errors degrade without blocking. Direct wiring
  merges only two hash-owned groups into `$CODEX_HOME/hooks.json`; install and
  uninstall preserve Cairn/user hooks and refuse to adopt or remove unowned,
  duplicated, or modified same-command groups. The plugin uses Codex's default
  `hooks/hooks.json` discovery, and tests require that file to equal the Python
  canonical definition. Doctor names plugin, direct, duplicate, stale, and interactive
  `/hooks` trust-unverified states. Remaining proof is an authenticated live
  Codex session and Windows execution evidence.
- **Plugin bundle moved out of the repository root (2026-07-18).** A live
  marketplace proof found that a root `.codex-plugin/plugin.json` caused Codex
  to stage the entire 5.1 GiB checkout, including `.venv`, and fail with
  `ENOSPC`. The component-owned source is now the bounded
  `plugins/agent-notes/` tree. Its bundled skills are byte-checked against the
  canonical top-level `skills/` sources and its hook document is checked
  against the Python canonical definition, so the packaging copies fail tests
  on drift rather than becoming a second implementation.

- **Suite Plan 007 component plugin packaged (2026-07-18; source location
  superseded above).** The first version used a repository-root
  `.codex-plugin/plugin.json` whose `skills` field pointed directly at the
  canonical `./skills/` tree. The obsolete
  `skills/opencode/` non-skill placeholder was moved into the OpenCode
  integration documentation because Codex correctly rejected a child of a
  plugin skill root that lacked `SKILL.md`. The resulting plugin passed the
  plugin-creator schema validator and a real isolated Codex CLI marketplace
  add/list/install/idempotent-reinstall/remove cycle.
- **WI-3.1 / WI-3.3 landed (2026-07-18).** Doctor now emits a named
  `codex_harness` check. An absent direct-install manifest is informational
  (suite plugin health is suite-owned); once the manifest exists, missing,
  modified, untracked, legacy-unhashed, and source-outdated skills are stale
  failures that name the affected skill. A clean wheel build was unpacked into
  an isolated import root, discovered all eight packaged canonical skills, and
  completed `install-harness codex` followed by an idempotent no-op reinstall
  under a temporary home.

- **Decision 2 RE-RESTORED to `$HOME/.agents/skills` (2026-07-18, WI-022).** An
  earlier note in this section "corrected" Decision 2 to `$CODEX_HOME/skills`
  based on codex 0.144.1's `skill-creator`/`skill-installer` naming that path as
  a user-skill auto-discovery path. That reading was too narrow and put
  agent-notes in conflict with the authoritative suite install-harness contract
  (§2 Codex: "User-scoped shared skills live separately under
  `$HOME/.agents/skills`"; dated 2026-07-17) and with acb's own Plan 007
  Decision 4. Sol's suite audit flagged both agent-notes and acb writing to the
  wrong place. Skills now install to `$HOME/.agents/skills/<name>/SKILL.md`
  (home-relative), matching claude/hermes/codex sharing one shared skills root
  across the suite; the ownership manifest remains under
  `$CODEX_HOME/.agent-notes-harness.json` as a stable sidecar. `doctor
  _check_skills_installed` was updated to probe the new location. This is the
  authoritative decision; the prior `$CODEX_HOME/skills` text below is retained
  only as historical record of the reversion.
- **WI-022 — false-green on missing skills source fixed (2026-07-18).**
  `_repo_skills_root()` defaulted to the package-internal
  `agent_notes/skills/`, which is absent under editable installs (the wheel
  `force-include` in `pyproject.toml` only populates it for built wheels). The
  resolver now prefers the package-internal path and falls back to the
  repository-root `skills/` (four parents up from `cli/skills.py`). Separately,
  `install-harness` no longer reports `installed / no_op: true` when zero skills
  are discovered: per contract §4 that state is reserved for an
  already-installed idempotent install, so an empty/missing source now yields
  `status: failed, no_op: false` with an explicit `status: "missing"` action in
  both real and dry-run modes.
- **WI-1.3 superseded by the hardened Plan 007 (2026-07-17).** `all` stays the
  stable set (claude, opencode) and Codex is promoted into it *atomically across
  all components* only after conformance — so Codex remains an explicit target
  here, NOT part of `all`. Matches acb and the suite contract.
- **WI-1.2 "user-modified skill not overwritten" is now fully implemented
  (2026-07-17).** The manifest stores ``skills`` and ``agents`` as
  ``{name: sha256_hash}`` dicts. On install, if the on-disk content differs
  from the source and the on-disk hash does not match the recorded installed
  hash, the skill is treated as user-modified and preserved (not overwritten).
  On uninstall, the same hash check prevents removal of user-modified skills.
  Conflicts are surfaced in both JSON actions (``status: "conflict"``) and
  stderr warnings. Legacy list-format manifests (``["name1", "name2"]``) are
  handled via ``_normalize_hash_map`` — entries without hashes get ``None``,
  preserving the old always-update/always-remove behavior for backward
  compatibility. This applies uniformly to all harnesses (claude, opencode,
  codex, hermes) and to opencode subagent definitions.

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
