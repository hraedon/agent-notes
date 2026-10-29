# agent-notes-dispatch

The per-VM dispatcher from Plan 025 (§6 "Dispatcher", §6a, Phase 5). One
instance runs on each machine that hosts Paseo agents or runs review jobs. It
does two things:

- **L3 idle wake.** A direct `wake=true` message for a session on this VM is
  handed to the local Paseo daemon with
  `paseo send <agent> --no-wait --behavior steer --prompt-file <file>`, framed
  as peer content (D6). It only does this when all of the following hold:
  - The message is still unclaimed 1 s after it arrives. The grace period lets
    `msg await` or a Stop hook take it first.
  - The shared wake rule (`messaging.may_wake`, also used by the Stop hooks)
    allows it: the sender isn't an `untrusted-input` session, and the
    session's `wake_policy` is `direct`, or `any-direct-from-project` with a
    sender on the same project.
  - `auto_continues` is below the cap.
  - `paseo inspect` reports the agent `idle`.

  It sends at most one message per session per sweep or notification. The
  woken turn's per-prompt hook usually delivers the rest; otherwise they are
  rechecked at the next sweep. If the agent is running, the message is left for
  L1 and L2, and the dispatcher rechecks it every sweep for up to 10 minutes.

  `inspect` and `send` are two calls, so a human can start a turn between
  them. That race can't be closed from outside Paseo. `--behavior steer` is
  always passed, so losing it means the framed message steers the running
  turn; it never interrupts it (without the flag, Paseo 0.13 uses the
  agent's default and otherwise interrupts).
- **Jobs.** It claims `request` rows addressed to this VM and runs them, at
  most two at a time. v1 has one job type, `review`: one Claude review through
  Plan 023's runner. Every request gets a direct `wake=true` reply, whether it
  succeeds or fails.

It never runs `paseo run`, never passes `--host`, and never logs the
environment, which on a Paseo machine contains `PASEO_PASSWORD`.

## Why a separate process

Plan 025 D1 described the dispatcher as a mode of `agent-notes-bridge`. It's a
separate console script instead, because the bridge and the dispatcher share
almost nothing:

- The bridge forwards `agent_notes_changes` to agent-wake's HTTP ingest with an
  HMAC secret, and it's retired along with agent-wake.
- The dispatcher listens on a different channel (`agent_notes_messages`). It
  holds a per-VM lock, drives the local Paseo CLI, and runs review
  subprocesses.

Keeping them apart means the bridge can be deleted without touching the
dispatcher.

## How it works

- It uses one dedicated Postgres connection (not pooled), with
  `LISTEN agent_notes_messages`. That connection also holds
  `pg_try_advisory_lock(hashtextextended('agent-notes-dispatch:<vm>', 0))`. A
  second instance for the same VM exits with code 4.
- If the connection is lost, it reconnects with exponential backoff (1 s up to
  60 s).
- Every 30 s it sweeps for anything a lost notification would have missed:
  unclaimed direct `wake=true` rows from the last 10 minutes for sessions on
  this VM, plus queued requests.
- It finds the Paseo agent in this order:
  1. `agent_sessions.paseo_agent_id`, recorded from `PASEO_AGENT_ID` when the
     session registers.
  2. Paseo's local agent records, `<PASEO_HOME>/agents/*/*.json`, matched on
     `persistence.sessionId` (for agy, on the plugin `conversationId`). This
     covers threads adopted with `paseo import`. The result is stored with
     `messaging.set_paseo_agent_id`, which also turns on the `direct` wake
     policy unless a policy was set explicitly.
- If a send fails for any reason (including the prompt file), the claim is
  released, so L1 still delivers the message.
- A request gets at most one reply. The reply is posted in the transaction
  that locks the request row and checks for an existing reply, so a retry, a
  recovery and a second dispatcher can't answer twice.
- If a finished job's reply can't be posted (three tries), the reply is kept
  in memory and retried every sweep.
- At startup and on every sweep, it answers each request it claimed that has
  no reply and no job running here with `JOB_INTERRUPTED`. That covers a
  crash, a kill or a reboot during a job.
- On SIGTERM it waits up to 25 s for running jobs before closing the
  connection that holds the VM lock (the systemd unit allows 30 s).

## The review job

`agent-notes review request --reviewer claude --base main --head HEAD
[--work-item ID | --brief TEXT] [--on VM]`, run from a project checkout, does
the following:

1. Resolves the head to a SHA, and the base to the merge base of the base and
   the head.
2. Refuses if the head isn't on any remote-tracking branch, unless
   `--allow-unpushed` is passed.
3. Inserts the `request` row addressed to `--on`, `AGENT_NOTES_REVIEW_VM`, or
   the current VM, in that order.
4. Prints the request ID.

`--wait SECONDS` blocks like `msg await --reply-to`.

On the reviewing VM, the job then runs these steps:

1. Finds this VM's checkout through `project_checkouts` (WI-2.6).
2. Runs `git fetch --all`. Fetches are serialized per checkout.
3. Checks that both SHAs exist.
4. Calls 023's `evaluate_and_record` with `ClaudePrintRunner` in a detached
   worktree. The request is a brief-mode `ReviewRequest`, keyed in the attempt
   journal as `brief:agent-message-<id>` (WI-5.3).

The job stops at `result_validated`. It makes no lifecycle transition. The
reply carries:

- in the body: the verdict, the summary, the blocking findings (about 4 KB
  at most) and `agent-notes review show <id>`;
- in the payload: `attempt_id`, `artifact_digest` and the verdict.

`review show <id>` prints the request, its status (`queued`, `claimed`,
`replied` or `expired`), the reply, the attempt row and the full artifact.
`review list [--mine]` lists recent requests.

The reviewing checkout may have local changes. The job reviews fetched SHAs in
a detached worktree, so it skips 023's clean-checkout check
(`allow_dirty_checkout`).

Error codes in `payload.error_code`:

| Code | Meaning |
|---|---|
| `REVIEW_PLATFORM_UNSUPPORTED` | The dispatcher is on Windows. 023's runner needs `os.killpg`. Address the request to a Linux VM |
| `REVIEW_NO_CHECKOUT` | No checkout of the project is registered on this VM |
| `REVIEW_REVISION_MISSING` | The base or head isn't in the checkout after the fetch, usually because it wasn't pushed |
| `REVIEW_RUNNER_TIMEOUT` | The reviewer exceeded its timeout (600 s by default) |
| `REVIEW_RUNNER_FAILED`, `REVIEW_OUTPUT_INVALID`, `REVIEW_IDENTITY_MISMATCH`, `REVIEW_GIT_TARGET_INVALID`, `REVIEW_TARGET_INTEGRITY_FAILED` | 023's own failure codes |
| `BAD_REQUEST` | The payload isn't a JSON object, or has no string `job` (migration 853 keeps a pre-853 non-object payload under `invalid_payload`) |
| `REVIEW_REQUEST_INVALID`, `REVIEW_REVIEWER_UNSUPPORTED` | The request payload is malformed, or names a reviewer other than `claude` |
| `REVIEW_ATTEMPT_CONFLICT`, `REVIEW_JOB_FAILED`, `JOB_FAILED`, `JOB_TYPE_UNSUPPORTED`, `JOB_INTERRUPTED` | Journal conflict, an unexpected error, an unknown job type, or a dispatcher restart |

## Configuration

Each value is read from the process environment, then from `suite.env`. On
Linux, put them in `~/.config/agent-notes/dispatch.env`. The DSN comes from
the usual `~/.config/agent-notes/config.json` or `AGENT_NOTES_DSN`.

| Variable | Default | Meaning |
|---|---|---|
| `AGENT_NOTES_VM` | short hostname | This VM's ID. It must match the VM part of the session keys registered here |
| `AGENT_NOTES_PASEO_BIN` | `paseo` on `PATH` (`paseo.cmd` on Windows) | The Paseo CLI. Without it, L3 is off and a warning is logged |
| `PASEO_HOME` | `~/.paseo` | Where Paseo's agent records are read from |
| `AGENT_NOTES_MAX_AUTO_CONTINUES` | `3` | The loop-guard cap shared with the Stop hook (§6) |
| `AGENT_NOTES_DISPATCH_MAX_JOBS` | `2` | Concurrent jobs |
| `AGENT_NOTES_DISPATCH_SWEEP_SECONDS` | `30` | Sweep interval |
| `AGENT_NOTES_DISPATCH_LOG` | (stderr) | Log file instead of stderr, rotated at 5 MB × 3 |
| `AGENT_NOTES_REVIEW_MODEL` | `claude-opus-5-5` | The reviewer model when the request doesn't name one |
| `AGENT_NOTES_REVIEW_TIMEOUT` | `600` | The reviewer timeout in seconds, clamped to 30–1800 |
| `AGENT_NOTES_REVIEW_CLAUDE_BIN` | `claude` | The Claude Code executable |
| `AGENT_NOTES_REVIEW_DIFF_BUDGET` | `49152` | Byte budget for the unified diff in the reviewer prompt (the reviewer has no Git tool). Whole per-file diffs go in, in path order; files that don't fit are listed for the reviewer to Read. Clamped to 0–1 MiB; an invalid value means the default |
| `AGENT_NOTES_REVIEW_CREDENTIAL_ENV` | (none) | Comma-separated names of the variables passed to the reviewer, for example `CLAUDE_CODE_OAUTH_TOKEN`. The reviewer runs with a minimal environment and an isolated `HOME` (023 WI-054/056), so without a credential it can't authenticate |
| `AGENT_NOTES_REVIEW_ACCEPT_MODELS` | (none) | Comma-separated reported-model aliases to accept for the requested model (023's `accepted_reported_models`) |
| `AGENT_NOTES_REVIEW_VM` | the current VM | Requester side: where `review request` sends jobs |

## Operations

**mvmcc02 (Linux, systemd user unit)**

```bash
cp deploy/agent-notes-dispatch.service ~/.config/systemd/user/
$EDITOR ~/.config/agent-notes/dispatch.env   # AGENT_NOTES_VM=mvmcc02, AGENT_NOTES_PASEO_BIN=..., credential names
systemctl --user daemon-reload
systemctl --user enable --now agent-notes-dispatch
journalctl --user -u agent-notes-dispatch -f
```

`loginctl enable-linger itadmin` keeps the service running without a login.
Make sure the project's checkout is registered for the VM (`agent-notes
checkout scan /projects --apply`, or any `orient` in the checkout), and that
`git fetch` works non-interactively from the service. `GIT_TERMINAL_PROMPT=0`
is set, so a fetch that needs a password fails rather than hangs.

**Windows (logon task)**

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\agent-notes-dispatch.ps1 -Register
Start-ScheduledTask -TaskName "agent-notes dispatcher"
Get-Content $env:LOCALAPPDATA\agent-notes\dispatch.log -Wait
```

The task runs hidden (`conhost --headless`), and the script restarts the
dispatcher with backoff. On Windows it only does L3 wakes. Review requests sent
to it get `REVIEW_PLATFORM_UNSUPPORTED`.

**One-shot check.** `agent-notes-dispatch --once` sweeps, runs everything due
(wakes and jobs), and exits. It takes the VM lock, so stop the service first.
`--no-wake` and `--no-jobs` turn off either half.

**What the log shows.** For each settled wake, the log has one line with the
message IDs, the outcome (`sent`, `refused`, `not_hosted`, `skipped` or
`failed`), the Paseo agent ID and the reason. A wake deferred for 10 minutes
gets a "leaving it for L1" line. For each job, it logs the claim
and the finish line with the status. It never logs message bodies, prompts or
environment values.
