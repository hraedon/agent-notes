# Plan 017 — Suite cohesion: the agent face, deployable

**Status:** In progress 2026-07-09 — WI-1.1 (config), WI-2.1/2.2/2.3
(install-harness + SUITE.lock + opencode review bridge), WI-3.1 (doctor --json,
suite `ok`/`degraded` shape), WI-3.2 (doc-drift cutover — regista-as-SoT
confirmed, AGENTS.md updated, no physical breadcrumb write paths), WI-4.1
(secret-backend resolution + Windows code-review validation) landed and tested;
WI-4.2 per-user config layering + `principal_id` resolution seam implemented
(suite.env reader + `resolve_principal_id` with local dev fallback; live IdP
binding environment-gated); WI-4.3 publication gate (identifier-gate + CI job +
checklist + dry-run audit report) added, gate green. Remaining: WI-4.1 Windows
live host validation (code-reviewed only — no Windows host in this session),
WI-4.2 live LDAP/Entra `principal_id` binding (environment-gated — requires a
live IdP connection), WI-4.3 the public flip itself (owner-gated — dry-run audit
complete, destructive scrub not executed).
**Author:** Claude (Fable 5), from the 2026-07-02 agent-suite deployment review
**Strategic role:** agent-notes is the suite's agent face. For a suite deployment
it must read the shared config, install its harness wiring (skills + hooks) as a
repeatable step rather than the hand-edited `~/.claude/settings.json` it is today,
report health in the common shape, and finish the go-live cutover so the physical
breadcrumb files are gone and regista is the single source of truth. See
`/projects/agent-suite-blueprint.md` (Phase B). agent-notes' own lifecycle/kernel
roadmap is unaffected.

## Ground truth at time of writing

- agent-notes is public (`hraedon/agent-notes`), CI green on 3.13/3.14. The
  agent SoT MVP went live 2026-06-29: `AGENT_NOTES_REGISTA_*` + `WRITES=1` wired
  into `~/.claude/settings.json` env; per-project face routing (Plan 011) landed;
  the native op_log is now read-only history, rollback = `WRITES=0`.
- **The config surface is large** (~30 `AGENT_NOTES_*`
  vars incl. `AGENT_NOTES_REGISTA_DSN`, `_HMAC_KEY_PATH`, `_PROJECT`,
  `_REQUIRE_SSL`) — the same three shared facts as the other tools, under private
  names.
- Go-live remaining (agent-notes Plans 012 written, not fully executed): wire other
  harnesses, **remove physical breadcrumb files**, update AGENTS.md. Working
  branches exist (`plan-014-degrade-gate-parity`, recent Plan 016 review-gate CLI).
- `deploy/` holds systemd units (bridge, requeue, trigger-loop) — the Tier-B
  daemon pieces, currently optional.
- Harness wiring today is hand-edited settings + installed skills; there is no
  single "install agent-notes into this harness" step a fresh machine can run.

## Principles this plan must hold

- **Adopt the shared facts, keep the rest.** Converge `AGENT_NOTES_REGISTA_DSN`
  /`_HMAC_KEY_PATH` onto `REGISTA_DSN`/`REGISTA_KEY_PATH` (regista Plan 025), with
  a one-release alias. `AGENT_NOTES_PROJECT` stays (per-tool slug); the embed
  model, bridge, principal, and outbox vars stay `AGENT_NOTES_*` (tool-specific).
- **Install is a command, not a manual edit.** The suite must be able to wire
  agent-notes into a sanctioned harness reproducibly; hand-editing settings.json
  is exactly the hodgepodge the blueprint targets.
- **Regista is the source of truth; the cutover finishes.** Physical breadcrumb
  files are removed as part of becoming a clean suite component — two stores is
  the divergence the family already fought (see the breadcrumb-store-divergence
  reference).

---

## Prep — install-harness contract review (WI-2.1 gap analysis)

**Reviewed 2026-07-02** against
[`agent-suite/docs/install-harness-contract.md`](../../agent-suite/docs/install-harness-contract.md)
and agent-notes' existing `install-skills` command (`src/agent_notes/cli/skills.py`).
The contract is the shared interface the four harness-wiring components converge
on; this is what agent-notes must build (and what it already has) to conform.

