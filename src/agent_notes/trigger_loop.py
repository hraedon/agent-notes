"""Cross-project trigger loop — async listener on `agent_notes_op_log_events` (Plan 008 P3).

A separate, optional process: LISTEN on `agent_notes_op_log_events`, buffer briefly,
inspect each event for cross-project routing, POST routed events to the target
project's wake channel via agent-wake HTTP ingest.

Routing rules:
- `request.created` → target project's wake channel
- `link.added` with cross_project=True → target project's wake channel
  (maps to `dependency.blocked` at the target)
- `item.closed` → query reverse edges (links + cross_project_links) and wake
  each blocked dependent project with `dependency.resolved`

Required environment:
    AGENT_NOTES_DSN              Postgres DSN (LISTEN connection).

Optional:
    AGENT_NOTES_BRIDGE_TARGET    Default agent-wake HTTP ingest endpoint
                                 (fallback when a project's wake_channel is NULL).
    AGENT_NOTES_BRIDGE_SECRET    HMAC-SHA256 shared secret for POST signing.
    AGENT_NOTES_BRIDGE_SOURCE    X-AgentWake-Source value (default "agent-notes").
    AGENT_NOTES_BRIDGE_BATCH_MS  Buffer window in ms (default 100).
    AGENT_NOTES_BRIDGE_BATCH_N   Max events per flush (default 50).

Design notes:
- The trigger loop is **best-effort** (Invariant W). A lost wake is recovered by
  the level-tail (`events --since`) on the next SessionStart.
- Idempotent delivery: events carry `op_id`; dedupe at the receiver.
- Coalesce/debounce: multiple events for the same target within the buffer window
  are batched into a single POST.
- The loop does NOT auto-spawn agents; it enqueues + wakes-if-listening.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.request
import uuid
from typing import Iterable

import psycopg
from psycopg.rows import dict_row

_log = logging.getLogger("agent_notes.trigger_loop")

DEFAULT_SOURCE = "agent-notes"
DEFAULT_BATCH_MS = 100
DEFAULT_BATCH_N = 50
RETRY_DELAYS = (0.1, 1.0, 10.0)


def _sign(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _post_with_retry(
    target: str,
    secret: str,
    source: str,
    event: dict,
    *,
    delays: Iterable[float] = RETRY_DELAYS,
    timeout: float = 5.0,
) -> bool:
    """POST one event with exponential backoff. Returns True on 2xx."""
    body = json.dumps(event).encode("utf-8")
    sig = _sign(body, secret)

    attempts = [0.0] + list(delays)
    last_error: str | None = None
    for delay in attempts:
        if delay:
            time.sleep(delay)
        req = urllib.request.Request(
            target,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-AgentWake-Source": source,
                "X-AgentWake-Signature": sig,
                "X-AgentWake-Event-Id": event["event_id"],
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if 200 <= resp.status < 300:
                    return True
                last_error = f"HTTP {resp.status}"
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = f"transport: {exc}"
    _log.warning("dropping event %s after retries: %s", event.get("event_id"), last_error)
    return False


def _build_wake_event(
    event_type: str,
    payload: dict,
    source: str,
    meta: dict | None = None,
) -> dict:
    """Build an agent-wake v0 event from a trigger-loop routing decision."""
    effective_meta: dict[str, str] = dict(meta or {})
    effective_meta.setdefault("agent_notes_event_type", event_type)
    return {
        "v": 0,
        "event_id": str(uuid.uuid4()),
        "source": source,
        "kind": "trigger-loop",
        "content": f"{event_type}: {json.dumps(payload, default=str, sort_keys=True)}",
        "meta": effective_meta,
        "wake": True,
    }


def _resolve_project_wake_channel(
    conn: psycopg.Connection,
    project_id: int | None = None,
    project_slug: str | None = None,
) -> str | None:
    """Return the wake_channel for a project, or None if not configured."""
    cur = conn.cursor(row_factory=dict_row)
    if project_id is not None:
        cur.execute("SELECT wake_channel FROM projects WHERE id = %s", (project_id,))
    elif project_slug is not None:
        cur.execute("SELECT wake_channel FROM projects WHERE slug = %s", (project_slug,))
    else:
        return None
    row = cur.fetchone()
    return row["wake_channel"] if row else None


def _route_request_created(
    conn: psycopg.Connection,
    payload: dict,
    default_target: str | None,
    secret: str,
    source: str,
    buffer: list[dict],
) -> None:
    """Route a `request.created` event to the target project's wake channel."""
    target_project_slug = payload.get("target_project")
    if not target_project_slug:
        _log.debug("request.created without target_project; skipping")
        return

    wake_channel = _resolve_project_wake_channel(conn, project_slug=target_project_slug)
    target = wake_channel or default_target
    if not target:
        _log.debug("no wake channel for target_project=%s", target_project_slug)
        return

    event = _build_wake_event(
        "request.created",
        payload,
        source,
        meta={"target_project": target_project_slug},
    )
    buffer.append({"target": target, "secret": secret, "event": event})


