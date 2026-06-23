"""Work-log coordination kernel — op-CRDT model (Plan 008 P0–P2).

Public surface:
- `create_op` / `commit_op` — write an op to the op-log and fold into cache.
- `fold_work_item_state` — pure in-memory fold of a work_item's state (no DB write).
- `fold_work_item` — fold and upsert into the `work_items` cache.
- `fold_all_work_items` — rebuild the entire cache (useful for recovery).
- `fold_all` — rebuild the entire cache (useful for recovery).
- `merge_entity` — deterministic merge of two divergent chains (P2).
- `reconcile_entity` — merge remote ops into local, write a `merge` op (P2).
- `ready_work_items` — query the `work_items_ready_v` view.
- `content_hash` — SHA-256 of canonical text for content-addressed blobs.

Design notes:
- P0 is single-writer, so ``op_log.id`` (BIGSERIAL) acts as the lamport clock.
- P2 (multi-writer) replaces this with per-actor Lamport counters.
- Bodies are content-addressed so metadata edits never re-log the body.
- The fold is deterministic: apply ops ordered by ``(lamport, op_id)``.
- Status lattice (P2): fail-safe, open dominates closed (surface unfinished work).
- Merge (P2): union of ops from both chains, sort by (lamport, op_id), fold.
"""

from __future__ import annotations

import hashlib
import json
import threading
from typing import Any

import psycopg
from psycopg.rows import dict_row

from agent_notes.core.db import _conn
from agent_notes.core.envelope import NullSigner, make_envelope

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
    signer: Any | None = None,
) -> dict:
    """Write an op to op_log and return the committed op dict.

    This is the low-level primitive. Callers (e.g. WorkItemModel) should
    open a connection, call this, then fold the entity, then commit.

    From P0 every op carries a DSSE envelope in ``payload['envelope']``;
    the envelope is *not* part of the ``op_id`` hash (decision: the op is
    the payload inside the envelope).  ``signer`` defaults to ``NullSigner``
    so the structural format is present from day one; flipping to
    ``LocalKeySigner`` in P1 is a config change.
    """
    parent_op_ids = parent_op_ids or []
    op_id = _make_op_id(entity_type, op_type, payload, parent_op_ids)
    lamport = _next_lamport(conn)

    signer = signer or NullSigner()
    effective_actor_id = actor_id if actor_id is not None else signer.key_id()

    # Build envelope around the inner payload; envelope is stored inside the
    # JSONB payload column but excluded from the op_id hash.
    envelope = make_envelope(
        payload_type="agent-provenance-v0/op",
        payload=payload,
        signer=signer,
    )
    stored_payload = dict(payload)
    stored_payload["envelope"] = envelope

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
            effective_actor_id,
            psycopg.types.json.Jsonb(stored_payload),
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


# ---------------------------------------------------------------------------
# Status lattice (P2)
# ---------------------------------------------------------------------------

# Fail-safe direction: open dominates closed (surface unfinished work).
_STATUS_LATTICE = {
    "open": 3,
    "claimed": 2,
    "closed": 1,
    "deferred": 0,
}


def _status_rank(status: str | None) -> int:
    return _STATUS_LATTICE.get(status, -1) if status is not None else -1


def _resolve_status_lattice(
    status_ops: list[dict],
    current_status: str | None,
) -> str | None:
    """Pick the winning status from a set of concurrent ops.

    For a single status op (sequential case) the new status is applied
    directly — last-write-wins.  For multiple concurrent status ops,
    the higher lattice rank wins.  Ties are broken by lexicographically
    smaller ``op_id`` (deterministic, content-addressed).
    """
    if not status_ops:
        return current_status

    # Single op: sequential, apply directly.
    if len(status_ops) == 1:
        op = status_ops[0]
        if op["op_type"] == "close":
            return "closed"
        return (op.get("payload") or {}).get("status")

    # Multiple ops: concurrent — resolve among them by lattice.
    winning_status: str | None = None
    winning_op_id: str | None = None

    for op in status_ops:
        op_type = op["op_type"]
        op_id = op["op_id"]
        if op_type == "close":
            new_status = "closed"
        else:
            new_status = (op.get("payload") or {}).get("status")

        if new_status is None:
            continue

        if winning_status is None:
            winning_status = new_status
            winning_op_id = op_id
            continue

        current_rank = _status_rank(winning_status)
        new_rank = _status_rank(new_status)

        if new_rank > current_rank:
            winning_status = new_status
            winning_op_id = op_id
        elif new_rank == current_rank and op_id < (winning_op_id or ""):
            winning_status = new_status
            winning_op_id = op_id

    return winning_status


def _apply_op_to_state(state: dict[str, Any], op: dict) -> None:
    """Apply a single non-status op to the state accumulator."""
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

    elif op_type == "snapshot":
        sealed = payload.get("sealed_state", {})
        state.update(sealed)

    elif op_type == "merge":
        merged = payload.get("merged_state", {})
        state.update(merged)