### What agent-notes already has (`install-skills`)

`agent-notes install-skills --target <claude|opencode> [--dry-run] [--json]`
discovers `skills/*/SKILL.md`, and for each: claude → `~/.claude/skills/<name>/SKILL.md`
(verbatim), opencode → `~/.config/opencode/command/<name>.md` (frontmatter `name:`
line stripped). It is idempotent (per-file `created`/`updated`/`unchanged`),
`--dry-run` acts on nothing, `--json` emits a per-skill result list. So **skill
discovery + per-target dest paths + file-level idempotency are done and reusable.**

### Gaps to close for WI-2.1 (contract → agent-notes)

| # | Contract req't (§) | agent-notes today | Gap |
|---|---------------------|--------------------|-----|
| 1 | `install-harness <harness>` positional, `claude\|opencode\|all` (§1) | `install-skills --target <claude\|opencode>` (no `all`; `--target` optional, defaults claude) | Add `install-harness` verb (keep `install-skills` as the skills-only sub-step it wraps); `<harness>` positional incl. `all` |
| 2 | Env vars wired into `~/.claude/settings.json` env block (claude, §2) | none — install-skills writes skill files only | **New:** merge `REGISTA_DSN`/`REGISTA_KEY_PATH`/`AGENT_NOTES_*` into the harness env block (JSON merge, never overwrite — §3 rule 2) |
| 3 | Env vars wired into `~/.config/opencode/opencode.json` env block (opencode, §2) | none | **New:** same merge into opencode config |
| 4 | opencode plugin transforms registered: `experimental.chat.system.transform` + `experimental.session.compacting` (§2) | `integrations/opencode/index.js` exists but install-skills does **not** wire it into the config | **New:** register the plugin path + the two transform hooks into opencode.json (merge) |
| 5 | `--uninstall` reverses prior install, idempotent on clean profile (§1, §3 rule 4) | not implemented | **New:** track own additions (sentinel comment / sidecar manifest / known key set) and remove exactly those; user-authored config untouched |
| 6 | `--user <principal_id>` per-user overlay, no shared-state touch (§1) | not implemented | **New:** write `principal_id` + default project into the per-user harness layer (depends on WI-1.1 config layering + the suite per-user model) |
| 7 | `--dry-run` emits contract action shape `{tool, harness, user, actions:[{kind,path,keys,detail}], no_op}` (§4) | own shape `{target, dry_run, skills:[{name,src,dest,status}]}` | **New:** emit the contract shape (action kinds `create_file` / `merge_json`); `no_op:true` when already wired |
| 8 | Exit codes 0 (success/no-op) / 1 (failure) / 2 (dry-run) (§7) | `EXIT_SUCCESS`/`EXIT_GENERIC`/`EXIT_NOT_CONFIGURED(=2, meaning not-configured)` | Align: 2 = dry-run informational; reserve a distinct code/not-configured handling so 2 isn't overloaded |
| 9 | No secret clobber — existing secret kept + warned (§3 rule 3) | N/A (no env wiring yet) | Must hold once §2 lands: if `REGISTA_KEY_PATH`/DSN already set, keep + warn, never overwrite |
| 10 | Dual-harness validation: both targets + regression guard (doctor before/after on existing opencode) (§5) | both targets install skills, but no regression guard | **New:** a validation step asserting an existing opencode config still resolves after install (cheap — both run locally) |

### Sequencing note (confirmed)

- **WI-1.1 (canonical `REGISTA_*`) is a hard precondition** — install-harness sets
  `REGISTA_DSN`/`REGISTA_KEY_PATH` in the harness env block, so the config-resolution
  precedence must exist first. Do WI-1.1, then WI-2.1.
- `install-harness` is a **superset of `install-skills`**: it runs the skill install
  (existing, reusable) **plus** the env/plugin/hook wiring (new). Keep `install-skills`
  as the skills-only path (tests + the `install-skills` AC in Plan 004 §9 Q4 still hold);
  `install-harness` calls into it and adds the config-merge layer.