def _route_link_added(
    conn: psycopg.Connection,
    payload: dict,
    default_target: str | None,
    secret: str,
    source: str,
    buffer: list[dict],
) -> None:
    """Route a `link.added` cross-project event to the target project's wake channel."""
    cross_project = payload.get("cross_project")
    if not cross_project:
        # Same-project link — not a cross-project trigger concern.
        return

    to_project_id = payload.get("to_project_id")
    to_project_slug = payload.get("to_project_slug")
    if not to_project_id and not to_project_slug:
        _log.debug("link.added without to_project; skipping")
        return

    wake_channel = _resolve_project_wake_channel(conn, project_id=to_project_id)
    target = wake_channel or default_target
    if not target:
        _log.debug("no wake channel for to_project_id=%s", to_project_id)
        return

    event = _build_wake_event(
        "dependency.blocked",
        payload,
        source,
        meta={
            "to_project_id": str(to_project_id) if to_project_id else "",
            "to_project_slug": str(to_project_slug) if to_project_slug else "",
        },
    )
    buffer.append({"target": target, "secret": secret, "event": event})


def _route_item_closed(
    conn: psycopg.Connection,
    payload: dict,
    default_target: str | None,
    secret: str,
    source: str,
    buffer: list[dict],
) -> None:
    """Route `item.closed` to dependent projects via reverse-edge lookup."""
    entity_id = payload.get("entity_id")
    identifier = payload.get("identifier")
    if not entity_id or not identifier:
        return

    # Find the project for this entity.
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        "SELECT project_id FROM work_items WHERE entity_id = %s",
        (entity_id,),
    )
    row = cur.fetchone()
    if row is None:
        return
    project_id = row["project_id"]

    cur.execute("SELECT slug FROM projects WHERE id = %s", (project_id,))
    proj_row = cur.fetchone()
    project_slug = proj_row["slug"] if proj_row else None

    # Find same-project dependents (links table).
    cur.execute(
        """
        SELECT l.from_project, l.from_identifier, p.slug AS from_project_slug
        FROM links l
        JOIN projects p ON p.id = l.from_project
        WHERE l.to_project = %s
          AND l.to_identifier = %s
          AND l.relationship = 'blocks'
          AND l.to_kind = 'work_item'
          AND l.from_kind = 'work_item'
        """,
        (project_id, identifier),
    )
    for r in cur.fetchall():
        from_project_id = r["from_project"]
        from_identifier = r["from_identifier"]
        from_project_slug = r["from_project_slug"]
        wake_channel = _resolve_project_wake_channel(conn, project_id=from_project_id)
        target = wake_channel or default_target
        if not target:
            continue
        event = _build_wake_event(
            "dependency.resolved",
            {
                "blocker_project": project_slug,
                "blocker_identifier": identifier,
                "unblocked_project": from_project_slug,
                "unblocked_identifier": from_identifier,
            },
            source,
            meta={
                "unblocked_project": from_project_slug,
                "unblocked_identifier": from_identifier,
            },
        )
        buffer.append({"target": target, "secret": secret, "event": event})

    # Find cross-project dependents (cross_project_links table).
    if project_slug:
        cur.execute(
            """
            SELECT cpl.from_project_id, cpl.from_identifier, p.slug AS from_project_slug
            FROM cross_project_links cpl
            JOIN projects p ON p.id = cpl.from_project_id
            WHERE cpl.to_project_slug = %s
              AND cpl.to_identifier = %s
              AND cpl.relationship = 'blocks'
            """,
            (project_slug, identifier),
        )
        for r in cur.fetchall():
            from_project_id = r["from_project_id"]
            from_identifier = r["from_identifier"]
            from_project_slug = r["from_project_slug"]
            wake_channel = _resolve_project_wake_channel(conn, project_id=from_project_id)
            target = wake_channel or default_target
            if not target:
                continue
            event = _build_wake_event(
                "dependency.resolved",
                {
                    "blocker_project": project_slug,
                    "blocker_identifier": identifier,
                    "unblocked_project": from_project_slug,
                    "unblocked_identifier": from_identifier,
                },
                source,
                meta={
                    "unblocked_project": from_project_slug,
                    "unblocked_identifier": from_identifier,
                },
            )
            buffer.append({"target": target, "secret": secret, "event": event})


