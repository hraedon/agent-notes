# Plan 025 — Coordination after AOS: memory tiers, a Postgres mailbox, and wake through Paseo

**Status:** Proposed 2026-10-28. Alternative to Plan 024. Same goals, different
split of responsibilities. The owner's answers (§10) and Argon's review are
folded in, the same day.
**Owner:** `agent-notes` (memory, mailbox, dispatcher). Wake transport is
adopted from Paseo (operator-console trial), not built.
**Retires:** `agent-wake` (frozen now, service disabled after Phase 5),
`regista` write-through (024 WI-1, corrected below).
**Harnesses:** Claude Code, Codex, OpenCode 2, Antigravity (`agy`, desktop and
CLI). Hermes Agent is a stretch goal.

---

## 1. Goals

From the owner's brief, with AOS being retired:

1. **Shared memory** at three levels: project, VM, global.
2. **Agent-to-agent communication**, including across machines.
3. **Wake / next turn** for harnesses that can't be woken natively.

The owner doesn't mind how these are achieved and prefers borrowing existing
machinery over building it.

**Headline scenario.** A Codex session asks for a Claude review of its branch.
It keeps working or ends its turn, and it's woken with the verdict when the
review finishes. Every decision below is checked against this flow, and §6a
walks through it end to end.

## 2. What already exists

| Piece | Where | What it gives us | Limits |
|---|---|---|---|
| `agent-notes` | Postgres `agent_notes` on `mvmpostgres01`, shared by every machine | Memories (project-scoped, plus a seeded `global` project), `orient`, SessionStart wiring for Claude (per-repo) / Codex / OpenCode 2, `agent-notes-bridge` already LISTENs on Postgres | No VM scope, no messaging, frozen since 2026-10-04 |
| `agent-wake` | `agent-waked` on mvmcc02 only (systemd user, unix socket, SQLite queue) | Claude channel adapter (needs `--dangerously-load-development-channels`), OpenCode `promptAsync` adapter, dead-letter queue | Codex unsupported (its Plan 006 never landed), no agy, not deployed on Windows, coupled to the AOS suite contracts |
| **Paseo 0.13.2** | Daemons on mvmcc02, Mac Studio, Windows (operator-console Phase 0) | Hosts `claude`, `codex` (app-server), `opencode`, and `antigravity` (drives the `agy` CLI) sessions. `paseo send <id>` delivers a prompt to an existing agent (idle → new turn; busy → steer or interrupt). `paseo wait`, `paseo heartbeat` (recurring self-prompts), `paseo schedule`, `paseo import` (adopt an existing provider session). Every hosted agent gets `PASEO_AGENT_ID` in its environment | Only reaches sessions Paseo hosts. Cross-machine control needs a pairing link, which is a full-control secret |
| Harness hooks | Each harness | Session-start and per-prompt context injection on all five. A Stop hook that can extend a turn that's already ending on Claude, Codex, and agy. Each harness sets its own session ID in the tool environment, except agy | Can't wake an idle session. See §6 for the per-harness matrix |

**The key observation.** Wake is the hard part, and Paseo already solves it
for all four target harnesses, as long as the session runs under Paseo. That
makes `agent-wake` redundant. It also means `agent-notes` doesn't need its own
idle-push path. It needs a mailbox, and a small local dispatcher that hands
mailbox rows to the local Paseo daemon.

## 3. Decisions

### D1. Three concerns, three owners

| Concern | Owner | Transport |
|---|---|---|
| Shared memory | `agent-notes` | Postgres |
| Messages (durable, cross-machine) | `agent-notes` | Postgres (`agent_messages` + `LISTEN/NOTIFY`) |
| Wake of an idle session | Paseo, via a local dispatcher | `paseo send` against the **local** daemon only |
| Bounded jobs (v1: review) | `agent-notes` dispatcher, reusing Plan 023's review runner | A `request` row in Postgres; the job runs as a local headless subprocess (D8) |

Nothing new listens on a network port, and no new daemon is introduced. The
dispatcher is a mode of the existing `agent-notes-bridge`.

### D2. Postgres is the only cross-machine path for agent messages

Agents never get Paseo pairing links or `--host` access to other machines. A
pairing link grants full control of the remote daemon: spawning agents, raw
terminal input, and permission-mode changes. The operator-console design
requires a step-up passkey for exactly those. So an agent sends a message to a
row in Postgres. The dispatcher on the target machine decides whether to wake
a local session, and it only ever talks to its own `127.0.0.1` daemon.

