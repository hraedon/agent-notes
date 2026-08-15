# Session-scoped identity (WI-067)

## Why session-scoped

Work-item attribution and the cross-lineage review gate key off the model
lineage of the agent that filed the item. A **host-wide** lineage — one value
for every session on the host — is wrong on any host that runs more than one
model: it is false for most sessions, and worse, a wrong lineage passes a
same-lineage review as cross-lineage (fail-open). The regista epoch contract
therefore requires **identity that is session-scoped and resolvable**, and
treats a host-wide lineage as legitimate only where the host runs exactly one
model.

agent-notes implements this with a per-session record keyed by the harness
session id. Only the agent knows which model it is, so it declares once per
session; the record persists across tool calls (shell state does not).

## Precedence

Lineage resolution follows (WI-067, revised per cross-lineage review):

```
session record (once declared)  >  explicit --model-lineage
  >  process env (AGENT_NOTES_MODEL_LINEAGE)
  >  per-user suite.env  >  system suite.env  >  default
```

The session record is the **stable source once declared**. A conflicting
explicit `--model-lineage` is refused (`SESSION_LINEAGE_CONFLICT` /
`SessionIdentityConflict`), never silently honored: honoring it would let one
session author as one lineage and then "review" as another — manufacturing
false cross-lineage independence. A genuinely distinct reviewer must be a
distinct session (its own record / env), not a flag on the authoring session.
Before a session declares, explicit/env/suite still apply (unattended callers,
pre-declaration).

An undeclared session fails closed: writes attribute as UNKNOWN, and
`agent-notes invariants probe` reports the identity as unresolvable.

## Session record

- Location: `~/.local/state/agent-notes/sessions/<session-id>.env`
  (Linux) / `%LOCALAPPDATA%/agent-notes/sessions/` (Windows); override with
  `AGENT_NOTES_SESSION_DIR`.
- Keyed by the harness session id, resolved in this order:
  `CLAUDE_CODE_SESSION_ID` (Claude Code) → `OPENCODE_SESSION_ID` (opencode
  plugin) → `CODEX_SESSION_ID` (codex hook adapter). If only the generic
  `AGENT_NOTES_SESSION` fallback is present (a per-process UUID from the
  outbox), resolution still works but emits a warning — a record keyed by it
  will not survive to the next tool call.
- Written atomically (temp file + `os.replace`) with private permissions
  (file 0600, directory 0700; a pre-existing loose directory is tightened to
  0700 on write). A partial write is never visible.
- Garbage-collected after 30 days (`gc_session_records`).

## CLI surface

- `agent-notes session declare --model-lineage <family>` — write the record
  for the current session. Refuses non-canonical families (regista's closed
  vocabulary), refuses when no harness session id resolves, and **refuses
  changing an already-declared lineage** (`SESSION_LINEAGE_CONFLICT`).
  Re-declaring the same value is idempotent. A filesystem failure writing the
  record surfaces as `SESSION_RECORD_WRITE_FAILED` (never a traceback).
- `agent-notes session status` — read back what currently resolves.
- `agent-notes invariants probe --json` — measure
  `agent_notes.session_identity_resolvable`. **Fail-closed**: the check is
  `fail` (exit 1) when no lineage resolves, or when the declared value is not
  a canonical family. This is the agent-notes contribution to
  `agent-suite invariant-probes` / `genesis-gate`.

Both `session declare` and `session status` accept `--session-id <id>` — the
safe explicit mechanism for harnesses that cannot export a session id to tool
subprocesses (opencode). The flag is per-invocation (no process-global state)
and names the session whose record is written/read; the same stable-source
rule applies (a declared session's lineage cannot change mid-session).

The `/start` skill declares the session identity as its first step. The
opencode plugin threads `OPENCODE_SESSION_ID` into every spawned
`agent-notes` process **per-spawn** — it never mutates the shared
`process.env`, so concurrent sessions in one server process cannot leak their
id into each other's tool subprocesses (those tool calls fail closed with no
session id; use `--session-id` explicitly there). The codex hook adapter
forwards the hook payload's `session_id` as `CODEX_SESSION_ID`, and the
payload value is authoritative within the hook process (overwriting stale env
from an earlier hook run; a payload with no `session_id` clears the stale
value).

## Actor integration

`load_actor_config()` resolves `model_lineage` through the chain above, so the
regista claim path (which propagates `actor_metadata`) carries the
session-scoped lineage. `actor_with_overrides()` refuses an explicit lineage
that contradicts the session's declared record, closing the mid-session
independence vector in both the authoring and review-gate paths.
`actor_id` and the principal fields follow the env > suite.env layering;
`actor_id` is stable per host and belongs in suite.env.
