# Plan 025 Phase 0 spike results

Run 2026-10-29 (UTC 05:00-05:20) from the Windows workstation against the local
Paseo daemon (0.13.2) and the mvmcc02 daemon (0.13.2, via `ssh itadmin@mvmcc02`,
local `paseo` there). No repository or harness config was modified. Every Paseo
agent created was deleted (`paseo delete <id>`), and the duplicate workspaces
`paseo run` created were archived (see "Side effects"). Secrets masked: the
`PASEO_PASSWORD` and `ANTIGRAVITY_CSRF_TOKEN` values, and a test DSN password
seen in `~/.gemini/settings.json`, were never copied here.

Versions: mvmcc02 `claude` 2.1.312, `codex-cli` 0.165.0, `agy` 1.4.9 at start
and 1.4.10 at the end (agy auto-updated mid-run; binary mtime 05:08).
Windows `codex-cli` 0.165.0, `agent-notes` 1.1.1, no `agy` CLI.

Provider availability (`paseo provider ls`):

| | claude | codex | antigravity | opencode |
|---|---|---|---|---|
| mvmcc02 | available | available | available | available |
| Windows | available | available | **unavailable/disabled** (Antigravity desktop installed, no `agy` CLI) | available |

## Summary

| Spike | Result | Implication |
|---|---|---|
| (a) `send` to idle agent starts a turn | **Yes** on all 3 providers on mvmcc02 and on claude/codex/opencode on Windows | L3 idle wake is feasible. Go for Paseo as L3 |
| (b) `inspect --json` fields | **No provider session ID in `inspect --json`.** It has `Id`, `Cwd`, `Provider`, `Mode`, `Status` and so on. The provider ID is in `<PASEO_HOME>/agents/<project>/<id>.json` (`persistence.sessionId`) | The dispatcher can't get the mapping from the CLI. Prefer self-registration from env (c): the session records its own `(session_id, PASEO_AGENT_ID)` pair |
| (c) `PASEO_AGENT_ID` in the agent shell | **Yes**, all providers, both OSes. Values match the Paseo id; harness IDs match `persistence.sessionId` | D5 registration works as designed. agy has `ANTIGRAVITY_CONVERSATION_ID` (D5 says none) |
| (d) harness hooks inside Paseo sessions | **mvmcc02: yes** (Claude and Codex injected orient). **Windows: nothing injected**, because no project is registered under a Windows path (not Paseo-specific) | Paseo does not block hooks. Windows needs a project-identity fix before L1 means anything there |
| (e) agy hooks | No `hooks.json` on either machine. Hooks are documented in agy's bundled docs, in a different format than the plan assumes | See details. Desktop firing still unconfirmed empirically |
| (f) hook latency | mvmcc02 orient min 677 / median 691 ms. Windows orient (unregistered-project error path) min 872 / median 900 ms. `import agent_notes.core.db`: 252/254 ms (Linux), 356/364 ms (Windows) | A 1 s total hook budget is tight on Linux and too tight on Windows. Needs a lighter hook entrypoint |
| (h) `paseo import` of a non-Paseo Codex thread | **Works.** `send` ran a turn and appended it to the original rollout. `delete` leaves the rollout intact | Hand-started Codex sessions are wakeable once imported. Whether a live desktop window redraws is unverified |
| (i) Codex shell timeout | No fixed kill timeout for the model's shell commands in 0.165. Commands run through `exec_command` and **yield** (10 s default, 250-30000 ms range), then the model polls. Config `background_terminal_max_timeout` default 300000 ms | `msg await` must work in short slices or tolerate being backgrounded |

## (a) send to an idle agent starts a new turn

Flags checked with `paseo run --help`: `--provider`, `--cwd`, `--mode`,
`--model`, `--no-wait`, `-q`. Mode ids differ from the display names: claude
accepts `plan|default|acceptEdits|auto|bypassPermissions` (`bypass` was
rejected: `Invalid mode 'bypass' for provider 'claude'`).

mvmcc02, claude:

```
$ paseo run --provider claude --mode bypassPermissions --cwd /tmp/paseo-spike --no-wait -q "Reply with the word PONG and nothing else"
d4c8f9ae-1f2b-478d-a648-ec2f800d2927
$ paseo wait d4c8f9ae... --timeout 300      -> idle, "PONG"          (1.2 s)
$ paseo send d4c8f9ae... --no-wait --prompt "Reply with the word PING2 and nothing else"
  STATUS sent   Message sent, not waiting for completion
$ paseo wait d4c8f9ae... --timeout 300      -> idle                  (1.4 s)
$ paseo logs d4c8f9ae...
[User] Reply with the word PONG and nothing else
PONG
[User] Reply with the word PING2 and nothing else
PING2
```

Same commands, same outcome everywhere:

| Machine | Provider | Turn-2 wait | Second reply seen in `paseo logs` |
|---|---|---|---|
| mvmcc02 | claude | 1.4 s | PING2 |
| mvmcc02 | codex | 2.3 s | PING2 |
| mvmcc02 | antigravity | 23.3 s | PING2 |
| Windows | claude | quick | PING2 |
| Windows | codex | quick | PING2 |
| Windows | opencode (bonus) | quick | PING2 |

An idle agent starts a fresh turn; no `--behavior` flag is needed when idle.
`send --no-wait` returns `sent` immediately and the following `wait` blocks
until the new turn completes (no idle-before-start race in 6 runs).
Cost note: Paseo's default Claude model is `claude-opus-5-5` (the first run
cost about $0.21, mostly cache creation); pass `--model claude-sonnet-5-5` for
spikes and for anything created on a schedule.

## (b) What `paseo inspect <id> --json` exposes

Top-level keys (capitalised, unlike the lowercase of `paseo ls --json`):

```
Id, Name, Provider, Model, Thinking, Status, Archived, ArchivedAt, Mode, Cwd,
CreatedAt, UpdatedAt, LastUsage{InputTokens,OutputTokens,CachedTokens,CostUsd},
Capabilities{Streaming,Persistence,DynamicModes,McpServers},
AvailableModes[], PendingPermissions[], Worktree, ParentAgentId
```

`Cwd` is present. **The provider's session/thread ID is not.** (`paseo ls --json`
has only `id, shortId, name, provider, thinking, status, cwd, created`.)

The ID lives in the daemon's on-disk record,
`<PASEO_HOME>/agents/<project-slug>/<agent-id>.json` (`~/.paseo` on both
machines):

```
claude : persistence.sessionId = persistence.nativeHandle = 80bd6362-b85b-4e50-8df4-f4a9194c891d
         (equals ~/.claude/projects/-tmp-paseo-spike/80bd6362-....jsonl and CLAUDE_CODE_SESSION_ID)
codex  : persistence.sessionId = persistence.nativeHandle = persistence.metadata.threadId = 01a1860d-e6a7-7a63-9beb-e8c6bbe69480
         (equals the rollout file name and CODEX_THREAD_ID / CODEX_SESSION_ID)
agy    : persistence.sessionId = plugin:{"version":1,"data":{"conversationId":"ba209f77-209d-4f9c-8ad0-206eb60ec58c"}}
         (conversationId equals ANTIGRAVITY_CONVERSATION_ID)
other fields: id, provider, cwd, workspaceId, title, labels, lastStatus,
  lastModeId, config{modeId,model}, runtimeInfo{provider,sessionId,model,modeId}, attention*
```

Windows records have the same shape (`cwd` stored as `C:\...`).

Implication: do not rely on `paseo inspect` for the mapping. Options:
(1) the session registers `(harness session_id, PASEO_AGENT_ID)` itself, since
both are in its own env (spike c). Recommended. (2) read the JSON file under
`PASEO_HOME` (undocumented layout, version-fragile).

## (c) Environment visible to the agent's shell tool

Each agent ran `env | grep -E "PASEO|CLAUDE_CODE_SESSION_ID|CODEX_THREAD_ID|CODEX_SESSION_ID..."`
(PowerShell `Get-ChildItem Env:` for Windows Codex).