def _process_event(
    conn: psycopg.Connection,
    payload: dict,
    default_target: str | None,
    secret: str,
    source: str,
    buffer: list[dict],
) -> None:
    """Inspect one NOTIFY payload and append routed events to the buffer."""
    event_type = payload.get("event_type", "")
    event_payload = payload.get("payload", {})

    if event_type == "request.created":
        _route_request_created(conn, event_payload, default_target, secret, source, buffer)
    elif event_type == "link.added":
        _route_link_added(conn, event_payload, default_target, secret, source, buffer)
    elif event_type == "item.closed":
        _route_item_closed(conn, event_payload, default_target, secret, source, buffer)
    else:
        # Other events are not cross-project routing concerns.
        pass


def _flush(buffer: list[dict]) -> None:
    """Flush the buffered events, grouping by target for coalescing."""
    if not buffer:
        return

    # Group by target URL to coalesce POSTs.
    by_target: dict[str, list[dict]] = {}
    for item in buffer:
        target = item["target"]
        by_target.setdefault(target, []).append(item)

    for target, items in by_target.items():
        secret = items[0]["secret"]
        # For multiple events to the same target, batch them in a single POST
        # using agent-wake's batch shape if supported.
        if len(items) == 1:
            _post_with_retry(target, secret, items[0]["event"]["source"], items[0]["event"])
        else:
            # Emit a batch event with all payloads.
            event = {
                "v": 0,
                "event_id": str(uuid.uuid4()),
                "source": items[0]["event"]["source"],
                "kind": "trigger-loop-batch",
                "content": f"Batch of {len(items)} trigger events",
                "meta": {
                    "count": str(len(items)),
                    "events": json.dumps(
                        [i["event"]["meta"] for i in items],
                        default=str,
                        sort_keys=True,
                    ),
                },
                "wake": True,
            }
            _post_with_retry(target, secret, event["source"], event)

    buffer.clear()


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
    """Run the trigger-loop. Blocks until SIGINT or max_events reached (tests)."""
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

    # Secret is optional for the trigger loop — if absent, we can still route
    # to targets that don't require signing, or we skip POST and just log.
    if not secret:
        _log.info("AGENT_NOTES_BRIDGE_SECRET not set; POSTs will be unsigned (or skipped)")

    from agent_notes.core.config import resolve_dsn

    effective_dsn = resolve_dsn(dsn)

    poll = max(batch_ms / 1000.0, 0.05)
    sent = 0
    buffer: list[dict] = []
    deadline: float | None = None

    conn = psycopg.connect(effective_dsn, autocommit=True)
    try:
        conn.execute("LISTEN agent_notes_op_log_events")
        _log.info("listening on agent_notes_op_log_events")
        while True:
            for n in conn.notifies(timeout=poll):
                try:
                    payload = json.loads(n.payload)
                except (json.JSONDecodeError, TypeError):
                    payload = {"raw": n.payload}
                _process_event(conn, payload, target, secret or "", source, buffer)
                if deadline is None:
                    deadline = time.monotonic() + batch_ms / 1000.0
                if len(buffer) >= batch_n:
                    break
            if buffer and (
                len(buffer) >= batch_n or (deadline is not None and time.monotonic() >= deadline)
            ):
                count = len(buffer)
                _flush(buffer)
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
        _log.error("trigger loop crashed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(main())
