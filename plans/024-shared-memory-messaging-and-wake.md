# Plan 024 — 3-Tier Shared Memory, Inter-Agent Messaging, and Turn-Boundary Wake

**Status:** Proposed 2026-10-28 (awaiting team review)
**Owner:** `agent-notes` (with optional idle-push bridge to `agent-wake`)
**Supersedes / Simplifies:** Plan 009/012/018 (`regista` write-through authority), Plan 022 (multi-principal Regista memory authority)
**Target Harnesses:** Claude Code (`claude`), Codex (`codex`), OpenCode 2 (`opencode`), Antigravity Desktop & CLI (`agy`), and Hermes (`hermes`)

---

## 1. Context & Problem

With `agent-operations-suite` (AOS) and the `regista` compliance/signing regime parked, `agent-notes` no longer needs to dual-write signed envelopes to `regista` or hold work-item completion hostage to off-box `regista` review gates. At the same time, day-to-day multi-agent workflows across workstation (`c:\projects`) and server VMs (`mvmcc02`) have three concrete operational needs:

1. **Shared Memory at Three Explicit Scopes (`project`, `vm`, `global`)**:
   - Currently, `memories` are scoped only to `(workspace_id, project_id)`. Cross-project notes rely on an implicit convention (`--project global`), there is no **VM-level scope** (for host-specific facts like installed toolchains, service ports, local paths, GPU/driver quirks, or SSH topology on `mvmcc02` vs. the workstation), and `agent-notes orient` only surfaces project memories.
2. **Inter-Agent Communication & Presence Across Harnesses and VMs**:
   - Agents running concurrently across harnesses (`claude`, `codex`, `opencode`, `agy`, `hermes`) or across machines (`workstation` ↔ `mvmcc02`) have no supported way to see which peer sessions are active (`who`), send a direct or project-targeted message (`msg send`), or wait synchronously mid-turn for a peer's reply (`msg await`).
3. **Wake / Next-Turn Continuation for Harnesses That Lack Native Push**:
   - `agent-wake` (`agent-waked`) provides an external daemon for OpenCode 2 (`session.promptAsync`) and Claude Code (`--channels`), plus a file-backed `Stop`-hook queue on Linux (`mvmcc02`), but:
     - `agent-waked` is not deployed everywhere (e.g., Windows workstation running `agy` or `codex`);
     - `agy` (Antigravity Desktop & CLI) is not yet a supported harness in `agent-notes install-harness`;
     - Because **all machines connect to the same shared Postgres instance and database (`AGENT_NOTES_DSN`)**, `agent-notes` itself can provide cross-VM message delivery, `LISTEN/NOTIFY` synchronous waiting (`msg await`), and lifecycle-hook turn continuation (`Stop` / `PreInvocation` / `SessionStart`) directly over Postgres—without requiring `agent-waked` unless the operator wants unprompted idle-terminal wake.

---

## 2. Architectural Decisions

### D1. Native Postgres Is the Sole Authority (`regista` Write-Through Retired)

1. **`face_factory.get_face()` defaults to `None`**:
   - Native Postgres tables (`op_log`, `work_items`, `memories`, `links`, `change_log`) are the sole source of truth.
   - `AGENT_NOTES_REGISTA_WRITES` defaults to `false` and `install-harness` stops propagating `REGISTA_DSN`, `REGISTA_KEY_PATH`, `REGISTA_REQUIRE_SSL`, and `AGENT_NOTES_REGISTA_WRITES` into harness configurations.
2. **Direct native `work-item close`**:
   - Under Plan 014 Option A(b), `WorkItemModel.close_work_item` on the native path deferred `close` to `in_review` so that completion could only happen via `regista`. With `regista` retired, `agent-notes work-item close <id>` transitions directly to `done` on the native op-log path (while preserving `work-item review` and `work-item update --status in_review` for explicit review workflows when desired).