| Machine / provider | Variable names seen | `PASEO_AGENT_ID` equals Paseo id | Harness ID equals `persistence.sessionId` |
|---|---|---|---|
| mvmcc02 claude | `PASEO_AGENT_ID`, `PASEO_AGENT_CWD`, `PASEO_HOME`, `PASEO_PASSWORD`, `CLAUDE_CODE_SESSION_ID` | yes | yes |
| mvmcc02 codex | `PASEO_AGENT_ID`, `PASEO_AGENT_CWD`, `PASEO_HOME`, `PASEO_PASSWORD`, `CODEX_THREAD_ID`, `CODEX_SESSION_ID` | yes | yes (both vars, same value) |
| mvmcc02 antigravity | `PASEO_AGENT_ID`, `PASEO_AGENT_CWD`, `PASEO_HOME`, `PASEO_PASSWORD`, `ANTIGRAVITY_CONVERSATION_ID`, `ANTIGRAVITY_TRAJECTORY_ID`, `ANTIGRAVITY_AGENT=1`, `ANTIGRAVITY_LS_ADDRESS`, `ANTIGRAVITY_CSRF_TOKEN`, `ANTIGRAVITY_SOURCE_METADATA`, `ANTIGRAVITY_PROJECT_ID`, `ANTIGRAVITY_APP_DATA_DIR`, `ANTIGRAVITY_LS_VERSION` | yes | `ANTIGRAVITY_CONVERSATION_ID` equals the `conversationId` in the Paseo record |
| Windows claude (shell tool is Git Bash) | same set as mvmcc02 claude | yes | yes |
| Windows codex (PowerShell) | same set as mvmcc02 codex | yes | yes |

Findings worth acting on:

- `PASEO_PASSWORD` is exported into every agent's tool environment (values not recorded here). Anything the agent runs can read it. This is a Paseo property. D6's trust model should treat a Paseo-hosted session as able to drive its own daemon, and `agent-notes` must never log the environment.
- agy exposes `ANTIGRAVITY_CONVERSATION_ID`. D5 says agy "has no verified environment variable"; this is one, at least under Paseo with agy 1.4.x. Check it outside Paseo (plain `agy` TUI) before dropping D5 step 3 for agy. `ANTIGRAVITY_AGENT=1` also marks "running inside agy".
- Codex sets both `CODEX_THREAD_ID` and `CODEX_SESSION_ID`, identical here. D5 can read either.
- Not directly captured: the `session_id` on a hook's stdin versus these variables. For Claude the transcript file name equals the env var and Paseo's ID (which is how Claude defines `session_id`); for Codex the rollout name equals the thread id. Very likely equal, but assert it with a hook-side print in Phase 4.

## (d) Do the harness hooks fire inside Paseo-hosted sessions?

mvmcc02 config:

- `~/.claude/settings.json` has `hooks.SessionStart` with `agent-notes orient --path "$CLAUDE_PROJECT_DIR" --reconcile` and `gitea-orient "$CLAUDE_PROJECT_DIR"`.
- `~/.codex/hooks.json` does **not** exist. Codex hooks come from the plugin `agent-notes@agent-suite-local` (`~/.codex/plugins/cache/agent-suite-local/agent-notes/1.1.0/hooks/hooks.json`): `SessionStart` runs `agent-notes codex-hook session-start`, `Stop` runs `agent-notes codex-hook stop`. Both are trusted by hash in `~/.codex/config.toml` `[hooks.state]`.

Evidence on mvmcc02 (agents with cwd `/projects/agent-notes`, a registered repo; Claude in `--mode plan`; the prompt asked, without tools, whether the context contains orient text):

- Claude: the transcript JSONL contains `"type":"hook_success"` twice with `hookName":"SessionStart:startup`. Reply: `YES: "SessionStart:startup hook success: Orientation for agent-notes (workspace default): Open work"`.
- Codex: reply `YES` quoting `Agent-notes project orientation (bounded stored metadata; may be stale): Project:`.
- Control: with cwd `/tmp/paseo-spike` (unregistered) the Claude transcript has no hook output, and `agent-notes orient --path /tmp/paseo-spike` returns `PROJECT_NOT_REGISTERED`. Hooks fail quiet by design (`|| true`), so an unregistered cwd looks the same as "hook did not fire".

Windows (same prompt, cwd `C:\projects\agent-notes`): **both Claude and Codex answered NO.** This is not Paseo-specific:

