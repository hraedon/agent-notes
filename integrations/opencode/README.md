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
- The hooks are OpenCode 2's `ctx.session.hook("context")` and
  `ctx.session.hook("compaction")`. If OpenCode changes that API, the plugin
  will need updating.
