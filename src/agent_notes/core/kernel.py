"""Work-log coordination kernel — op-CRDT model for single-writer (Plan 008 P0).

Public surface:
- `create_op` / `commit_op` — write an op to the op-log and fold into cache.
- `fold_entity` — rebuild current state for an entity from its op chain.
- `fold_all` — rebuild the entire cache (useful for recovery).
- `ready_work_items` — query the `work_items_ready_v` view.
- `content_hash` — SHA-256 of canonical text for content-addressed blobs.

Design notes:
- P0 is single-writer, so ``op_log.id`` (BIGSERIAL) acts as the lamport clock.
- P2 (multi-writer) replaces this with per-actor Lamport counters.
- Bodies are content-addressed so metadata edits never re-log the body.
- The fold is deterministic: apply ops ordered by ``(lamport, op_id)``.
- Status lattice for P0: simple last-write-wins (no concurrent conflict yet).
"""

from __future__ import annotations

import hashlib
import json
import threading
from typing import Any

import psycopg
from psycopg.rows import dict_row

from agent_notes.core.db import _conn

# ---------------------------------------------------------------------------
# Content-addressed blobs
# ---------------------------------------------------------------------------


def content_hash(text: str) -> str:
    """Return SHA-256 hex digest of canonical UTF-8 bytes."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def store_blob(conn: psycopg.Connection, text: str) -> str:
    """Store text in content_blobs, returning its hash. Idempotent."""
    h = content_hash(text)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO content_blobs (hash, content) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (h, text),
    )
    return h


def get_blob(conn: psycopg.Connection, h: str) -> str | None:
    """Retrieve blob content by hash."""
    cur = conn.cursor(row_factory=dict_row)
    cur.execute("SELECT content FROM content_blobs WHERE hash = %s", (h,))
    row = cur.fetchone()
    return row["content"] if row else None


# ---------------------------------------------------------------------------
# Op creation and commit
# ---------------------------------------------------------------------------


def _make_op_id(entity_type: str, op_type: str, payload: dict, parent_op_ids: list[str]) -> str:
    """Deterministic content hash of an op."""
    canonical = json.dumps(
        {
            "entity_type": entity_type,
            "op_type": op_type,
            "payload": payload,
            "parent_op_ids": sorted(parent_op_ids),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Lamport clock (P0: single-writer = database BIGSERIAL)
# ---------------------------------------------------------------------------

_lamport_lock = threading.Lock()


def _next_lamport(conn: psycopg.Connection) -> int:
    """Return the next lamport value. In P0 this is the next op_log.id."""
    # We use the sequence directly to avoid race conditions.
    cur = conn.cursor(row_factory=dict_row)
    cur.execute("SELECT nextval('op_log_id_seq') AS lamport")
    row = cur.fetchone()
    return row["lamport"]


# ---------------------------------------------------------------------------
# Core op commit
# ---------------------------------------------------------------------------


def commit_op(
    conn: psycopg.Connection,
    entity_id: str,
    entity_type: str,
    op_type: str,
    payload: dict,
    parent_op_ids: list[str] | None = None,
    actor_id: str | None = None,
) -> dict:
    """Write an op to op_log and return the committed op dict.

    This is the low-level primitive. Callers (e.g. WorkItemModel) should
    open a connection, call this, then fold the entity, then commit.
    """
    parent_op_ids = parent_op_ids or []
    op_id = _make_op_id(entity_type, op_type, payload, parent_op_ids)
    lamport = _next_lamport(conn)

    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        """
        INSERT INTO op_log
            (op_id, entity_id, entity_type, op_type, lamport, actor_id, payload, parent_op_ids)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id, op_id, entity_id, entity_type, op_type, lamport, actor_id, payload,
                  parent_op_ids, created_at
        """,
        (
            op_id,
            entity_id,
            entity_type,
            op_type,
            lamport,
            actor_id,
            psycopg.types.json.Jsonb(payload),
            parent_op_ids,
        ),
    )
    row = cur.fetchone()
    return dict(row)


# ---------------------------------------------------------------------------
# Fold: rebuild entity state from op chain
# ---------------------------------------------------------------------------


def _get_entity_ops(conn: psycopg.Connection, entity_id: str) -> list[dict]:
    """Return all ops for an entity, ordered by (lamport, op_id)."""
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        """
        SELECT op_id, op_type, lamport, payload, parent_op_ids, created_at
        FROM op_log
        WHERE entity_id = %s
        ORDER BY lamport ASC, op_id ASC
        """,
        (entity_id,),
    )
    return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Work-item fold
# ---------------------------------------------------------------------------


def fold_work_item(conn: psycopg.Connection, entity_id: str) -> dict | None:
    """Rebuild a work_item from its op chain and write to the cache.

    Returns the folded work_item dict, or None if the entity has no ops.
    """
    ops = _get_entity_ops(conn, entity_id)
    if not ops:
        return None

    # State accumulator
    state: dict[str, Any] = {
        "entity_id": entity_id,
        "project_id": None,
        "identifier": None,
        "title": None,
        "body_hash": None,
        "kind": None,
        "status": None,
        "severity": "medium",
        "external_refs": {},
        "diagnostic_keys": {},
        "embedding": None,
        "frontmatter_version": 1,
    }

    for op in ops:
        op_type = op["op_type"]
        payload = op.get("payload") or {}

        if op_type == "create":
            state["project_id"] = payload.get("project_id")
            state["identifier"] = payload.get("identifier")
            state["title"] = payload.get("title")
            state["body_hash"] = payload.get("body_hash")
            state["kind"] = payload.get("kind")
            state["status"] = payload.get("status", "open")
            state["severity"] = payload.get("severity", "medium")
            state["external_refs"] = payload.get("external_refs", {})
            state["diagnostic_keys"] = payload.get("diagnostic_keys", {})
            state["embedding"] = payload.get("embedding")
            state["frontmatter_version"] = payload.get("frontmatter_version", 1)

        elif op_type == "set_status":
            state["status"] = payload.get("status")

        elif op_type == "set_field":
            for field in ("title", "kind", "severity", "body_hash", "frontmatter_version"):
                if field in payload:
                    state[field] = payload[field]
            if "external_refs" in payload:
                state["external_refs"].update(payload["external_refs"])
            if "diagnostic_keys" in payload:
                state["diagnostic_keys"].update(payload["diagnostic_keys"])
            if "embedding" in payload:
                state["embedding"] = payload["embedding"]

        elif op_type == "close":
            state["status"] = "closed"

        elif op_type == "snapshot":
            # snapshot op carries a sealed state payload; replace entirely.
            sealed = payload.get("sealed_state", {})
            state.update(sealed)

    # If we never got a create op, this is a dangling entity.
    if state["project_id"] is None or state["identifier"] is None:
        return None

    # Upsert into work_items cache.
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        """
        INSERT INTO work_items
            (entity_id, project_id, identifier, title, body_hash, kind, status,
             severity, external_refs, diagnostic_keys, embedding, frontmatter_version)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (entity_id) DO UPDATE SET
            project_id      = EXCLUDED.project_id,
            identifier      = EXCLUDED.identifier,
            title           = EXCLUDED.title,
            body_hash       = EXCLUDED.body_hash,
            kind            = EXCLUDED.kind,
            status          = EXCLUDED.status,
            severity        = EXCLUDED.severity,
            external_refs   = EXCLUDED.external_refs,
            diagnostic_keys = EXCLUDED.diagnostic_keys,
            embedding       = EXCLUDED.embedding,
            frontmatter_version = EXCLUDED.frontmatter_version,
            updated_at      = now()
        RETURNING *
        """,
        (
            state["entity_id"],
            state["project_id"],
            state["identifier"],
            state["title"],
            state["body_hash"],
            state["kind"],
            state["status"],
            state["severity"],
            psycopg.types.json.Jsonb(state["external_refs"]),
            psycopg.types.json.Jsonb(state["diagnostic_keys"]),
            state["embedding"],
            state["frontmatter_version"],
        ),
    )
    row = cur.fetchone()
    return dict(row)


# ---------------------------------------------------------------------------
# Fold all entities (recovery / rebuild)
# ---------------------------------------------------------------------------


def fold_all_work_items(conn: psycopg.Connection) -> int:
    """Rebuild the entire work_items cache from the op_log.

    Returns the number of work_items rebuilt.
    """
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        """
        SELECT DISTINCT entity_id FROM op_log
        WHERE entity_type = 'work_item'
        ORDER BY entity_id
        """
    )
    entity_ids = [r["entity_id"] for r in cur.fetchall()]
    count = 0
    for entity_id in entity_ids:
        folded = fold_work_item(conn, entity_id)
        if folded:
            count += 1
    return count


# ---------------------------------------------------------------------------
# Event emission
# ---------------------------------------------------------------------------


def emit_event(
    conn: psycopg.Connection,
    op_id: str,
    event_type: str,
    payload: dict | None = None,
) -> int:
    """Write an event row to op_log_events.

    Returns the event id. This is the post-commit hook surface.
    """
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        """
        INSERT INTO op_log_events (op_id, event_type, payload)
        VALUES (%s, %s, %s)
        RETURNING id
        """,
        (op_id, event_type, psycopg.types.json.Jsonb(payload or {})),
    )
    row = cur.fetchone()
    return row["id"]


# ---------------------------------------------------------------------------
# Event queries
# ---------------------------------------------------------------------------


def events_since(
    cursor: int,
    event_type: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Return op_log_events newer than ``cursor``, newest-first.

    ``cursor`` is the last-seen ``op_log_events.id``. Pass 0 for the tail.
    """
    conditions = ["id > %s"]
    params: list[Any] = [cursor]
    if event_type is not None:
        conditions.append("event_type = %s")
        params.append(event_type)
    where = " AND ".join(conditions)
    params.append(min(limit, 200))

    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT id, op_id, event_type, payload, created_at
            FROM op_log_events
            WHERE {where}
            ORDER BY id DESC
            LIMIT %s
            """,
            params,
        )
        return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Ready / claimable queries
# ---------------------------------------------------------------------------


def ready_work_items(
    project_id: int | None = None,
    workspace_id: int | None = None,
    limit: int = 50,
) -> list[dict]:
    """Query the ``work_items_ready_v`` view."""
    conditions: list[str] = []
    params: list[Any] = []
    if project_id is not None:
        conditions.append("project_id = %s")
        params.append(project_id)
    if workspace_id is not None:
        conditions.append("workspace_id = %s")
        params.append(workspace_id)
    where = " AND ".join(conditions) if conditions else "TRUE"
    params.append(min(limit, 200))

    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT id, entity_id, project_id, identifier, title, status,
                   kind, severity, created_at, updated_at, workspace_id,
                   project_slug, workspace_slug
            FROM work_items_ready_v
            WHERE {where}
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            params,
        )
        return [dict(r) for r in cur.fetchall()]


def claimable_work_items(
    project_id: int | None = None,
    workspace_id: int | None = None,
    limit: int = 50,
) -> list[dict]:
    """Query the ``work_items_claimable_v`` view.

    For P0 this is identical to ``ready``; P4 adds the lease table.
    """
    return ready_work_items(project_id, workspace_id, limit)