- Windows `~/.claude/settings.json` has no hooks. The only Claude hook there is the repo's `.claude/settings.json`, which runs `agent-notes orient --path /projects/agent-notes` (a Linux path).
- Windows Codex has the plugin enabled and trusted (`agent-notes@agent-notes-local`). A plain `codex exec` (no Paseo) prints `hook: SessionStart Completed` and `hook: Stop Completed` and still answers NO. Running the hook by hand returns `{"continue":true}` with no `additionalContext`.
- Cause: `agent-notes orient` on Windows returns `PROJECT_NOT_RESOLVED ... 'C:\projects\agent-notes' is not inside a registered project`, even with `--path /projects/agent-notes` (the path is normalised to `C:\...`). None of the direct sub-directories of `C:\projects` resolves. The DB connection is fine (`agent-notes doctor`: DSN reachable, schema OK, 36 projects in workspace `default`). The shared `projects.repo_root` is the Linux path, so Windows paths don't match.

Implication: Plan 6a's Windows Codex requester needs project identity that resolves from a Windows checkout. The per-VM checkout map is needed for `orient` and hook registration too, not only for the dispatcher. Until then a Windows hook fires but registers nothing and injects nothing.

Side issue: `agent-notes doctor` crashes on Windows with `UnicodeEncodeError` (cp1252 stdout) unless `PYTHONUTF8=1`.

## (e) agy hooks, version, and the plan's agy assumptions

- `agy --version`: mvmcc02 1.4.9 at start, 1.4.10 at the end (auto-update). Windows: no `agy` on PATH, and `paseo provider ls` marks antigravity unavailable there. The Antigravity 2.0 desktop app is installed (`%LOCALAPPDATA%\Programs\antigravity`) with data in `~/.gemini/antigravity/`.
- `~/.gemini/config/hooks.json`: **does not exist on mvmcc02 or Windows.** `~/.gemini/config/` holds `config.json`, `mcp_config.json`, `projects/` (Windows also empty `plugins/`, `sidecars/`). No hook entries in `config.json` or `settings.json`.
- The agy binary contains the hooks protobuf (`PreInvocationHookArgs`, `SessionStartHookArgs`), so hooks are a real feature.
- Windows bundled docs (`~/.gemini/antigravity/builtin/skills/agy-customizations/docs/hooks.md`, `json_configs.md`, `plugins.md`) document the contract. Differences from what Plan 025 section 6 assumes:
  - The format is **named hooks**: the top-level key is a hook name, then event arrays, e.g. `{"agent-notes": {"SessionStart": [{"command": "..."}], "PreInvocation": [...], "Stop": [...]}}`. `SessionStart`, `PreInvocation`, `PostInvocation` and `Stop` are flat lists of handlers; `PreToolUse`/`PostToolUse` use a `matcher` + `hooks` wrapper. The handler is `command` (run via `sh -c` or `cmd /c`, cwd is the directory holding `hooks.json`) with `timeout` in seconds (default 30). Only `type: command` exists.
  - Locations: global `~/.gemini/config/hooks.json`, project `.agents/hooks.json`, or `<plugin_dir>/hooks.json` (plugin root only).
  - `injectSteps` accepts `systemMessage` (nested object `{"systemMessage": {"systemMessage": "..."}}`), `userMessage` (string) and `ephemeralMessage` (string, dropped after the turn). Both `SessionStart` and `PreInvocation` accept it. This matches D6.
  - Stop: `{"decision":"continue","reason":...}` re-enters the loop. Input has `executionNum`, `terminationReason`, `fullyIdle`; no `stop_hook_active`. Matches the plan. `PostInvocation` can also return `terminationBehavior: "force_continue"`.
  - Stdin (camelCase): `conversationId`, `workspacePaths`, `transcriptPath`, `artifactDirectoryPath`, `modelName`, `lastUserInput`. The `transcriptPath` directory differs by product: CLI `antigravity-cli/`, Antigravity 2.0 desktop `antigravity/`, IDE `antigravity-ide/`.
  - Hooks run synchronously and block the agent loop.