3. **No mandatory outbox drain on `orient` / `Stop`**:
   - Session start (`orient`) and `Stop` hooks no longer run Regista outbox reconciliation by default, removing startup/teardown latency and split-brain warnings.

---

### D2. First-Class 3-Tier Shared Memory (`project` / `vm` / `global`)

We introduce three closed memory scopes in `memories`:

| Scope (`--scope`) | `scope_kind` | `scope_key` | Backing `projects.slug` | Intended Content |
| :--- | :--- | :--- | :--- | :--- |
| **`project`** *(default)* | `'project'` | `<project_slug>` (e.g. `agent-notes`) | `<project_slug>` | Repo architecture, design decisions, conventions, local gotchas |
| **`vm`** | `'vm'` | `<vm_id>` (e.g. `mvmcc02`, `workstation`) | `vm:<vm_id>` | Host-specific paths, installed services/ports, GPU/OS quirks, local mounts |
| **`global`** | `'global'` | `'global'` | `'global'` | Cross-host/cross-project conventions, user preferences, network/fleet topology |

#### Why Dual Columns (`scope_kind`, `scope_key`) + Backing Synthetic `projects` Row?
- Every `memories` row gets explicit `scope_kind TEXT NOT NULL DEFAULT 'project'` (`CHECK (scope_kind IN ('project', 'vm', 'global'))`) and `scope_key TEXT NOT NULL` columns, indexed by `(workspace_id, scope_kind, scope_key, active)`.
- At the same time, for `scope_kind = 'global'` and `scope_kind = 'vm'`, `agent-notes` resolves (via `INSERT ... ON CONFLICT (workspace_id, slug) DO NOTHING`) a synthetic row in `projects` (`slug = 'global'` or `slug = 'vm:<vm_id>'`, `repo_root = NULL`).
- **Benefits**:
  1. `memories.project_id` remains `NOT NULL REFERENCES projects(id)`.
  2. The existing partial unique index `memories_name_active_unique ON memories (project_id, name) WHERE active = true` enforces per-scope active name uniqueness across all three tiers with zero breakage.
  3. `links` (`[[wikilinks]]`), `change_log`, `all_notes_search_v`, and `NativeMemoryEngine` continue to work without nullable-FK special cases.
  4. Existing memories already stored under the seeded `global` project are automatically migrated in place to `scope_kind = 'global', scope_key = 'global'`.

#### VM Identity Resolution
`resolve_vm_id(explicit: str | None = None) -> str` resolves in order:
1. Explicit CLI flag (`--vm <name>`);
2. `AGENT_NOTES_VM` environment variable or `vm` key in `config.json` / `suite.env`;
3. ` platform.node().split(".")[0].lower()` (short lowercase hostname).

#### Composed Recall & `orient`
- **Write CLI**:
  - `agent-notes memory add <name> --type <type> --body <text> [--scope project|vm|global] [--vm <vm_id>]`
  - `agent-notes memory get|update|delete <name> [--scope project|vm|global] [--vm <vm_id>]`
- **Read / Search CLI**:
  - `agent-notes memory list [--scope project|vm|global|composed] [--vm <vm_id>]`
  - `agent-notes memory search <query> [--scope project|vm|global|composed]`
  - Default for `memory search` (and `memory recall`) is `composed`: searches across `('project', current_project)`, `('vm', current_vm)`, and `('global', 'global')` in one query, tagging every result with `scope_kind` and `scope_key`.
  - When a memory `name` or topic appears in multiple scopes, results surface `scope_kind` explicitly (`project` > `vm` > `global` specificity order).
- **Orientation (`agent-notes orient`)**:
  - `orient` returns three bounded memory sections in both human and `--json` output:
    - `memories.global`: active global memories (capped, e.g., top 10 by `updated_at` / importance);
    - `memories.vm`: active memories for the current host (`vm:<vm_id>`, capped at 10);
    - `memories.project`: active memories for the resolved project (existing behavior).
  - Even if the current working directory is **not** a registered project, `agent-notes orient` still succeeds in degraded/host mode by returning `memories.global`, `memories.vm`, and any unread messages for the session/VM.

