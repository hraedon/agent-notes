# Plan 017 — Suite cohesion: the agent face, deployable

**Status:** Proposed 2026-07-02
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

**Remaining WI-2.1 gaps (deferred):** `--user` for opencode is a warned no-op
(principal_id resolves from git config per `core/actor.py`); the full per-user
overlay model awaits the suite per-user layer (WI-4.2). WI-2.2 (pin regista to a
SHA in `SUITE.lock`) is not yet done. The plugin/skills path is repo-relative
(works for the editable-install deployment model; a published wheel would need
`integrations/` + `skills/` as package data — pre-existing, tracked separately).

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