- Desktop firing: the docs describe the same `hooks.json` for Antigravity 2.0, which suggests the desktop honours it, but no hooks were present to test and none were created. **Still unconfirmed empirically** for desktop and for Windows. Add a throwaway echo hook in a scratch `.agents/hooks.json` (not the global file) and run the CLI and the desktop app once.
- agy hooks inside Paseo: not testable (none configured). The Paseo-run agy had a normal agy environment (spike c), so hooks would load from the same config roots.

## (f) Hook latency

Method: Python `subprocess.run` x5, wall clock, stdout and stderr discarded.

| Machine | Command | Runs (ms) | Min | Median |
|---|---|---|---|---|
| mvmcc02 (`/projects/agent-notes`, registered, success) | `agent-notes orient --json --limit 1` | 714, 703, 687, 691, 677 | 677 | 691 |
| mvmcc02 | `agent-notes --version` | 832, 758, 812, 774, 805 | 758 | 805 |
| mvmcc02 | `python -c "import agent_notes.core.db"` (tool venv python) | 254, 266, 252, 253, 254 | 252 | 254 |
| mvmcc02 | `python -c pass` | about 18 | 18 | 18 |
| Windows (`C:\projects\agent-notes`, **error path: project not registered**, exit 3) | `agent-notes orient --json --limit 1` | 932, 887, 880, 901, 875 (second set 919, 883, 946, 872, 900) | 872 | 900 |
| Windows | `agent-notes codex-hook session-start` | 866, 821, 838, 826, 901 | 821 | 838 |
| Windows | `agent-notes --version` | 2957, 3039, 2994, 3053, 3016 (second set 3026, 3099, 2939, 3011, 3134) | 2939 | 3026 |
| Windows | `python -c "import agent_notes.core.db"` | 369, 360, 364, 368, 356 | 356 | 364 |
| Windows | `python -c pass` | 76, 70, 73, 210, 67 | 67 | 73 |

Notes: the Windows orient number includes Python start, imports, the Postgres
connect and the failed project lookup but not the successful digest queries, so
a real success will be somewhat slower. `agent-notes --version` taking about 3 s
on Windows (versus 0.9 s for orient) is odd and repeatable; likely the
console-script launcher or an import-time check on that path, so look at it
before choosing the hook entrypoint. On Linux about 250 ms of the 690 ms is
`import agent_notes.core.db`; bare Python is about 18 ms.

Implication: a per-prompt hook at about 0.7 s (Linux) and 0.9 s or more
(Windows) is noticeable on every prompt and exceeds a 1 s total budget on
Windows. The plan's "1 s connect_timeout" bounds one step, not the total.
Options: a dedicated light hook module that avoids `core.db` imports until it
has something to say; a persistent local helper (the dispatcher already runs
per machine) that the hook asks over a local socket; or a per-prompt hook limited
to a cheap "any pending messages?" query with a minimal import set.

## (h) `paseo import` of a Codex thread not started by Paseo

