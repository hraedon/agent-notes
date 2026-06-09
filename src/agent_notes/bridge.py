"""NOTIFY → agent-wake HTTP bridge (Plan 004 §7, Phase 9c).

A separate, optional process: LISTEN on `agent_notes_changes`, buffer briefly,
POST each event to the agent-wake HTTP ingest endpoint with HMAC signing.

One DB connection (LISTEN-only), one HTTP client. The bridge does *not*
publish to regista (decision 56) and does *not* persist a high-water mark
(open Q3 — change_log row is the durable record; replay is deferred).

Wire format note: agent-wake's HTTP ingest accepts one event per POST
(`adapters/claude/.../ingest.py`). Plan 004 §7 mentions `{"events":[...]}`
batching but the v0 wake schema is single-event per request — the bridge
honors agent-wake's wire shape and emits one POST per buffered change.

Required environment:
    AGENT_NOTES_DSN              Postgres DSN (LISTEN connection).
    AGENT_NOTES_BRIDGE_TARGET    e.g. http://127.0.0.1:8788/
    AGENT_NOTES_BRIDGE_SECRET    HMAC-SHA256 shared secret.

Optional:
    AGENT_NOTES_BRIDGE_SOURCE    X-AgentWake-Source value (default "agent-notes").
    AGENT_NOTES_BRIDGE_BATCH_MS  Buffer window in ms (default 100).
    AGENT_NOTES_BRIDGE_BATCH_N   Max events per flush (default 50).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid

from agent_notes.core.http_transport import DEFAULT_SOURCE, post_with_retry

_log = logging.getLogger("agent_notes.bridge")

DEFAULT_BATCH_MS = 100
DEFAULT_BATCH_N = 50


def _payload_to_wake_event(payload: dict, source: str) -> dict:
    """Map a change_log NOTIFY payload to an agent-wake v0 event.

    NOTIFY payloads are JSON objects emitted by the change_log AFTER INSERT
    trigger. They carry at minimum {kind, event, identifier, workspace_id,
    project_id}. We normalize to the minimum shape agent-wake requires.
    """
    kind = payload.get("kind", "note")
    event = payload.get("event", "changed")
    identifier = payload.get("identifier", "?")
    project_id = payload.get("project_id")
    workspace_id = payload.get("workspace_id")

    content = f"{kind} {identifier} {event}"
    if project_id is not None:
        content += f" (project_id={project_id})"

    meta: dict[str, str] = {
        "agent_notes_kind": str(kind),
        "agent_notes_event": str(event),
        "agent_notes_identifier": str(identifier),
    }
    if project_id is not None:
        meta["agent_notes_project_id"] = str(project_id)
    if workspace_id is not None:
        meta["agent_notes_workspace_id"] = str(workspace_id)

    return {
        "v": 0,
        "event_id": str(uuid.uuid4()),
        "source": source,
        "kind": "note-change",
        "content": content,
        "meta": meta,
        "wake": False,
    }


def _flush(buffer: list[dict], target: str, secret: str, source: str) -> None:
    while buffer:
        event = buffer.pop(0)
        post_with_retry(target, secret, source, event)


def run(
    *,
    target: str | None = None,
    secret: str | None = None,
    source: str | None = None,
    dsn: str | None = None,
    batch_ms: int | None = None,
    batch_n: int | None = None,
    max_events: int | None = None,
) -> None:
    """Run the bridge loop. Blocks until SIGINT or max_events reached (tests)."""
    target = target or os.environ.get("AGENT_NOTES_BRIDGE_TARGET")
    secret = secret or os.environ.get("AGENT_NOTES_BRIDGE_SECRET")
    source = source or os.environ.get("AGENT_NOTES_BRIDGE_SOURCE", DEFAULT_SOURCE)
    batch_ms = (
        batch_ms
        if batch_ms is not None
        else int(os.environ.get("AGENT_NOTES_BRIDGE_BATCH_MS", DEFAULT_BATCH_MS))
    )
    batch_n = (
        batch_n
        if batch_n is not None
        else int(os.environ.get("AGENT_NOTES_BRIDGE_BATCH_N", DEFAULT_BATCH_N))
    )

    if not target or not secret:
        raise RuntimeError("AGENT_NOTES_BRIDGE_TARGET and AGENT_NOTES_BRIDGE_SECRET required")

    import psycopg

    from agent_notes.core.config import resolve_dsn

    effective_dsn = resolve_dsn(dsn)

    poll = max(batch_ms / 1000.0, 0.05)
    sent = 0
    buffer: list[dict] = []
    deadline: float | None = None

    conn = psycopg.connect(effective_dsn, autocommit=True)
    try:
        conn.execute("LISTEN agent_notes_changes")
        while True:
            for n in conn.notifies(timeout=poll):
                try:
                    payload = json.loads(n.payload)
                except (json.JSONDecodeError, TypeError):
                    payload = {"raw": n.payload}
                buffer.append(_payload_to_wake_event(payload, source))
                if deadline is None:
                    deadline = time.monotonic() + batch_ms / 1000.0
                if len(buffer) >= batch_n:
                    break
            if buffer and (
                len(buffer) >= batch_n or (deadline is not None and time.monotonic() >= deadline)
            ):
                count = len(buffer)
                _flush(buffer, target, secret, source)
                sent += count
                deadline = None
                if max_events is not None and sent >= max_events:
                    return
    finally:
        conn.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    try:
        run()
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # pragma: no cover - defensive
        _log.error("bridge crashed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
