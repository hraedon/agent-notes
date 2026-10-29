# agent-notes OpenCode 2 plugin

OpenCode 2 lifecycle hooks. The V1 `experimental.chat.system.transform` /
`experimental.session.compacting` implementation was removed by OpenCode's
deliberate V2 plugin API break; this package targets V2 only.

## One canonical skill tree

OpenCode commands are rendered from the same `skills/<name>/SKILL.md` files
used by Claude, Hermes, and Codex. `agent-notes install-skills --target
opencode` removes the `name:` frontmatter field and writes
`~/.config/opencode/command/<name>.md`; there is deliberately no parallel
OpenCode skill tree. If wording ever has to diverge, use a sibling
`SKILL.opencode.md` override rather than copying the full tree (Plan 004 §9
Q4).

## What it does

- **Session-start orientation:** the `context` hook injects
  `agent-notes orient --json` output into the system prompt on the first model
  request of each session, so the agent starts with open work items, recent
  changes, and active memories already loaded.
- **Mailbox delivery (Plan 025 L1):** on each user prompt the `prompt` hook runs
  `agent-notes msg inbox --claim --json` once, with `OPENCODE_SESSION_ID` set
  in the child's environment and the session's directory as its cwd. The CLI
  resolves the session as `<vm>/opencode/<session id>` (D5) and heartbeats its
  `agent_sessions` row, so `agent-notes who` shows it and direct messages can
  target it. Claimed messages are framed as peer content, not operator
  instructions (D6), and a second `context` hook splices them into the next
  request's messages just before the user's turn and persists them with
  `ctx.session.synthetic` (the same mechanism OpenCode's own Plan-mode
  reminder uses). They never go through the system prompt. Notices, which the
  CLI lists but never claims, are shown once per session.
  The claim is best effort: a 2 s timeout, no output on any failure, and at
  most one spawn per prompt (concurrent firings share it, a repeat within
  500 ms is skipped and waits for the next prompt).
- **Compaction reconciliation:** the `compaction` hook appends the
  reconciliation checklist to the summary request, enforcing the
  `/end` discipline (close work items, file new ones, run `/reflect`, commit)
  before context is lost.

## Installation

Preferred (copies the package and writes the V2 config for you):

```bash
agent-notes install-harness opencode
```

This installs the package at `~/.config/opencode/agent-notes` and writes:

```json
{
  "plugins": ["./agent-notes"]
}
```

It also removes the obsolete V1 `plugin` entry
(`.../integrations/opencode/index.js`) if present. Reload or restart OpenCode
afterwards.

Manual source-checkout install: OpenCode 2 loads a plugin **directory** and
resolves `index.js` at its root. Do not point at the file, and do not import
`@opencode/plugin` from a file-path plugin:

```json
{
  "plugins": ["/projects/agent-notes/integrations/opencode"]
}
```

## Configuration

| Environment variable | Default | Description |
|---|---|---|
| `AGENT_NOTES_ORIENT_TIMEOUT_MS` | `15000` | Timeout for `agent-notes orient` subprocess |
| `AGENT_NOTES_MSG_TIMEOUT_MS` | `2000` | Timeout for the per-prompt `agent-notes msg inbox --claim` |
| `AGENT_NOTES_BIN` | `agent-notes` | CLI executable to spawn (tests point it at a fake) |
| `AGENT_NOTES_DSN` | — | Postgres connection string; the CLI also reads its own config file |

## Troubleshooting

- **"orient failed"** — `agent-notes` is not on PATH, the project is not
  registered (`agent-notes init`), or the DSN is unreachable. V2 has no
  `client.app.log`; the plugin logs to stderr, which lands in the OpenCode
  server log.
- **No orientation** — the session's directory is resolved with
  `ctx.session.get`; there is no `session.created` event dependency. Check the
  server log for `[agent-notes]` lines.

## Limitations

- The plugin does not run `git` or `agent-notes` commands itself; it only
  injects prompts. The agent still decides whether to act on them. This is the
  same enforcement model as Claude Code's `settings.json` SessionStart hook —
  the harness makes the prompt non-optional, but the agent chooses the tool
  calls.
- No idle wake. OpenCode 2.2 has no Stop-equivalent hook, so a message that
  arrives while the session is idle waits for the next prompt (L1). L3 idle
  wake for OpenCode comes through Paseo (`paseo send`), not from this plugin.
- A message is claimed when the prompt starts and handed over on the next
  context build; if that build never happens (the prompt fails first), the
  claimed message is not redelivered.
- The hooks are OpenCode 2's `ctx.session.hook("prompt")`,
  `ctx.session.hook("context")` and `ctx.session.hook("compaction")`, plus
  `ctx.session.synthetic`. If OpenCode changes that API, the plugin will need
  updating.

## Tests

`tests/test_opencode_plugin.py` drives `index.js` under Node (`node` must be
on PATH; the tests skip without it) through `tests/opencode_plugin_harness.mjs`,
which fakes OpenCode's `ctx`, and `tests/opencode_fake_cli.cjs`, which stands in
for `agent-notes` (via `AGENT_NOTES_BIN`) so the real spawn path is exercised.