- The opencode plugin (`integrations/opencode/index.js`) is already written — WI-2.1
  only needs to *register* it into `opencode.json`, not author it. Low risk.

---

## Phase 1 — Adopt the suite config contract

### WI-1.1 — Canonical `REGISTA_*` with aliased fallback
- Resolve DSN/key via the suite precedence, preferring `REGISTA_DSN`/
  `REGISTA_KEY_PATH`, falling back to `AGENT_NOTES_REGISTA_DSN`/`_HMAC_KEY_PATH`
  with a deprecation warning. `AGENT_NOTES_REGISTA_WRITES` and the per-tool slug
  stay.
- **AC:** the CLI operates reading only `suite.env`; legacy vars still work and
  warn; precedence tests cover the overlap; live write path unchanged.

## Phase 2 — Harness install as a repeatable step

### WI-2.1 — `agent-notes install-harness <claude|opencode>`
- One idempotent command that installs the skills and wires the harness config
  (the env + hooks agent-notes needs) for a named harness — replacing the manual
  `~/.claude/settings.json` edits. Re-runnable; reports what it changed; a
  `--dry-run` prints the diff; an `uninstall-harness` reverses it.
- **AC:** on a clean profile, `install-harness claude` produces a working wired
  harness (skills present, env set, hooks registered); re-running is a no-op;
  `--dry-run` mutates nothing; uninstall cleanly reverses.

### WI-2.2 — Package + pinned regista
- Pin the regista dependency to a SHA (recorded in `SUITE.lock`) rather than the
  local `[tool.uv.sources]` path / `@main`, so the agent face and spine are a
  known-good pair. Document the pre-cache path for the embedding model so a work
  install needs no model-download egress at first run.
- **AC:** a fresh `pip install` from the pin resolves regista at the locked SHA;
  the embed model can be pre-staged and the first run does no network fetch when
  it is.

## Phase 3 — Health + go-live closeout

### WI-3.1 — `agent-notes doctor --json`
- Conform the existing `doctor` to the suite shape (regista Plan 025 WI-3.1):
  `{component:"agent-notes", version, regista:{reachable, project, chain_ok,
  writes_enabled}, checks:[skills installed, harness wired, embed model present,
  outbox drained, …]}`.
- **AC:** validates against the suite shape; degrade mode
  (`coordinator-absent`) is a named status, not a failure; an unreachable regista
  is clean.

### WI-3.2 — Finish the cutover (remove physical breadcrumb files)
- Execute the remaining go-live steps: confirm regista holds the authoritative
  log, remove the physical breadcrumb files/dirs, and update AGENTS.md so the
  next agent doesn't write to a store the suite no longer reads. This is the
  "single source of truth" guarantee the suite depends on.
- **AC:** no code path writes physical breadcrumb files; a migration/verify step
  confirms nothing was lost (cache rebuild from op-log / regista matches);
  AGENTS.md reflects regista-as-SoT.

## Phase 4 — Cross-platform, secrets, multi-user, publication

### WI-4.1 — Resolve secrets through the backend; Windows support
- Resolve DSN password / signing key via `regista.secrets.resolve` (Plan 025
  WI-1.2). Ensure the CLI + `install-harness` work on **Windows** (Claude
  Code/opencode on Windows), not just Linux — the pgvector connection, the skills
  install, and the harness wiring are the OS-specific surfaces to verify.
- **AC:** the CLI reads its secret from each backend (gated tests); `install-harness`
  succeeds on a Windows profile and a Linux profile; no plaintext key on disk.

### WI-4.2 — Per-user config + `principal_id` from the shared identity source
- Honor per-user config layering (blueprint §2.6) so each human's agents attribute
  to *their* `principal_id`, sourced from the same workplace identity source
  dossier binds (WI-4.2 there) — multiple users share one regista with distinct
  attributed agent writes.
- **AC:** two users' agents writing to the same shared project produce distinctly
  attributed events; the per-user overlay sets `principal_id` without touching the
  system config; no cross-user attribution bleed.

