# Desktop Codex

The component plugin bundles the canonical eight agent-notes skills and the
existing SessionStart/Stop hooks. The desktop runs the installed `agent-notes`
CLI; the plugin contains no database, signing key, DSN, or credential.

First verify `agent-notes --version`, `agent-notes invariants probe --json`,
and `agent-notes doctor --json` on the execution host. The identity probe is
read-only and opens no connection. Doctor separately checks store connectivity.
Configure a Codex principal and truthful producer metadata in its process
environment using the existing canonical suite settings. Do not reuse a
ZCode producer declaration just because both clients share a machine, and do
not alter a shared suite.env solely for one desktop session.

From this checkout register and install the bounded plugin:

```text
codex plugin marketplace add .
codex plugin add agent-notes@agent-notes-local
```

The marketplace source is `plugins/agent-notes`, not the repository root:
copying the entire checkout previously exhausted plugin staging space.
Alternatively use the desktop Plugins directory's local source. Restart or
open a new chat after installation. Select the installed `start` skill (its
displayed name may be namespaced) or ask to orient to the project's notes.

Review lifecycle hooks using `/hooks` where supported. Installation does not
prove hook trust or execution. SessionStart emits bounded project metadata;
Stop attempts outbox reconciliation and does not force another agent turn.
Do not also run `agent-notes install-harness codex` for the same profile: that
fallback installs duplicate skills and hooks. Doctor names duplicate wiring.

The fallback installer remains available when plugin installation is unsuitable:

```text
agent-notes install-harness codex --dry-run --json
agent-notes install-harness codex --json
agent-notes install-harness codex --uninstall --json
```

It hash-tracks shared skills under `$HOME/.agents/skills` and owned hooks under
`$CODEX_HOME`, preserving modified and unowned files. Use one installation
method. Remove the plugin through the desktop UI or
`codex plugin remove agent-notes@agent-notes-local`; remove direct wiring with
the fallback uninstaller only if it was installed through that method.

Use single-line CLI examples in PowerShell. For file contents use
`Get-Content -LiteralPath "<file>" -Raw`; the `reflect` skill includes the
complete PowerShell command. POSIX environment prefixes are shell-specific;
the review skill provides a PowerShell alternative. Windows checkout paths
may differ from the store's registered repository roots. If `--path` returns
`PROJECT_NOT_REGISTERED`, use explicit `--workspace <slug> --project <slug>`
selectors rather than guessing or registering a duplicate project.

Select the shared store deliberately: a request to remember something does
not automatically authorize saving it into a different harness's private
memory. Native Codex memory and agent-notes shared project memory are distinct.
Remote and hosted tasks need separately provisioned runtime and network access.

### Short orientation and freshness

`agent-notes orient --workspace default --project <slug>` shows five entries
per section and recorded update ages. `--full` shows fifteen and expands
individual change events; `--limit` overrides the section size (1–100).
`--json` includes `has_more` flags so a sample cannot be mistaken for a total.
The latest reflection is looked up separately from recent memories.

`generated_at` is the snapshot time. `updated_at`, `age_days`, and `freshness`
describe recorded edits, not verified claims. `aged` means at least 30 days
since the last update; use `--stale-after-days` to adjust that threshold.
Missing or timezone-ambiguous times are `unknown`; future dates are `future`.
The start skill now begins with this single bounded query. Automatic Codex
context retains its metadata-only filtering and includes handoff timing.

Packaging follows the [official plugin documentation](https://developers.openai.com/plugins/build/plugins).

## Verification on Windows (2026-10-15)

The four focused modules (`test_codex_plugin`, `test_codex_lifecycle`,
`test_codex_doctor`, `test_harness`) passed 77 tests before the final probe-sharing
refactor. The final plugin/lifecycle/doctor run passed 22 tests, including the
enabled/disabled/stale/absent plugin-only regression cases. Changed Python
files passed Ruff checks. All eight skill sources passed frontmatter validation
and match their bundled copies byte-for-byte.

In a temporary `CODEX_HOME`, the actual installed Codex CLI registered the
marketplace, installed/listed the plugin as enabled, removed it, and reinstalled
it. All ten cached plugin files matched the source (41,728 bytes). This proves
packaging and cache behavior. The everyday desktop profile was not changed;
trusted hooks, authenticated note/work-item writes, embedding and the full
Postgres integration suite were not exercised in that packaging check.

After the owner authorized local installation, both component plugins were
installed and enabled in the Windows Codex profile and their cached bytes
were rechecked. The CLI was updated from this source. A local profile exposed
an unfiltered plugin-list timeout on unrelated remote sources; the readiness
probe now queries configured agent-notes marketplaces directly. The final
plugin/lifecycle/doctor regression run passed 23 tests. Live doctor returned
`healthy` with plugin skills and wiring confirmed; hook trust remains unverified.
Codex shell settings now select a distinct `agent:codex-workstation` principal
and Codex harness metadata. No fixed model identity is imposed on future chats.
The prior profile config was backed up under `$CODEX_HOME/backups`.

### Orientation follow-up verification (2026-10-15)

The final focused orientation, Codex lifecycle, plugin, doctor, and installer
run passed 96 tests. Broader recall, projection, CLI, manifest, and desktop
regressions passed 82 tests; 65 PostgreSQL-dependent tests skipped because
Docker was unavailable, with 9 slow tests deselected. These runs overlap and
must not be added into a unique test total. Changed Python passed Ruff and
byte-compilation; both start-skill copies passed skill validation. Optional
mypy was unavailable in the development environment.

A fresh wheel was built and installed into the existing Windows CLI tool
environment without changing dependencies (regista remains at the locked
0.7.2). The local plugin was refreshed and all ten cached files matched the
source. Live read-only checks confirmed five-entry defaults, fifteen-entry
full views, timestamp/age fields, clipping flags, and the separately retrieved
81-day-old latest reflection. Rendering that live payload through the installed
Codex adapter produced 657 characters and omitted memory names and note bodies.
Doctor returned healthy; automatic hook trust and execution remain unverified.