- Chose `01a17480-b4b3-70c0-ad75-5eb1fb1296ce` (Codex desktop/VS Code client: `originator codex-tui`, `source vscode`; started 2026-10-25, last modified 2026-10-26, cwd `/projects`, 1254 lines). Not an active session.
- `paseo import 01a17480-... --provider codex --cwd /projects` created Paseo agent `355eec99-...` (status created; title taken from the thread's first prompt) and `paseo logs` showed the thread's history.
- `paseo send 355eec99-... --no-wait --prompt "Reply with the word PONG and nothing else. Do not use tools."` then `wait` -> idle, `PONG`. The turn was **appended to the original rollout file** (1254 to 1270 lines; last event `task_complete` with `last_agent_message: "PONG"` at 05:08Z). Same thread id, no new thread. The original client's history therefore contains the turn when it next reads the rollout. Whether an open desktop window refreshes live was not tested.
- Delete safety: Paseo's server code (`handleDeleteAgentRequest`) closes the runtime and removes the Paseo registry record and timeline; it does not call into Codex's thread store. Confirmed: after `paseo delete 355eec99-...` the rollout still exists (4,021,064 bytes, 1270 lines). Provider session files were left alone for all spike agents too.
- Caveat: importing makes the thread drivable by Paseo while the user may also have it open in the desktop app. Paseo does not coordinate two writers on one thread.

Implication: a hand-started Codex desktop/TUI session is wakeable after `paseo import`, but that is a manual step that produces a Paseo agent id the session does not know (no `PASEO_AGENT_ID` in its env). The dispatcher must map by `persistence.sessionId == session_id` (from `paseo ls`/the agent record), or a small wrapper around `paseo import` must record the new id.

## (i) Codex shell-tool timeout

Sources: strings in the installed binary (`~/.codex/packages/standalone/releases/0.165.0-x86_64-unknown-linux-musl/bin/codex`; there is no JS package or local docs) plus one live test. This is not documented behaviour, so treat it as version-specific.

- In 0.165 the model's shell commands go through `exec_command` (unified exec, PTY) from the "code mode" `exec` tool. Tool description strings: `Wait before yielding output. Defaults to 10000 ms; effective range is 250-30000 ms.` and `yield_time_ms controls how long to wait for more output before yielding again. Defaults to 10000 ms.`
- A command that outlives the yield is not killed; the call returns a `session_id` and the model polls with `write_stdin`. A run-to-completion tool also exists with `timeout_ms` (`Maximum command runtime. Defaults to 10000 ms.`; the process is terminated on timeout).
- Config key `background_terminal_max_timeout` appears with the default string `background_terminal_max_timeout = 300000` (ms, 5 min). Also `tool_output_token_limit`. MCP tools have their own `tool_timeout_sec` (this machine's `config.toml` sets 300 for one server).
- Live test (mvmcc02, Paseo codex agent): asked it to run `sleep 40; echo SLEPT`. The first tool result came back after about 1 s: `{"chunk_id":"016b55","wall_time_seconds":1.000571757,"session_id":41756,"original_token_count":0,"output":""}`, then the model polled and got `SLEPT`. The turn took 48 s. A long command is not cut off at 10 s, but the model chooses the yield, must poll, and each poll costs a model round trip.
- Claude, for comparison (the Bash tool contract in this environment): default 120000 ms, max 600000 ms.

Implication: `msg await` cannot assume it holds a Codex tool call open for its whole wait. Design it in slices: a default `--timeout` well under 30 s per call that exits 0 quickly with a "still waiting, call again" status. The L2/L3 paths are the long-wait mechanism. If `msg await` may run longer in the background, the skill text must tell Codex to poll rather than assume failure. A 300 s ceiling is the plausible upper bound.

## Side effects and cleanup

- All agents created were deleted; `paseo ls -a` is empty on both machines at the end.
- `paseo run` and `paseo import` **create a new workspace on every call** even for the same cwd (`Created workspace wks_...`): five duplicate "paseo-spike" workspaces and several "main"/"agent-notes" ones appeared. All workspaces created by this run were archived with `paseo workspace archive`. A dispatcher that ever creates agents should pass `--workspace <id>`; the plan says it never runs `paseo run`.
- `/tmp/paseo-spike` and temp files on mvmcc02, and the scratch dir on Windows, were removed. Harness transcripts of the spike agents remain in `~/.claude/projects/-tmp-paseo-spike`, `~/.claude/projects/-projects-agent-notes`, and `~/.codex/sessions/2026/10/{28,29}/` (Windows: `~/.codex/sessions/2026/10/28/`).
- Hook side effect: the agents run in `/projects/agent-notes` triggered the normal `orient --reconcile` / `codex-hook` session registration, same as any session. The repo's git status was unchanged (`.identifiers-denylist.local.bak-20260808` was already untracked).

## Open items for the plan

1. Windows project identity (blocks any Windows hook value, including 6a's requester side): decide how `orient` and hooks resolve `C:\projects\agent-notes` to the shared project.
2. Hook latency budget: about 0.7 s Linux and 0.9 s Windows today; set the real budget and decide on a lighter entrypoint.
3. Mapping `session_key` to `paseo_agent_id`: self-register from env; for imported threads, map by `persistence.sessionId`.
4. agy: confirm `ANTIGRAVITY_CONVERSATION_ID` outside Paseo, add a throwaway hook to test CLI and desktop firing, and update the section 6 agy hook description to the named-hook `hooks.json` format.
5. Choose the `msg await` slice length for Codex.
6. Not done in this pass: (g) Hermes, (j) 023 runner conformance, and a direct capture of hook stdin `session_id` versus env.