### WI-4.3 — Publication gate (sanitize before flipping public)
- Before agent-notes flips public (blueprint §3): filter-repo scrub, CI
  identifier-gate, publication-review checklist. Watch for the known cross-project
  dep gotcha (`agent_waked` import) already `importorskip`-guarded — keep it so CI
  stays green post-publication.
- **AC:** history clean of work-domain identifiers (verified); identifier gate
  green; checklist complete before the flip.

## Sequencing & notes

- **Harness note (2026-07-02, revised):** work deployment is Claude-first, but the
  operator runs **both harnesses locally**, so WI-2.1 (`install-harness`) supports
  **both** targets and a dual-harness validation confirms the cohesion changes don't
  regress an existing opencode config (blueprint §4). The `adversarial-review` skill
  should record the reviewer's lineage (regista Plan 027) so a same-model review is
  logged honestly rather than counted as independent.
- **WI-2.3 — opencode adversarial-review bridge (implemented 2026-07-05):**
  `install-harness opencode` now installs the `adversarial-review` skill *and*
  the opencode subagent definitions under `.opencode/agents/`. The coding
  subagents (`glm`, `kimi`) are granted `task: adversarial-reviewer-*` so they can
  dispatch reviewers; the adversarial reviewers are read-only with limited
  `agent-notes`/`git` bash access so they can drive the review gate without
  mutating code. This closes the last gap between the CLI review-gate surface
  (Plan 016) and opencode subagent invocation.
- Depends on regista Plan 025 WI-1.1 (config) + WI-1.2 (secrets) + WI-2.1
  (`provision` + service role).
- **WI-2.1 (`install-harness`) is the highest-leverage item** for the blueprint —
  it turns "hand-wire each machine" into a bootstrap step, and it's what lets acb
  (Plan 005) and the harness wiring stop being manual.
- Mirror dossier Plan 013's config-adoption pattern (land dossier first as the
  reference). The Tier-B systemd daemons in `deploy/` stay optional; the suite's
  first pass does not require the requeue/trigger-loop timers.
- Parallel-safe with the in-flight kernel work (Plans 014–016); this plan touches
  config/install/doctor surface, not the op-CRDT core.

---

## Implementation log

### WI-1.1 — Canonical `REGISTA_*` with aliased fallback (implemented 2026-07-03)

`core/config.py` resolves the three shared suite facts — DSN, signing-key path,
SSL flag — through canonical env vars (`REGISTA_DSN`, `REGISTA_KEY_PATH`,
`REGISTA_REQUIRE_SSL`), falling back to the legacy `AGENT_NOTES_REGISTA_*`
aliases with a one-shot `DeprecationWarning` (module-level `_WARNED_LEGACY` set
guards against spam). `AGENT_NOTES_REGISTA_WRITES` and
`AGENT_NOTES_REGISTA_PROJECT` stay tool-specific. The two admin scripts
(`dedup_regista_source_identifiers`, `migrate_to_regista`) resolve through
`regista_config()` so the whole CLI surface reads suite.env.

### WI-2.1 — `install-harness` (implemented 2026-07-03)

`cli/harness.py` implements the suite install-harness contract. Reusable
skill-install helpers from `cli/skills.py`; `install-skills` stays the
skills-only sub-step (Plan 004 AC intact). Reviewed by an independent
adversarial reviewer (same-model — GLM 5.2); the one blocking finding
(manifest-drift on re-install with a reduced env-var set) was fixed and tested.

**Decision D1 — opencode env goes in the agent-notes config file, not
opencode.json.** The opencode config schema (`additionalProperties: false` on
`Config`) has **no top-level `env` key** (verified against
`https://opencode.ai/config.json`). Writing one would violate the schema and
risk breaking an existing opencode setup — exactly the regression the contract
§5 guard targets. So env vars are written to agent-notes' own config file
(`~/.config/agent-notes/config.json`), the existing harness-independent fallback
that `config.py` already reads (Plan 012 WI-1). The plugin path is registered
in `opencode.json["plugin"]` (schema-supported); the two transform hooks ship
inside the plugin, so registering the path wires them. For **claude**, env goes
into `settings.json["env"]` (the schema-supported, working mechanism). This
asymmetry is forced by the two harnesses' schemas, not a design preference.