This also answers the operator-console backlog item "agents need a sanctioned
channel to invoke one another before SSH key auth is retired". The channel is
the mailbox. Every call is attributable through its sender columns, and the
wake policy (D6) is enforced on the receiving machine.

### D3. Memory scope is a property of the project row

Add `projects.kind TEXT NOT NULL DEFAULT 'repo' CHECK (kind IN ('repo','vm','global'))`.
VM scope is a project row with `slug = 'vm:<vm_id>'` and `kind = 'vm'`. The
seeded `global` row gets `kind = 'global'`.

- `memories` is unchanged. `memories_name_active_unique (project_id, name)`
  already gives per-scope name uniqueness. Links, `change_log`, search, and
  the memory engine keep working as they do.
- Plan 024 duplicated `scope_kind`/`scope_key` onto every memory row. That
  copy goes stale if a slug is renamed, and it buys nothing a join doesn't.
- One `WHERE p.kind = 'repo'` keeps the synthetic rows out of the project
  list, the web UI, work-item project resolution, cross-project `ready`, and
  export. Every one of those call sites must be audited (WI-2.4).

### D4. Two kinds of address: a session, or a project/VM inbox

- **Direct** (`to_session`): exactly one recipient. This is the only kind
  that may **wake** a session (Stop continuation or Paseo send).
- **Inbox** (`to_project` and/or `to_vm`): claimed by the first session that
  picks it up, at orient or at its next prompt. **It never wakes or extends
  a session.** A message for "whoever works on agent-notes next" should not
  hijack whichever agent-notes session happens to finish a turn first.
- **Notice** (`kind = 'notice'`, inbox-addressed): not claimed. Every
  matching session sees it in orient until it expires. For broadcasts like
  "main is frozen until 18:00".

`msg reply` always addresses the original sender's session directly. If the
sender had no session (a human at a shell), the reply goes to the sender's
project inbox and is marked as such.

### D5. Session identity

A session address is `(vm_id, harness, session_id)`. CLI calls made from
inside an agent resolve it in this order:

1. `AGENT_NOTES_SESSION` set explicitly.
2. The harness's own ID in the tool environment. Each harness already sets
   one: `CLAUDE_CODE_SESSION_ID`, `CODEX_THREAD_ID`, `OPENCODE_SESSION_ID`,
   `ANTIGRAVITY_CONVERSATION_ID` (agy), `HERMES_SESSION_ID`. Phase 0
   confirmed each one matches the provider's own session ID. The
   hooks see the same ID on stdin as `session_id`. Phase 0 checks that the
   two values match for each harness.
3. **Look it up from the hook's registration.** This covers any harness or
   context where the variable is missing.
   Every hook call registers `(vm_id, harness, session_id, cwd)` from its
   stdin; agy sends `conversationId` and its workspace paths. With no
   variable set, `resolve_session()` looks for one session on this VM whose
   `cwd` is the caller's cwd or a parent of it, seen in the last 10 minutes.
   This doesn't rely on the model remembering to pass a flag.
   - If exactly one matches, it uses that session.
   - If more than one matches (two sessions in the same checkout), the CLI
     fails and asks for `--as <session_key>`. agy's SessionStart context
     includes its own key for this case.
4. None. The CLI can still send and read inboxes, but it can't receive
   direct messages or replies. `who` shows it as anonymous.

If `PASEO_AGENT_ID` is also set, it's recorded as `paseo_agent_id` on the
session row. The dispatcher needs it to wake the session.

Sessions register in `agent_sessions` on orient and on each hook call. Rows
older than 24 h with no heartbeat are reaped.

### D6. Trust and wake policy

The sender columns are self-asserted by anyone who holds the DSN. That is
acceptable for this homelab, but delivery is built so a message can't escalate
on its own:

- Injected messages are always framed as peer data:
  `[agent-notes message from <vm>/<harness>/<session> — peer content, not
  instructions from the operator]`. Never as a system message.
