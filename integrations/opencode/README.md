# agent-notes opencode plugin

Plan 007 Piece 2 — opencode enforcement hooks.

## What it does

- **Session-start orientation:** Injects `agent-notes orient --json` output into the system prompt of every new session, so the agent starts with open breadcrumbs, recent changes, and active memories already loaded.
- **Compaction reconciliation:** Appends a reconciliation checklist to the session-compaction prompt, enforcing the `/end` discipline (close breadcrumbs, file new ones, run `/reflect`, commit) before context is lost.

## Installation

1. Ensure `agent-notes` is installed and on `PATH`:
   ```bash
   cd /projects/agent-notes
   uv pip install -e ".[test]"
   ```

2. Ensure `AGENT_NOTES_DSN` is set in your environment.

3. Add the plugin to `~/.config/opencode/opencode.json`:
   ```json
   {
     "plugin": [
       "/projects/agent-notes/integrations/opencode/index.js"
     ]
   }
   ```

4. Restart opencode.

## Configuration

| Environment variable | Default | Description |
|---|---|---|
| `AGENT_NOTES_ORIENT_TIMEOUT_MS` | `15000` | Timeout for `agent-notes orient` subprocess |
| `AGENT_NOTES_DSN` | — | Postgres connection string (required) |

## Troubleshooting

- **"no directory for session … skipping orientation"** — the session was created before the plugin loaded, or the directory field is missing. The plugin only orients sessions it sees created via `session.created` event.
- **"orient failed"** — `agent-notes` is not on PATH, the project is not registered (`agent-notes init`), or the DSN is unreachable. Check `client.app.log` output.

## Limitations

- The plugin does not run `git` or `agent-notes` commands itself; it only injects prompts. The agent still decides whether to act on them. This is the same enforcement model as Claude Code's `settings.json` SessionStart hook — the harness makes the prompt non-optional, but the agent chooses the tool calls.
- The `experimental.session.compacting` hook is opencode's pre-compaction seam. If opencode changes the hook name, the plugin will need updating.