**Decision D2 — manifest tracks *managed* keys, not *newly-written* keys.** A
sidecar manifest (`.agent-notes-harness.json`) records what install-harness
owns. On a re-install (idempotent no-op), the manifest is re-derived from the
*previous* manifest: a "matching" key (already present, equal to our value) is
managed only if a prior install wrote it, so a user's pre-existing matching
value is never clobbered on uninstall (contract §3 rule 3). Previously-managed
keys still present in the config but no longer in `env_values` (user unset a var
between installs) are preserved so uninstall removes them (review B1). The same
preservation applies to skills removed from the repo between installs.

**Decision D3 — opencode subagents ship in `.opencode/agents/` and are
installed by `install-harness`.** Skills describe *what* to do; opencode
subagents define *who* can do it with which tool permissions. The four
adversarial reviewer agents are read-only (`edit: deny`) with limited bash
(`agent-notes*`, `git log*`, `git diff*`, `git status*`) so they can execute
the review workflow without mutating code. The coding agents (`glm`, `kimi`) are
granted `task: adversarial-reviewer-*` so a working agent can dispatch a reviewer
from inside an agent-notes session. This matches the Claude Code
`adversarial-review` skill but adds the opencode subagent invocation seam. The
agent files are tracked in the harness manifest so `uninstall` removes them.

**Remaining WI-2.1 gaps (deferred):** `--user` for opencode is a warned no-op
(principal_id now resolves from suite.env + git config per `core/actor.py`
WI-4.2, but the opencode config-file field for `principal_id` is not yet wired
— the per-user suite.env overlay is the canonical path, not the harness config).
WI-2.2 (pin regista to a SHA in `SUITE.lock`) is not yet done. The
plugin/skills/agent paths are repo-relative (works for the editable-install
deployment model; a published wheel would need `integrations/` + `skills/` +
`.opencode/` as package data — pre-existing, tracked separately).

### WI-3.1 — `doctor --json` suite shape (implemented 2026-07-04)

`scripts/doctor.py` gains `run_json()`, emitting the suite contract shape
(blueprint §2.4): `{component, version, status, regista:{reachable, project,
writes_enabled, chain_ok, mode}, checks:[{name, status, detail}]}`. Both the
CLI (`agent-notes doctor --json`) and the standalone `agent-notes-doctor --json`
use it. `status` is three-state: `healthy` (regista reachable, no failures),
`degraded` (no failures but spine absent — coordinator-absent is the default
safe mode, a non-failing named status per the AC), `unhealthy` (any check
failed). A configured-but-unreachable regista is a real failure; an unconfigured
regista is `degraded`, never `unhealthy`. The human-readable `run()` path now
runs the same suite-layer checks (chain, skills, harness-wired, regista
reachable) so both surfaces agree.

Two independent adversarial reviews (Kimi-K2.7, GLM-5.2 — same-lineage,
recorded honestly) drove the hardened shape: all exception details are
secret-safe (`_sanitize_conn_error` returns only the type name, never
`str(exc)`, so no DSN/username leaks into JSON/logs); `_check_chain_ok`
verifies agent-notes' own op-log chain (the chain the write-through face
replays into regista — regista's *own* event-log chain is regista's doctor
job); the hermetic test fixture (`tests/conftest.py`) now clears the canonical
`REGISTA_*` suite env vars, not just the legacy aliases (a host `REGISTA_DSN`
previously enabled regista for every test).

### WI-2.2 — `SUITE.lock` + pre-cache docs (implemented 2026-07-04)

`SUITE.lock` records the regista git SHA + envelope/workflow versions this
release is tested against (`d7d156c`, envelope v4, workflow v1). The
`[tool.uv.sources]` editable mapping resolves regista from `../regista`;
`SUITE.lock` is the SHA that pair is known-good at (a CI check comparing
`git -C ../regista rev-parse HEAD` against the lock is a follow-on). The embed
model pre-cache path is documented in `deploy/SUITE-INSTALL.md` (HF_HOME
pre-population + `HF_HUB_OFFLINE=1 doctor --check-embed` confirmation), so a
work install needs no model-download egress at first run.