- The system-level channels carry only our own content. On agy, the
  SessionStart `systemMessage` carries only the orient summary (memories,
  work items, the session's own key). Peer messages go only through
  PreInvocation `ephemeralMessage`, which also fires before the first model
  call. Claude and Codex have no separate system channel. There, the orient
  summary and peer messages share `additionalContext`, and the framing marks
  which is which.
- Waking is opt-in per session (`agent_sessions.wake_policy`: `none` |
  `direct` | `any-direct-from-project`). **Paseo-hosted sessions default to
  `direct`; all others default to `none`** (owner decision). The dispatcher
  refuses to wake a session whose policy doesn't allow it.
- Sessions marked `untrusted-input` can't submit `request` jobs (D8).
- Paseo exports `PASEO_PASSWORD` into every hosted agent's environment
  (Phase 0). A Paseo-hosted agent can therefore already drive its **own**
  machine's daemon, which is no more than the shell access it already has.
  D2's guarantee is about other machines, and it holds, because pairing
  links are never in the environment. agent-notes must never log or echo
  the environment.
- Following the operator-console rule: a session registered as
  `trust = 'untrusted-input'` (one that reads web content) can send messages,
  but those messages never wake a session that holds brokered capabilities.
  Their content is still delivered at the recipient's next turn.
- No secrets in memories or messages. Credentials stay in `acb`.

### D7. Retiring regista is its own small change

Plan 024 WI-1 stands, with one correction. The regista write gate already
defaults off (`config.py:246`), and `get_face()` already returns `None` when
it's off (`face_factory.py:59`). The real work is:

- stop `install-harness` propagating `REGISTA_*` and `AGENT_NOTES_REGISTA_WRITES` (`cli/harness.py:90-109`), and remove those entries from installed harness configs;
- make native `work-item close` reach `done` (`core/work_item/_native.py:304`);
- delete the orient outbox section (`cli/orient.py:160-168`) and the Codex Stop reconcile (`codex_lifecycle.py:212`). Both already do nothing when the gate is off, so this is cleanup, not a behaviour change;
- lift the freeze banner, and say in README why coordination lives here and not in the successor control plane.

This ships first and doesn't wait on anything else.

### D8. A review is a bounded job, not a conversation between agents

The headline scenario doesn't need a live Claude session to talk to. It needs
one bounded Claude run whose result comes back to the requester. Plan 023 has
already built and reviewed most of that, privately:

- a disposable detached worktree, so the reviewer can't touch the requester's checkout;
- `ClaudePrintRunner`: `claude -p` in safe mode, with only `Read`/`Grep`/`Glob` and `--json-schema` structured output;
- a bounded subprocess that kills its whole process group on timeout;
- the attempt journal and artifact store (`evaluate_and_record`, which stops at `result_validated` and never makes a lifecycle transition).

So:

- A **`request`** message is addressed to a VM inbox and carries a `payload`
  naming an allowlisted job type. v1 has one type: `review`. Only the
  dispatcher on that VM claims requests, and they're never injected into a
  session.
- The dispatcher runs the job through 023's orchestrator. When it finishes,
  succeeds or fails, the dispatcher posts a direct `reply` with `wake=true`
  to the requester. From there, delivery is ordinary mailbox delivery (§6).
- 023's runner is **Linux/macOS only** (it uses `os.killpg`). Review jobs
  run on mvmcc02 by default. A Windows requester just addresses that VM. The
  head revision must be pushed; the CLI checks this, and the dispatcher
  fetches before resolving revisions.
- 023's `ReviewRequest` currently requires a work item. WI-5.3 makes the
  work-item fields optional, with a `--brief` instead, so a plain branch
  review doesn't need a work item filed first.
- Starting open-ended agents in response to messages is still a non-goal
  (§8). A job runs one allowlisted, bounded subprocess and exits.

## 4. Memory tiers (Phase 2)

- **WI-2.1 Schema** (`schema/850_projects_kind_and_messaging.sql`):
  `projects.kind`, backfill `global` → `'global'` and any `vm:%` → `'vm'`.
  Add a partial index `(workspace_id, kind)`.
- **WI-2.2 VM identity.** `resolve_vm_id()`: `--vm` flag → `AGENT_NOTES_VM`
  (env or `suite.env`) → short lowercase hostname. `install-harness` writes
  `AGENT_NOTES_VM` explicitly on each machine. A hostname that looks generated
  (containers, k8s pods) is refused with an error asking for `AGENT_NOTES_VM`,
  so VM memories don't scatter across random names. `doctor` prints the
  resolved VM ID.
- **WI-2.3 CLI.** `memory add|get|update|delete --scope repo|vm|global [--vm ID]`
  (default `repo`). `memory list|search|recall --scope ...|composed` (default
  `composed`). `--scope vm|global` works outside a registered repo.
- **Composed ranking.** Search all three scopes with one query and rank by
  similarity with no tier boost. When the same `name` exists in more than one
  scope, return the most specific (repo > vm > global), so a project can
  override a global convention. The shadowed entries stay visible in
  `shadows: [{"scope": "vm", "scope_key": "mvmcc02", "body_preview": "<first ~120 chars>"}]`.
  Generic names like `setup` or `troubleshooting` often collide between
  unrelated topics, so this keeps the agent from losing them silently.
  Every result carries `scope` and `scope_key`.
- **WI-2.4 Synthetic-row audit.** Every query that lists projects filters on
  `kind = 'repo'`. Covers the web UI, `workspace` CLI, work-item project
  resolution, cross-project `ready`, export, and doctor.
- **WI-2.5 orient.** Additive JSON only. `memories` stays the project list,
  because the Codex hook (`codex_lifecycle.py:144`) and the OpenCode plugin
  (`integrations/opencode/index.js:120`) read it as one. Add `memories_vm`,
  `memories_global`, and `inbox`. Default caps: 3 global, 3 VM, 5 project
  (the current `DEFAULT_LIMIT`). Outside a registered repo, orient succeeds
  with VM, global, and inbox sections.
- **WI-2.6 Per-VM checkouts.** Phase 0 found that Windows can't resolve any
  project: `projects.repo_root` holds one path, and it's the Linux one. Add
  `project_checkouts(project_id, vm_id, path)`.
  - `init` records a checkout for the current VM, and running `init` on an
    existing slug adds a checkout instead of stealing `repo_root`.
  - `resolve_project(path)` matches the current VM's checkouts first, using
    the longest prefix (case- and separator-insensitive on Windows), and
    falls back to `repo_root` only after that.
  - `agent-notes checkout scan <dir> [--apply]` maps subdirectories whose git
    toplevel basename matches an existing slug. It's a dry run by default.
  - The review dispatcher uses the same table to find its checkout.
- **Tests** (real Postgres): the same name in all three scopes, shadowing,
  orient inside and outside a repo, and that `vm:*`/`global` rows don't
  appear in project listings.

## 5. Mailbox and presence (Phase 3, no hooks)

This phase alone covers most of goal 2. Any agent in any harness can shell out
to it.

```sql
CREATE TABLE agent_sessions (
    session_key   TEXT PRIMARY KEY,          -- '<vm>/<harness>/<session_id>'
    workspace_id  INTEGER NOT NULL REFERENCES workspaces(id),
    vm_id         TEXT NOT NULL,
    harness       TEXT NOT NULL,             -- claude|codex|opencode|agy|hermes|cli
    session_id    TEXT NOT NULL,
    paseo_agent_id TEXT,                     -- set when hosted by the local Paseo daemon
    project_id    INTEGER REFERENCES projects(id),
    cwd           TEXT,
    wake_policy   TEXT NOT NULL DEFAULT 'none'
                  CHECK (wake_policy IN ('none','direct','any-direct-from-project')),
    trust         TEXT NOT NULL DEFAULT 'normal'
                  CHECK (trust IN ('normal','untrusted-input')),
    auto_continues INTEGER NOT NULL DEFAULT 0, -- reset on each human prompt (§6)
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE agent_messages (
    id            BIGSERIAL PRIMARY KEY,
    workspace_id  INTEGER NOT NULL REFERENCES workspaces(id),
    from_session  TEXT,                      -- session_key, or NULL for a human/CLI
    from_vm       TEXT NOT NULL,
    from_project  TEXT,
    to_session    TEXT,                      -- direct
    to_project    TEXT,                      -- inbox
    to_vm         TEXT,                      -- inbox (AND with to_project when both set)
    kind          TEXT NOT NULL DEFAULT 'message'
                  CHECK (kind IN ('message','question','reply','handoff','notice','request')),
    reply_to      BIGINT REFERENCES agent_messages(id),
    subject       TEXT,
    body          TEXT NOT NULL CHECK (length(body) <= 32768),
    payload       JSONB NOT NULL DEFAULT '{}',     -- job spec for 'request'; result refs for its reply
    wake          BOOLEAN NOT NULL DEFAULT false,  -- honoured only for direct messages
    claimed_by    TEXT,
    claimed_at    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL DEFAULT now() + interval '7 days',
    CHECK (num_nonnulls(to_session, to_project, to_vm) >= 1),
    CHECK (to_session IS NULL OR (to_project IS NULL AND to_vm IS NULL)),
    CHECK (kind <> 'notice' OR to_session IS NULL),
    CHECK (kind <> 'request' OR (to_vm IS NOT NULL AND to_session IS NULL))
);
```

An `AFTER INSERT` trigger sends `pg_notify('agent_notes_messages', '{"id":…,"to_session":…,"to_project":…,"to_vm":…,"wake":…}')`
(IDs and addresses only, well under the 8 KB payload limit).

Semantics, kept deliberately small:

- **Claim** is the only state change: `claimed_by`/`claimed_at` set with
  `UPDATE … WHERE id = ANY(SELECT … FOR UPDATE SKIP LOCKED)`. Plan 024's
  `delivered`/`acked` split is dropped. Nothing used `acked`, and redelivery
  after a crash isn't worth the complexity for chat-sized messages. Notices
  are never claimed.
- **Expiry** is a query-time filter (`expires_at > now()`), plus a prune
  folded into the existing `doctor`/maintenance path. There's no `expired`
  status that nothing would ever set.
- **Matching.** A session reads direct rows for its `session_key`, plus inbox
  rows where every non-null `to_*` matches its project and VM. Sessions
  never match `request` rows; only the dispatcher claims those (D8).

CLI:

- `agent-notes who [--project P] [--vm V] [--active-within 30m] [--json]`
- `agent-notes msg send (--to SESSION_KEY | --to-project P [--to-vm V] | --to-vm V) --body … [--kind …] [--subject …] [--wake] [--json]`
- `agent-notes msg reply ID --body … [--wake]`: always direct to the sender.
- `agent-notes msg inbox [--claim] [--all] [--json]`
- `agent-notes msg await [--reply-to ID] [--from SESSION_KEY] [--timeout 120] [--json]`.
  This runs `LISTEN` **before** its first `SELECT`, so nothing can arrive
  in between, then claims and prints the first match. `--timeout` defaults
  below the harness tool limit (Claude's Bash tool caps at 10 minutes). On
  timeout it exits with code 2 so the agent can poll again.

**Phase exit:** an agent on the Windows workstation sends a `question` to a
session on mvmcc02, and receives the reply through `msg await`, with no hooks
or Paseo involved.

## 6. Delivery (Phases 4–5)

Four delivery levels, from weakest to strongest. Each harness gets as many as
it supports.

| Level | Mechanism | Applies to |
|---|---|---|
| **L0 Synchronous** | `msg await` inside a tool call | Every harness, already there after Phase 3 |
| **L1 Next turn** | SessionStart/orient plus a per-prompt hook inject claimed messages | Direct and inbox messages |
| **L2 Turn extension** | The Stop hook claims a **direct, `wake=true`** message and extends the turn that's ending | Direct only; behind `AGENT_NOTES_STOP_WAKE=1` |
| **L3 Idle wake** | The dispatcher runs `paseo send --no-wait <paseo_agent_id>` on the target VM | Direct, `wake=true`, Paseo-hosted, and allowed by `wake_policy` |

Per-harness surface, from official docs or source as of 2026-10-28 (research
notes in §11). Items marked † are not documented anywhere and get checked in
Phase 0 before anything is built on them.

| Harness | L1 session start | L1 per prompt | L2 turn extension | L3 idle wake |
|---|---|---|---|---|
| **Claude Code** | SessionStart `hookSpecificOutput.additionalContext` (10k-character cap) | UserPromptSubmit `additionalContext` | Stop `{"decision":"block","reason":…}`. Input has `stop_hook_active`. Claude also has its own cap of 8 consecutive continuations | Paseo `send`. Not Paseo-hosted: Claude's own cross-session messaging (`CLAUDE_CODE_MESSAGING_SOCKET`) wakes idle sessions, but only for local senders, and its wire format for outside processes is undocumented † |
| **Codex** | SessionStart `additionalContext` (wired today) | UserPromptSubmit `additionalContext` | Stop `{"decision":"block","reason":…}`; the reason becomes the continuation prompt. Input has `stop_hook_active`. Our handler currently exits early on it (`codex_lifecycle.py:286`), which must change | Paseo `send` (Paseo drives Codex through app-server `turn/start`). A plain TUI session can't be reached from outside † |
| **OpenCode 2** (v2.2.0 plugin API) | `context` hook (wired today) | `ctx.session.hook("prompt")` | No Stop equivalent in v2.2.0. `experimental.idle` exists only on the dev branch | Paseo `send`, **or in-process**: the plugin itself can `LISTEN` and call `ctx.session.prompt` on its own idle session (what agent-wake's adapter does today) |
| **agy** CLI / desktop | `~/.gemini/config/hooks.json` SessionStart → `injectSteps` `systemMessage`, **orient summary only** (D6) | PreInvocation → `injectSteps` `ephemeralMessage`, used for all peer messages, including on the first turn | Stop `{"decision":"continue","reason":…}`. No `stop_hook_active`; the guard uses the `executionNum` input. The client caps consecutive continuations (ceiling unknown) | **CLI only**, via Paseo (it drives `agy`; no steering). Desktop: whether hooks fire is unconfirmed †, so desktop may get L0 only |
| **Hermes** (stretch) | `pre_llm_call` with `is_first_turn` → `{"context":…}` | `pre_llm_call` | `pre_verify` only fires on turns that edited code (capped at 3 by default), so it isn't usable as a general L2 | Hermes API server (`POST /api/sessions/{id}/chat`) wakes API-owned sessions only. It returns 409 for CLI- or TUI-owned sessions. No Paseo provider |

Notes:
- Prefer the structured `hookSpecificOutput.additionalContext` form on
  Claude and Codex. Hermes and agy have their own shapes above. A shared
  formatter in `agent_notes/hook_common.py` frames the message once (D6) and
  each adapter wraps it.
- Hooks are best effort. Each one opens a connection with a 1 s timeout,
  queries, and exits 0 on any error. A Postgres outage must never block a
  prompt. Phase 0 measured Python import plus a query at about 0.7 s on
  mvmcc02 and about 0.9 s on Windows for a full `orient`. So the per-prompt
  hook gets its own minimal entry point: it imports only psycopg and
  `core/messaging`, does one indexed query, and targets < 400 ms on Windows.
  Hooks always write UTF-8, because the Windows console defaults to cp1252.

L2 loop guard: `auto_continues` in `agent_sessions` goes up by one on each L2
extension or L3 wake, and resets to 0 when the per-prompt hook sees a real
human prompt. At the cap (default 3) the Stop hook and the dispatcher stop
waking, and the message waits for L1. Two agents can't ping-pong
indefinitely. The cap is ours, and it applies on top of the harnesses' own
caps (Claude's 8, agy's unknown one), because those reset whenever a tool is
called. Two agents trading messages through tool calls would never hit them.

The per-prompt hook can't tell a human prompt from a wake. Claude fires
UserPromptSubmit on turns it starts on its own, and a Paseo `send` looks like
a user prompt. So the reset only applies when the prompt doesn't begin with
the D6 framing marker.

**Dispatcher** (`agent-notes-bridge --dispatch`, one per machine, next to the
Paseo daemon). It does `LISTEN agent_notes_messages` and, for each direct
`wake=true` row addressed to a session on this VM:

1. Waits 1 s, so an `await` or Stop hook can claim it first.
2. Re-reads the row. If it's still unclaimed and the session has
   `paseo_agent_id`, `wake_policy` allows it, the trust rule passes (D6), and
   `auto_continues < cap`: claim it, then run
   `paseo send --no-wait <id> --prompt-file <framed message>`.
3. If the session isn't Paseo-hosted, it leaves the message for L1 and logs
   once. The exception is OpenCode 2, where the agent-notes plugin claims
   and prompts its own idle session in-process, so no dispatcher is needed.

The dispatcher also claims `request` rows addressed to its VM and runs them
as jobs (D8, §6a). It runs at most two jobs at once, and the rest wait their
turn.

Before treating a session as gone, the dispatcher can check `paseo ls --json`.
It runs under systemd on Linux, launchd on the Mac, and as a logon task on
Windows (the same pattern as the Paseo daemon). It never runs `paseo run`.
Jobs are bounded subprocesses, not Paseo agents.

**Paseo for scheduled self-wake.** `paseo heartbeat create` already gives
Paseo-hosted agents "wake me in 20 minutes". We document it in the skill
rather than rebuilding it.

**agent-wake disposition.** Frozen now. The mvmcc02 service keeps running for
the Claude-channel and OpenCode sessions that depend on it until Phase 5 exit,
then gets disabled. Its one idea worth keeping (at-least-once delivery with a
dead-letter queue) isn't needed at this scale. External webhooks (CI, alerts)
can come back later as a thin ingest that calls `msg send`.

## 6a. Headline flow: Codex asks Claude for a review and is woken by the result

```text
Windows                                   Postgres                    mvmcc02
codex session W/codex/T                                               dispatcher
  │ agent-notes review request ──► request row (to_vm=mvmcc02) ──LISTEN──►│ claim
  │   --reviewer claude                                               git fetch
  │   --base main --head HEAD                                         023 orchestrator:
  │ (keeps working, or ends turn)                                     worktree + claude -p
  │                                                                   artifact stored
  │◄── L0/L1/L2/L3 ◄── reply row (to_session=W/codex/T, wake) ◄──────┘
```

1. **Request.** Codex runs
   `agent-notes review request --reviewer claude --base main --head HEAD [--work-item ID | --brief TEXT] [--on mvmcc02]`.
   - The CLI resolves both revisions to SHAs, refuses if the head isn't
     pushed, and inserts the `request` row with the requester's
     `session_key` (resolved per D5).
   - It prints the request ID and a one-line instruction for the agent:
     "keep working or end your turn; the verdict will arrive as a message".
   - `--on` defaults to the configured review VM (`AGENT_NOTES_REVIEW_VM`, set
     to `mvmcc02` by install-harness).
2. **Run.** The mvmcc02 dispatcher claims the row and fetches. It maps the
   repository identity to its local checkout through `project_checkouts`
   (WI-2.6). Then it calls 023's `evaluate_and_record` with `ClaudePrintRunner`. The default
   timeout is 600 s, and the attempt journal records every state.
3. **Reply.** The dispatcher inserts a direct `reply` (`reply_to` = the
   request, `wake=true`). Its body is the verdict, the blocking findings
   (bounded), and `agent-notes review show <request-id>` for the full
   artifact. Failures (timeout, unpushed head, runner error) come back the
   same way, so the requester never waits on silence.
4. **Wake.** What the requester sees depends on its state:

   | Codex session state | Delivery |
   |---|---|
   | Paseo-hosted and idle | **L3**: the Windows dispatcher runs `paseo send`, and Codex starts a turn with the verdict. Fully unattended, so this is the configuration the scenario is designed for |
   | Still in a turn and chose to wait | **L0**: `msg await --reply-to <id>` returns it |
   | Turn ending with no `await` | **L2**: the Stop hook claims it and extends the turn |
   | Started by hand and idle | **L1**: it arrives with your next prompt. To make such a session wakeable, `paseo import` it (Phase 0 h) |

The reverse direction (Claude asks Codex for a review) needs a Codex runner in
023 (`codex exec` with an output schema †). That's WI-6.1. Mailbox delivery
is identical for every reviewer/requester pair.

## 7. Phases

| Phase | Content | Exit |
|---|---|---|
| **0 Spikes** (≈1 day) | (a) `paseo send` to an **idle** agent starts a turn, for each provider, on mvmcc02 and Windows. (b) `paseo inspect --json` exposes the provider session ID and cwd, so the dispatcher can map `session_key` ↔ `paseo_agent_id`. (c) `PASEO_AGENT_ID` is visible to Bash tool calls under each provider. (d) Harness hooks still fire inside Paseo-hosted sessions. (e) Every † item in §6 and D5: whether agy desktop fires hooks, agy hooks on Windows, and that hook `session_id` equals the env-var ID (Codex: thread versus session). (f) Per-prompt hook latency on Windows and mvmcc02 (Python startup + Postgres round trip); set a budget and a 1 s `connect_timeout`, and make hooks fail open. (g) Hermes surface. (h) `paseo import` of a Codex desktop/TUI thread makes it wakeable, and whether the original client shows the resulting turn. (i) Codex's shell-tool timeout, which bounds `msg await`. (j) 023's Claude runner conformance tests still pass on mvmcc02 with regista off | Matrix filled in; go/no-go on Paseo as L3. **Done 2026-10-28** (`docs/spikes/025-phase0.md`): `send` wakes idle claude/codex/agy/opencode agents; `inspect` lacks the provider session ID, so sessions self-report `PASEO_AGENT_ID`, and the dispatcher maps imported threads from `~/.paseo/agents/*/*.json`; hooks fire under Paseo; Windows needs WI-2.6; Codex's shell tool yields after 10 s and polls for up to 300 s |
| **1** | Retire regista write-through (D7) | `file → claim → close` reaches `done` with no `REGISTA_*` |
| **2** | Memory tiers (§4) | Tests in §4 pass; orient shows three tiers on both machines |
| **3** | Mailbox, `who`, `await` (§5) | Cross-machine question/reply through `await` |
| **4** | L1 hooks for all four harnesses; L2 behind a flag | Message sent mid-session appears on the recipient's next prompt in each harness |
| **5** | Dispatcher: L3 wake (WI-5.1), the `review` job (WI-5.2), optional work-item fields in 023's `ReviewRequest` (WI-5.3), `review request` and `review show` CLI plus the skill text (WI-5.5); retire `agent-wake` | **The headline scenario (§6a), unattended:** a Paseo-hosted Codex session on Windows requests a Claude review, goes idle, and is woken with the verdict. agent-wake service disabled |
| **6** | Codex reviewer runner (WI-6.1), so Claude can ask Codex | The reverse of the headline scenario |
| **7** (stretch) | Hermes | L0 + L1 at minimum |

Phases 1 and 2 are independent and can run in parallel. 4 depends on 3. 5
depends on 1 (023 shouldn't route anything through regista), on 3, and on
Phase 0 (a)–(c), (h), and (j). L2 from Phase 4 isn't needed for the headline
scenario, so 4 and 5 can overlap.

## 8. Non-goals

- Starting open-ended agents in response to messages. v1 runs only
  allowlisted, bounded jobs (D8). Launch templates via `paseo run` + `acb`
  belong to operator-console Phase 1.
- Transcript custody and signed provenance. Review jobs record 023's
  artifact and attempt journal but make no gate transitions in v1. The
  requester decides what to do with the verdict.
- Authenticating senders beyond the Postgres role. If that's ever needed, use
  a per-machine DB role and stamp `from_vm` from `current_user` in the
  trigger.
- Cross-workspace messaging.

## 9. Changes from Plan 024

- Wake is adopted from Paseo, not built. agent-wake is retired, not extended.
- Postgres is the only cross-machine transport, and agents never hold pairing links.
- Scope lives on `projects.kind`, not in two columns copied onto every memory row.
- Inbox messages never wake or extend a session; only direct messages do.
  Replies are always direct. Broadcasts get an explicit `notice` kind.
- Session identity is specified (D5). Peer messages are framed as peer data,
  and waking is opt-in per session (D6).
- orient changes are additive, with smaller default caps.
- The status machine shrinks to "claimed or not", and expiry is a query filter.
- Hook latency has a measured budget and fail-open behaviour, instead of an assumed <50 ms.
- Every harness claim is verified in Phase 0 before anything is built on it.
- Cross-agent review is a bounded job that reuses Plan 023's runner (D8),
  not a new conversation protocol.

## 10. Owner decisions (2026-10-28)

1. **Rely on Paseo for L3, subject to Phase 0.** Argon agrees: L0–L2 are all
   Postgres and work without Paseo, so Phases 1–4 deliver value even if
   Paseo misbehaves for one harness. OpenCode can also wake itself in-plugin.
   If Paseo fails Phase 0 for Codex, herdr is the fallback for L3.
2. **Hand-started sessions get L0–L2.** L3 is for Paseo-hosted sessions. The
   owner's requirement is that "Codex calls for a Claude review and is
   awakened when it finishes" works. §6a meets it by running the requesting
   Codex session under Paseo, or by `paseo import`-ing it. Otherwise the
   verdict arrives with the next prompt.
3. **`wake_policy` defaults to `direct` for Paseo-hosted sessions** and to
   `none` for all others.

Argon's refinements, adopted:
- agy peer messages go only through PreInvocation `ephemeralMessage`; SessionStart carries only the orient summary (D6).
- Session lookup by `(vm, cwd)` from hook registrations, so agy doesn't depend on the model passing `--as` (D5).
- `shadows` entries carry scope and a body preview (§4).

## 11. Existing tools considered

Researched 2026-10-28, from official docs and source where available.

| Tool | Verdict |
|---|---|
| **Paseo** (Apache-2.0, already deployed) | **Adopted for L3.** Hosts all four target harnesses. `send` works on running or idle agents. Its MCP tools only reach the same host, which matches D2. It doesn't host Hermes |
| **MCP Agent Mail** (Rust rewrite) | Not adopted. It's a good mailbox, but it's SQLite + Git rather than our Postgres, pull-only with no wake, and its licence adds an "OpenAI/Anthropic rider" that needs reading before use. Its thread/inbox model informed §5 |
| **agent-peers** | Not adopted (new, Linux/macOS only, single machine, no agy). Its delivery paths are worth borrowing in Phase 4: Claude native messaging, Codex app-server `turn/start`, OpenCode `ctx.session.synthetic` |
| **herdr** | Fallback only. It types into a terminal pane, so it works with any TUI including agy and Hermes. But "delivered" only means text was written, and its state detection is screen-scraped |
| **ACC** (agents-can-communicate) | Not adopted (local only, unclear licence terms). Its issues #171 and #177 are the best public captures of agy's hook payloads; use them as Phase 0 fixtures |
| **Claude cross-session messaging** | Native and wakes idle sessions, but it's Claude-only, and cross-machine messages go through Anthropic and Remote Control. Usable as a Claude-only L3 for sessions not hosted by Paseo, if Phase 0 can post to the socket |
| A2A adapters, claude-peers-mcp | Not adopted. A2A adds an HTTP surface we don't need with a shared Postgres. claude-peers is superseded by Claude's native messaging |