def fold_work_item_state(conn: psycopg.Connection, entity_id: str) -> dict | None:
    """Rebuild a work_item's state from its op chain, in-memory (no DB write).

    P2 semantics:
    - Ops are grouped by ``lamport``; concurrent ops (same lamport) have their
      status changes resolved by the fail-safe lattice (open > claimed > closed).
    - ``merge`` ops replace state with the merged payload.

    Returns the folded state dict, or ``None`` if the entity has no ops or is a
    dangling entity (no create op).  This is the pure fold;
    :func:`fold_work_item` wraps it to also upsert into the ``work_items``
    cache, and the verifier uses it to compare the fold against the live cache.
    """
    ops = _get_entity_ops(conn, entity_id)
    if not ops:
        return None

    # Group ops by lamport for concurrent resolution.
    groups: list[list[dict]] = []
    current_lamport: int | None = None
    current_group: list[dict] = []
    for op in ops:
        if op["lamport"] != current_lamport:
            if current_group:
                groups.append(current_group)
            current_lamport = op["lamport"]
            current_group = [op]
        else:
            current_group.append(op)
    if current_group:
        groups.append(current_group)

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

    for group in groups:
        status_ops: list[dict] = []
        for op in group:
            if op["op_type"] in ("set_status", "close"):
                status_ops.append(op)
            else:
                _apply_op_to_state(state, op)

        if status_ops:
            state["status"] = _resolve_status_lattice(status_ops, state.get("status"))

    # If we never got a create op, this is a dangling entity.
    if state["project_id"] is None or state["identifier"] is None:
        return None

    return state


def fold_work_item(conn: psycopg.Connection, entity_id: str) -> dict | None:
    """Rebuild a work_item from its op chain and write to the cache.

    Wraps :func:`fold_work_item_state` (the pure in-memory fold) and upserts
    the result into the ``work_items`` cache.

    Returns the cached work_item dict, or ``None`` if the entity has no ops.
    """
    state = fold_work_item_state(conn, entity_id)
    if state is None:
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
# Merge / reconcile (P2)
# ---------------------------------------------------------------------------


def merge_entity(local_ops: list[dict], remote_ops: list[dict]) -> list[dict]:
    """Deterministic merge of two divergent op chains.

    Returns the merged, sorted list of ops. The merge is:
    1. Union all ops (deduplicate by op_id).
    2. Sort by (lamport, op_id).
    3. Return the sorted list.

    This is the git-bag op-CRDT merge primitive. The fold of the merged
    chain is deterministic because the sort is deterministic.
    """
    # Build a set of seen op_ids and a list of unique ops.
    seen: set[str] = set()
    merged: list[dict] = []
    for op in local_ops + remote_ops:
        op_id = op["op_id"]
        if op_id not in seen:
            seen.add(op_id)
            merged.append(op)

    # Sort by (lamport, op_id) for deterministic fold.
    merged.sort(key=lambda op: (op["lamport"], op["op_id"]))
    return merged


def reconcile_entity(
    conn: psycopg.Connection,
    entity_id: str,
    remote_ops: list[dict],
    actor_id: str | None = None,
) -> dict:
    """Reconcile remote ops into the local log for an entity.

    Steps:
    1. Fetch local ops.
    2. Merge with remote ops (deterministic union + sort).
    3. Fold the merged chain to get the reconciled state.
    4. Write a ``merge`` op that records the merged state.
    5. Re-fold and update the cache.

    Returns the folded work_item dict.
    """
    local_ops = _get_entity_ops(conn, entity_id)
    merged_ops = merge_entity(local_ops, remote_ops)

    # Fold the merged chain to determine the reconciled state.
    # We do this in-memory by reusing the fold logic.
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

    current_lamport: int | None = None
    current_group: list[dict] = []

    def _flush_group() -> None:
        nonlocal current_group
        if not current_group:
            return
        status_ops = [op for op in current_group if op["op_type"] in ("set_status", "close")]
        for op in current_group:
            if op["op_type"] not in ("set_status", "close"):
                _apply_op_to_state(state, op)
        if status_ops:
            state["status"] = _resolve_status_lattice(status_ops, state.get("status"))
        current_group = []

    for op in merged_ops:
        lamport = op["lamport"]
        if lamport != current_lamport:
            _flush_group()
            current_lamport = lamport
        current_group.append(op)
    _flush_group()

    # Write a merge op that records the reconciled state.
    # We only write the merge op if the state is valid (has project_id and identifier).
    if state["project_id"] is None or state["identifier"] is None:
        # No create op in the merged chain — nothing to reconcile.
        return None

    parent_op_ids = [op["op_id"] for op in merged_ops]
    merge_payload = {
        "merged_state": {
            "project_id": state["project_id"],
            "identifier": state["identifier"],
            "title": state["title"],
            "body_hash": state["body_hash"],
            "kind": state["kind"],
            "status": state["status"],
            "severity": state["severity"],
            "external_refs": state["external_refs"],
            "diagnostic_keys": state["diagnostic_keys"],
            "embedding": state["embedding"],
            "frontmatter_version": state["frontmatter_version"],
        }
    }

    merge_op = commit_op(
        conn,
        entity_id=entity_id,
        entity_type="work_item",
        op_type="merge",
        payload=merge_payload,
        parent_op_ids=parent_op_ids,
        actor_id=actor_id,
    )

    # Re-fold into cache.
    folded = fold_work_item(conn, entity_id)
    if folded is None:
        raise RuntimeError("fold_work_item returned None after merge op")

    emit_event(
        conn,
        op_id=merge_op["op_id"],
        event_type="item.merged",
        payload={
            "entity_id": entity_id,
            "identifier": state["identifier"],
            "merged_op_count": len(merged_ops),
        },
    )

    return dict(folded)


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