### WI-2.2 follow-on — SUITE.lock drift check (implemented 2026-07-04)

`scripts/check_suite_lock.py` + `make check-suite-lock` compare the local
sibling regista checkout against the `SUITE.lock` pin. Informational by
default (exit 0); `make check-suite-lock-strict` fails on drift for a
pre-release gate. CI is N/A (it installs regista from `@main`); this is a
local-dev guard against a stale/moved sibling checkout. The `SUITE.lock` SHA
was bumped to `ccf3304` (regista HEAD at WI-4.1 implementation, which ships
the `_secrets.py` resolver this item consumes).

### WI-4.1 — Resolve secrets through the backend (implemented 2026-07-04)

`core/secrets.py` routes the suite's two shared secrets — the regista DSN and
the signing key-set — through `regista.secrets` (regista Plan 025 WI-1.2), so
neither must live in plaintext config. The blueprint (§2.5) makes this the
custody contract for a regulated deployment.

- **DSN** (`resolve_dsn`): a literal DSN passes through unchanged (no provider
  prefix → no regista import, zero regression); a backend ref
  (`env:VAR`/`vault:...`/`azure:...`/`file:/path`) resolves at use time. The
  native `AGENT_NOTES_DSN` is routed through the same resolver for parity
  (literal-safe). `config.resolve_dsn` now resolves *all* of its inputs
  uniformly — explicit arg, env var, and file value.