---

### D3. Inter-Agent Presence (`who`) and Messaging (`msg`) Over Shared Postgres

Because all workstations and VMs share the same Postgres database, Postgres is the single broker for presence, durable message mailboxes, and real-time wake notifications (`LISTEN/NOTIFY`).

#### 1. Session Presence (`agent_sessions`)
Updated automatically whenever `agent-notes orient` or a harness lifecycle hook (`SessionStart`, `PreInvocation`, `Stop`) runs, or explicitly via `agent-notes who --register`:

```sql
CREATE TABLE IF NOT EXISTS agent_sessions (
    session_id      TEXT PRIMARY KEY,
    workspace_id    INTEGER NOT NULL REFERENCES workspaces(id),
    project_id      INTEGER REFERENCES projects(id),
    project_slug    TEXT,
    vm_id           TEXT NOT NULL,
    harness         TEXT NOT NULL,  -- 'claude' | 'codex' | 'opencode' | 'agy' | 'hermes' | 'cli'
    actor_id        TEXT,
    cwd             TEXT,
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'idle', 'stopped')),
    metadata        JSONB NOT NULL DEFAULT '{}',
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

- **CLI**: `agent-notes who [--project <slug>] [--vm <vm_id>] [--active-within 30m] [--json]`
  - Lists active/recent agent sessions across all VMs and harnesses so an agent (or human) can see who is working where and address them by `session_id`, `project`, or `vm`.

#### 2. Durable Mailbox + `NOTIFY` (`agent_messages`)
```sql
CREATE TABLE IF NOT EXISTS agent_messages (
    id                  BIGSERIAL PRIMARY KEY,
    message_uuid        UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    workspace_id        INTEGER NOT NULL REFERENCES workspaces(id),

    -- Sender provenance
    from_session_id     TEXT,
    from_harness        TEXT,
    from_vm_id          TEXT NOT NULL,
    from_project_slug   TEXT,
    from_actor          TEXT,

    -- Target routing (at least one target dimension required)
    to_session_id       TEXT,           -- direct session target (highest specificity)
    to_project_slug     TEXT,           -- any active session in project (or next session to orient)
    to_vm_id            TEXT,           -- restrict to a specific VM (or NULL for any VM)
    to_harness          TEXT,           -- restrict to a specific harness (or NULL for any)

    -- Content & threading
    reply_to_id         BIGINT REFERENCES agent_messages(id),
    kind                TEXT NOT NULL DEFAULT 'message'
                        CHECK (kind IN ('message', 'question', 'reply', 'handoff', 'signal')),
    subject             TEXT,
    body                TEXT NOT NULL CHECK (length(body) <= 32768),
    payload             JSONB NOT NULL DEFAULT '{}',

    -- Wake & delivery lifecycle
    wake                BOOLEAN NOT NULL DEFAULT TRUE,
    status              TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued', 'delivered', 'acked', 'expired')),
    delivered_to_session TEXT,
    delivered_at        TIMESTAMPTZ,
    acked_at            TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at          TIMESTAMPTZ NOT NULL DEFAULT (now() + INTERVAL '24 hours'),

    CHECK (
        to_session_id IS NOT NULL
        OR to_project_slug IS NOT NULL
        OR to_vm_id IS NOT NULL
    )
);
```

An `AFTER INSERT ON agent_messages` trigger calls:
```sql
PERFORM pg_notify(
    'agent_notes_messages',
    json_build_object(
        'id', NEW.id,
        'message_uuid', NEW.message_uuid,
        'to_session_id', NEW.to_session_id,
        'to_project_slug', NEW.to_project_slug,
        'to_vm_id', NEW.to_vm_id,
        'to_harness', NEW.to_harness,
        'from_session_id', NEW.from_session_id,
        'from_project_slug', NEW.from_project_slug,
        'from_vm_id', NEW.from_vm_id,
        'kind', NEW.kind,
        'wake', NEW.wake
    )::text
);
```

#### 3. Messaging CLI (`agent-notes msg`)
- `agent-notes msg send --to-project <slug> | --to-session <id> | --to-vm <vm> [--to-harness <h>] --body <text> [--subject <s>] [--kind message|question|handoff|signal] [--no-wake] [--json]`
- `agent-notes msg reply <message_id> --body <text> [--no-wake] [--json]`
- `agent-notes msg inbox [--all] [--claim] [--json]`
  - Lists `queued` (or `--all`) messages matching the caller's `(session_id, project_slug, vm_id, harness)`.
  - With `--claim`, atomically transitions matching `queued` rows to `delivered` (`FOR UPDATE SKIP LOCKED`) so two concurrent sessions in the same project never double-consume a project-targeted message.
- `agent-notes msg ack <message_id | --all> [--json]`
- `agent-notes msg await [--timeout 120] [--from-project <slug>] [--reply-to <message_id>] [--json]`
  - Uses `agent_notes.core.notify.listen(channel="agent_notes_messages")` (`LISTEN/NOTIFY` over the shared Postgres connection) while polling/checking `agent_messages` for a matching `queued` row, claims it atomically, and prints it to stdout.
  - **Why `msg await` is a game-changer**: Any agent in **any** harness (`agy`, `codex`, `claude`, `opencode`, `hermes`) can send a question or handoff to an agent on another VM (`mvmcc02`) and immediately run `agent-notes msg await --timeout 180`. When the remote agent replies, `msg await` returns immediately with the reply as tool output, continuing the waiting agent's turn with zero external daemon required.

---

### D4. Turn-Boundary Wake & First-Class Harness Support (`claude`, `codex`, `opencode`, `agy`, `hermes`)

Harnesses differ in how they can be woken or continued:

| Harness | Orientation (`SessionStart`) | Mid-Run Injection (`PreInvocation` / `UserPromptSubmit`) | Turn-End Continuation (`Stop` Hook) | Synchronous Wait (`msg await`) | Unprompted Idle Push (Optional `agent-waked`) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Antigravity (`agy`)** | `~/.gemini/config/hooks.json` `SessionStart` → `injectSteps.systemMessage` | `PreInvocation` → `injectSteps.ephemeralMessage` | `Stop` → `{"decision": "continue", "reason": "..."}` | Yes (`LISTEN/NOTIFY`) | N/A (handled via `Stop` + `PreInvocation` + `msg await`) |
| **Codex (`codex`)** | `~/.codex/hooks.json` `SessionStart` → `additionalContext` | `UserPromptSubmit` → `additionalContext` | `Stop` → `{"decision": "block", "reason": "..."}` | Yes (`LISTEN/NOTIFY`) | N/A (handled via `Stop` + `msg await`) |
| **Claude Code (`claude`)** | `~/.claude/settings.json` `SessionStart` → `additionalContext` | `UserPromptSubmit` → `additionalContext` | `Stop` → `{"decision": "block", "reason": "..."}` | Yes (`LISTEN/NOTIFY`) | Yes (`--channels` MCP via `agent-waked`) |
| **OpenCode 2 (`opencode`)** | `~/.config/opencode/plugins/agent-notes.ts` `session.created` | Plugin before-prompt hook | Plugin idle / `session.promptAsync` | Yes (`LISTEN/NOTIFY`) | Yes (`session.promptAsync` via `agent-waked` or plugin) |
| **Hermes (`hermes`)** | `~/.hermes/skills/agent-notes/SKILL.md` + startup hook | Skill / wrapper | Wrapper / `msg await` | Yes (`LISTEN/NOTIFY`) | Optional via `agent-wake` queue |

#### How Turn-Boundary Wake Works in `agent-notes` (Zero-Daemon Path)
1. **`SessionStart`**:
   - Runs `orient`, registers/heartbeats the session in `agent_sessions`, claims any `queued` messages targeted at this session/project, and injects them alongside the 3-tier memory summary.
2. **`PreInvocation` (`agy`) / `UserPromptSubmit` (`claude`, `codex`)**:
   - Fast (<50 ms) Postgres query checking if any `queued` message has arrived for this `session_id` or `project_slug` since the last check.
   - If found, atomically marks it `delivered` and injects a formatted `[agent-notes message from <sender>]: ...` block into the turn context (`ephemeralMessage` on `agy`, `additionalContext` on `claude`/`codex`).
3. **`Stop` (Next-Turn Continuation)**:
   - When the agent is about to stop (`terminationReason == "NO_TOOL_CALL"` on `agy`, or normal turn end on `codex`/`claude`), the hook checks `agent_messages` for any `queued` row with `wake = true` matching this session/project.
   - **Loop-safety guard**: Mirrors the battle-tested `stop_hook_active` guard—if a message is claimed, it is atomically transitioned `queued -> delivered` in Postgres (`FOR UPDATE SKIP LOCKED`) before returning `{"decision": "continue", "reason": "..."}` (`agy`) or `{"decision": "block", "reason": "..."}` (`claude` / `codex`). Because the message is now `delivered`, the subsequent turn will not re-trigger on the same message. A per-session consecutive-wake cap (default `3` consecutive auto-continues without a human turn) prevents runaway agent ping-pong loops.

#### Optional Idle-Terminal Wake via `agent-notes-bridge` → `agent-waked`
- For hosts running `agent-waked` (e.g., `mvmcc02` with OpenCode 2 or Claude Code `--channels`), `agent-notes-bridge` is extended to `LISTEN agent_notes_messages` (in addition to `agent_notes_changes`).
- When an `agent_messages` notification with `wake = true` arrives and remains `queued` after a brief grace window (e.g., 500 ms, so an active `msg await` or `Stop` hook claims it first), `agent-notes-bridge` forwards it to `agent-waked` to wake an idle OpenCode 2 or Claude Code session.

---

## 3. Work Items

### WI-1: Unfreeze `agent-notes` & Make Native Postgres the Default Authority
- **Files**:
  - `README.md`, `AGENTS.md`: Remove the `Frozen 2026-10-04` banner and document the post-AOS native architecture.
  - `src/agent_notes/core/config.py`, `src/agent_notes/core/face_factory.py`: Default `AGENT_NOTES_REGISTA_WRITES` off (`False`); keep `regista` modules import-isolated so normal operation never touches `regista`.
  - `src/agent_notes/core/work_item_model.py`, `src/agent_notes/core/work_item/_native.py`: Update `WorkItemModel.close_work_item` on the native path so `agent-notes work-item close <id>` transitions directly to `done` (writing a terminal `close` op) instead of deferring to `in_review` waiting for a Regista gate.
  - `src/agent_notes/cli/harness.py`, `src/agent_notes/codex_lifecycle.py`, `src/agent_notes/cli/orient.py`: Remove `REGISTA_*` env propagation from `install-harness` and disable automatic Regista outbox reconciliation in `orient` and `Stop` hooks.
- **Verification**:
  - Unit & integration tests verifying `work-item file -> claim -> close` reaches `done` natively without `--force` and without `REGISTA_*` configured.

### WI-2: 3-Tier Shared Memory (`project`, `vm`, `global`)
- **Files**:
  - `schema/850_shared_memory_messaging_and_wake.sql`:
    - Add `scope_kind TEXT NOT NULL DEFAULT 'project' CHECK (scope_kind IN ('project', 'vm', 'global'))` and `scope_key TEXT NOT NULL DEFAULT ''` to `memories`.
    - Backfill existing rows: rows where `project_id = (SELECT id FROM projects WHERE workspace_id = memories.workspace_id AND slug = 'global')` get `scope_kind = 'global', scope_key = 'global'`; rows with `projects.slug LIKE 'vm:%'` get `scope_kind = 'vm', scope_key = substr(slug, 4)`; all other rows get `scope_kind = 'project', scope_key = projects.slug`.
    - Add index `idx_memories_scope_active ON memories (workspace_id, scope_kind, scope_key, active, updated_at DESC) WHERE active = true`.
    - Update `all_notes_search_v` (or add `scope_kind`, `scope_key` columns at the tail of `all_notes_search_v` so existing column positions remain compatible).
  - `src/agent_notes/core/config.py`: Add `resolve_vm_id(explicit: str | None = None) -> str`.
  - `src/agent_notes/core/db.py`: Add helper `resolve_scope_project(workspace_id: int, scope_kind: str, scope_key: str) -> Project` that resolves or creates the synthetic `projects` row for `global` or `vm:<vm_id>`.
  - `src/agent_notes/core/memory_model.py`:
    - Update `add_memory`, `get_memory`, `list_memories`, `search_memories`, `search_memories_exact`, `update_memory`, and `delete_memory` to accept `scope_kind` and `scope_key` (defaulting to `'project'` and the resolved project slug) and support `composed` multi-scope queries (`project` + `vm` + `global`).
  - `src/agent_notes/cli/memory.py`:
    - Add `--scope {project,vm,global}` (and `composed` on `list`/`search`/`recall`) plus `--vm <vm_id>` to `memory add|get|list|search|recall|update|delete`.
    - Allow `--scope vm` and `--scope global` commands to run from **any** directory (even outside a registered project).
  - `src/agent_notes/cli/orient.py`:
    - Update `orient` to return `global`, `vm`, and `project` memory tiers, and succeed gracefully with `global` + `vm` memories when invoked outside a registered repository.
- **Verification**:
  - Real-Postgres integration tests covering `project`, `vm`, and `global` memory CRUD, active-name isolation across scopes (same memory `name` in `global`, `vm:mvmcc02`, and `project:foo`), composed search ranking, and `orient` output inside and outside a registered repo.

### WI-3: Inter-Agent Presence (`who`) and Messaging (`msg`) with `LISTEN/NOTIFY`
- **Files**:
  - `schema/850_shared_memory_messaging_and_wake.sql`:
    - Create `agent_sessions` and `agent_messages` tables + indexes + `agent_messages_notify_fn()` `AFTER INSERT` trigger emitting on channel `agent_notes_messages`.
  - `src/agent_notes/core/messaging_model.py`:
    - `register_or_heartbeat_session(...)`, `mark_session_stopped(...)`, `list_active_sessions(...)`
    - `send_message(...)`, `reply_message(...)`, `fetch_inbox(..., claim: bool = False)`, `ack_messages(...)`
    - `await_message(..., timeout_seconds: float = 120.0)` using `agent_notes.core.notify.listen(channel="agent_notes_messages")` with atomic `FOR UPDATE SKIP LOCKED` claim.
  - `src/agent_notes/cli/who.py` & `src/agent_notes/cli/msg.py`:
    - Register `agent-notes who` and `agent-notes msg {send,reply,inbox,ack,await}` in `src/agent_notes/cli/__init__.py`.
  - `src/agent_notes/cli/orient.py`:
    - Record session heartbeat in `agent_sessions` and include `unread_messages` in `orient` output.
- **Verification**:
  - Real-Postgres integration tests:
    - Session registration/heartbeat and `who` filtering by project/VM/staleness.
    - Direct (`--to-session`), project (`--to-project`), and VM (`--to-vm`) message routing.
    - Concurrent `inbox --claim` atomicity (`FOR UPDATE SKIP LOCKED` ensures exactly one consumer claims a project-wide message).
    - Live `msg await` test where a background thread/process inserts a message via `msg send` and `msg await` wakes via Postgres `NOTIFY` within <200 ms.

### WI-4: First-Class `agy` & `hermes` Harness Adapters + Unified Turn-Boundary Wake
- **Files**:
  - `src/agent_notes/agy_lifecycle.py`:
    - Implements `agent-notes agy-hook` handler reading Antigravity hook JSON from `stdin`:
      - `SessionStart`: runs `orient`, heartbeats `agent_sessions` (`harness='agy'`, `session_id=conversationId`), claims queued messages, and emits `{"injectSteps": [{"systemMessage": {"systemMessage": ...}}]}`.
      - `PreInvocation`: fast check for newly queued messages for this `conversationId` or project; if present, claims and emits `{"injectSteps": [{"ephemeralMessage": ...}]}`.
      - `Stop`: marks session idle; if a `queued` message with `wake=true` exists (and consecutive auto-continues < max cap), claims it and returns `{"decision": "continue", "reason": ...}` to immediately start the next turn.
  - `src/agent_notes/codex_lifecycle.py` & Claude hook handler:
    - Add `UserPromptSubmit` / `Stop` unread-message claim + `{"decision": "block", "reason": ...}` next-turn continuation (with the same consecutive-wake loop guard).
  - `src/agent_notes/cli/harness.py` & `src/agent_notes/cli/skills.py`:
    - Extend `--harness` choices to `claude`, `codex`, `opencode`, `agy`, `hermes`, `all`.
    - For `agy`: install `SKILL.md` to `~/.gemini/config/skills/agent-notes/SKILL.md` (user scope) or `.agents/skills/agent-notes/SKILL.md` (project scope) and non-destructively merge `SessionStart`, `PreInvocation`, and `Stop` entries into `~/.gemini/config/hooks.json` (or `.agents/hooks.json`).
    - For `hermes`: install `SKILL.md` to `~/.hermes/skills/agent-notes/SKILL.md` with hash tracking.
    - Update the packaged `SKILL.md` template to document `--scope project|vm|global`, `agent-notes who`, and `agent-notes msg send|inbox|reply|ack|await`.
- **Verification**:
  - Harness installer tests (`test_harness_install.py`) for `agy` and `hermes` (idempotency, hash tracking, non-clobbering merge of `hooks.json`, `--uninstall`).
  - Lifecycle hook tests (`test_agy_lifecycle.py`, `test_codex_lifecycle.py`) verifying `SessionStart`, `PreInvocation`, and `Stop` JSON contracts and loop-guard capping.

### WI-5 (Optional): `agent-notes-bridge` Message Wake Forwarding to `agent-waked`
- **Files**:
  - `src/agent_notes/bridge.py`:
    - Listen on both `agent_notes_changes` and `agent_notes_messages`.
    - When an `agent_notes_messages` notification has `wake=true`, wait a configurable grace delay (default `500ms` so an active `msg await` or `Stop` hook claims it first); if the message is still `queued` in Postgres, POST a `wake: true` event to `AGENT_NOTES_BRIDGE_TARGET` (`agent-waked`) with target project/session metadata so idle OpenCode 2 or Claude Code sessions wake automatically.
- **Verification**:
  - Bridge integration test verifying that an unclaimed `wake=true` message emits a signed `wake: true` POST to the mock ingest target, while an already-claimed message is skipped.

---

## 4. Non-Goals & Guardrails

1. **No Secrets in Memory or Messages**: `memories` and `agent_messages` are strictly for operational context and coordination; credentials and secret leases remain in `agent-capability-broker` (`acb`).
2. **No Automatic Process Spawning**: Neither `agent-notes` nor `agent-notes-bridge` spawns new CLI processes on incoming messages. Messages wake an already-running session (via `msg await`, `Stop` hook continuation, `PreInvocation` injection, or `agent-waked` idle push) or remain `queued` in Postgres until the target project/VM next runs `orient`.
3. **Bounded Auto-Continue**: `Stop`-hook continuation is strictly bounded (atomic `queued -> delivered` claim + max `3` consecutive auto-continues per human turn) so two agents cannot enter an infinite auto-reply loop.