- **Key-set manifest** (`materialize_key_manifest`): regista's `KeySet` already
  resolves per-key `secret_ref` from the backend, so the manifest itself need
  not contain secrets. A bare path / `file:` ref is read by regista directly
  (hot-reload works; `~` is expanded here because regista's KeySet does not).
  A *remote* ref (`env:`/`vault:`/`azure:`) materializes to a 0600 temp file,
  scrubbed at clean process exit and tracked per-face in `reset_face`.

**Doctor** gains `_check_secrets_backend` (resolves configured refs once;
`skip` when none configured). `_check_regista_reachable` now resolves the
backend DSN before probing, so a `REGISTA_DSN=env:...` deployment reports
reachability correctly.

**Security posture (adversarial-review hardened).** Two independent review
rounds (Kimi-K2.7 + GLM-5.2 round 1; Nemotron-3 round 2 — all same-lineage,
recorded honestly per Plan 027) drove the hardened shape:

- **Silent-literal-fallback guard.** When a remote provider's SDK is absent
  (`hvac`/`azure-identity`), regista's `_detect_prefix` reclassifies the ref
  as `literal` and returns the ref *string* unchanged. The DSN path now
  detects this (`result == normalized` for a `_REMOTE_PROVIDERS` prefix) and
  raises loudly; the manifest path's `_validate_manifest_bytes` rejects the
  non-JSON ref string. `_REMOTE_PROVIDERS` is `{vault, azure}` only — `env`/
  `file` are always registered and can never silently fall back.
- **`__cause__` suppressed** (`from None`) so a logged traceback cannot echo
  the original regista exception (which may include the ref / a Vault path).
  All operator-facing messages are exception-type-only. This is a deliberate
  security-over-debuggability trade (reproduce locally with the raw resolver
  for full backend diagnostics).
- **Prefix normalization.** regista's provider names are lowercase and its
  `_detect_prefix` does not lowercase; `ENV:VAR` would be silently
  reclassified as literal. `_normalize_for_regista` lowercases only the
  prefix before the regista call, so any case is accepted.
- **`akv:` unsupported.** regista registers no `akv` provider (only
  file/env/literal/vault/azure); the blueprint's `akv:` syntax awaits a
  regista-side provider. `azure:` is the supported prefix here.

**Remaining WI-4.1 gaps (deferred):** Windows runtime validation is
code-reviewed only (no Windows host in this session) — `mkstemp`/`%TEMP%`/
0600-chmod-is-POSIX-only are handled, but a real Windows profile run is a
follow-on. Vault/Azure integration tests are gated on the SDK being installed
(skipped cleanly otherwise, mirroring regista's pattern). A startup sweep for
orphaned `an-keys-*.json` temp files after an unclean shutdown is documented
as a recommended operator step, not implemented here.

### WI-3.2 — Doc-drift cutover (implemented 2026-07-05)

The cutover's three ACs are met: (1) **no code path writes physical breadcrumb
files** — `breadcrumb file` creates a DB work item via `WorkItemModel`;
`bc_files.py` is a one-time import path (files→DB), not a write path;
`export-index` writes an index file but that is an export, not a source-of-truth
file. (2) **AGENTS.md reflects regista-as-SoT** — the architecture paragraph
now explicitly states "Regista is the source of truth for work items" and "No
code path writes physical breadcrumb files." (3) **No physical breadcrumb
files exist** in the agent-notes repo (it is the tool, not a consumer repo;
the per-repo file removal was Plan 012 WI-2, already done for 4/6 repos with
gates on the remaining 2).

The "doc-drift" was the gap between AGENTS.md's "DB is the only source of
truth" (correct but generic) and the actual state where regista is the
specific SoT the CLI writes through. The architecture paragraph now names
regista explicitly and notes the degrade path.

### WI-4.1 — Windows code-review validation (implemented 2026-07-05)

A thorough code review of all Windows-specific code paths (16 files) confirmed
the core WI-4.1 surfaces (`secrets.py`, `harness.py`, `skills.py`, `config.py`,
`embed.py`) are correctly written for cross-platform use. Three issues were
found and fixed:

- **`cli/__init__.py` hook commands (HIGH):** the `2>/dev/null || true` and
  `>/dev/null || true` POSIX-shell syntax in Claude Code SessionStart/Stop
  hooks would fail under `cmd.exe`/PowerShell on Windows. Fixed: the bare
  commands (`agent-notes orient --path {root}`, `agent-notes outbox reconcile`)
  work on both platforms; Claude Code's hook executor handles non-zero exits.
- **`envelope.py` chmod (LOW):** `self._key_path.chmod(0o600)` was invoked
  unconditionally — a harmless no-op on Windows but inconsistent with
  `secrets.py`'s explicit `os.name == "posix"` guard. Fixed: added the same
  guard.
- **`tests/test_secrets.py` tilde-expansion test (LOW):** the test sets `HOME`
  and asserts a POSIX path, but Windows `expanduser` prefers `USERPROFILE`.
  Fixed: `pytest.mark.skipif(os.name != "posix")` guard added.

Also fixed: the git-root walk in `cli/__init__.py` used `while git_root != "/"`
(POSIX-only appearance, functionally correct via the `parent == git_root`
guard). Replaced with the cross-platform `while git_root != os.path.dirname(git_root)`
idiom (matching `src/agent_notes/scripts/init_project.py`).

**Remaining WI-4.1 gap:** a real Windows host run is still needed to fully
validate the AC. The code review confirms the *core* surfaces are ready; the
gaps were in lifecycle-hook wiring and test portability, not in the
secret-backend or install-harness machinery.

### WI-4.3 — Publication-gate audit (implemented 2026-07-05)

The identifier-gate denylist was expanded per the **adcs-lens WI-010 lesson**:
the scrub must cover **all identifier forms** — CA common name (CN), CA
hostname, NetBIOS domain name, real domain SID, certificate template names,
service accounts, and personal email — not just DNS hostnames. The denylist
sources are the samples from `adcs-lens` and `gpo-lens`.

A `git filter-repo --dry-run` audit on a bare clone (110 commits) found:
- **1 blob-content leak:** the personal email in the deleted
  `scripts/identifier-gate.py` (commits `2747b9c`/`6200f07`).
- **Author/committer identity:** the personal email across all 110 commits
  (requires `--mailmap` scrub before the public flip).
- **No other identifier forms** (CA CN, hostnames, domain SID, NetBIOS domain,
  template names, service accounts) in any blob or commit message.

The publication-review checklist (`docs/publication-review-checklist.md`) was
updated with the full audit report (§0). The destructive scrub + repo recreate
+ public flip are **not executed** — dry-run + report only, per the task scope.
The owner executes the scrub when ready.

### WI-4.2 — Per-user config + `principal_id` resolution (implemented 2026-07-09)

The multi-user keystone: each human's agents attribute to *their*
`principal_id`, sourced from the suite config layering (blueprint §2.6). Two
new modules + two modified modules implement the full precedence chain:

**`core/suite_env.py`** (new) reads the suite-wide `suite.env` files that
`agent-suite bootstrap` writes, providing the per-user overlay layer between
process env and the tool-specific config file. The precedence (blueprint §2.6 /
bootstrap-contract §2):

```
process env  >  per-user suite.env  >  system suite.env  >  tool default
```

The per-user file is at `~/.config/agent-suite/suite.env` (Linux) or
`%APPDATA%/agent-suite/suite.env` (Windows), overridable via
`AGENT_SUITE_CONFIG` (same as regista's `_config.py`). The system file is at
`/etc/agent-suite/suite.env` (Linux) or `%ProgramData%/agent-suite/suite.env`
(Windows), overridable via `AGENT_SUITE_SYSTEM_CONFIG` (agent-notes test
isolation). `load_suite_env()` returns a merged dict (per-user > system);
missing files are silently skipped. The parser mirrors regista's
`_parse_env_file` (comments, `export` prefix, quote unwrapping).

**`core/config.py`** (modified) — `RegistaConfig.__init__` now resolves the
shared suite facts (DSN, key path, SSL flag) through the full chain: process env
(canonical > legacy alias) > suite.env (canonical > legacy alias) > tool config
file. The project slug resolves `AGENT_NOTES_PROJECT` (suite canonical,
blueprint §2.6) > `AGENT_NOTES_REGISTA_PROJECT` (legacy) > suite.env > default.
A new `_env_or_suite()` helper encapsulates the env > suite.env precedence;
`_aliased_suite()` handles the legacy-alias fallback within the suite.env layer
(one-shot deprecation warning, same as the env layer).

**`core/actor.py`** (modified) — `resolve_principal_id()` is the principal_id
resolution seam. Precedence:

1. `AGENT_NOTES_PRINCIPAL_ID` env var (tool-specific override, highest)
2. `REGISTA_PRINCIPAL_ID` env var (suite canonical, process env)
3. `REGISTA_PRINCIPAL_ID` from per-user suite.env
4. `REGISTA_PRINCIPAL_ID` from system suite.env
5. git config `user.email` (local dev fallback)
6. `None`

`load_actor_config()` calls `resolve_principal_id()` so the actor's
`on_behalf_of.principal_id` is sourced from the suite layering. The
`principal_display_name` still falls back to git config `user.name`.

**Live IdP binding (environment-gated).** The live LDAP/Entra binding — where
`principal_id` is resolved from the authenticated session (dossier binds LDAP;
agent-notes adopts the one-identity-source binding) — is a **seam**, not yet
wired. `resolve_principal_id()` is the extension point: a live IdP resolver
would be inserted between the suite.env layer and the git-config fallback (or
replace the fallback entirely in a production deployment). The binding is
environment-gated: it requires a live LDAP/Entra connection, the `ldap3`/
`msgraph` SDK, and a configured identity-source endpoint — none of which are
available in this session. The local source (suite.env + git config) is the
dev/default path and is fully functional.

**AC verification.** The AC ("two users' agents writing to the same shared
project produce distinctly attributed events; the per-user overlay sets
`principal_id` without touching the system config; no cross-user attribution
bleed") is verified by `tests/test_suite_env.py`:
`test_no_cross_user_attribution_bleed` — two users with different per-user
suite.env overlays produce distinct principal_ids.
`test_per_user_overlay_does_not_touch_system` — the per-user overlay sets
principal_id without modifying the system file.

**Conftest isolation.** `tests/conftest.py` now isolates from host suite.env
files: `AGENT_SUITE_CONFIG` and `AGENT_SUITE_SYSTEM_CONFIG` are pointed at
non-existent paths, and `REGISTA_PRINCIPAL_ID` / `AGENT_NOTES_PRINCIPAL_ID` are
cleared, so a host `/etc/agent-suite/suite.env` or the operator's
`~/.config/agent-suite/suite.env` does not leak into tests (routing test writes
to production or attributing them to the operator's principal_id).
