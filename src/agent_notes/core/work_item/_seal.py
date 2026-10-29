"""Seal a ``work_items`` cache row into the op-log (Plan 025 Phase 1).

While regista was the write path, the (since deleted) write-through mirrored
each snapshot straight into the ``work_items`` cache and wrote no ops. Those
rows have no op chain, or a chain that stopped when the item moved to regista.
With native Postgres as the only authority, the next native write folds the
entity: a chainless row fails to fold, and a stale chain silently reverts the
row.

``seal`` appends the ops that make the fold reproduce the cached row exactly:
a ``create`` stub for a chainless entity (usually its op_id is the regista-era
entity_id; the snapshot parents the create's real op_id either way, so it never
dangles), then a ``snapshot`` op carrying the cached state. The snapshot also
supersedes an earlier tombstone: regista re-mirrored some natively deleted
items, so the cache row is the live truth. It writes nothing when the fold
already matches, so it is safe to call before every native mutation and from
the one-shot bulk backfill that ran at the Plan 025 cutover.
"""

from __future__ import annotations

import json
from typing import Any

import psycopg
from psycopg.rows import dict_row

from agent_notes.core import kernel

from . import _common

ENTITY_TYPE = "work_item"
SEAL_REASON = "plan-025-native-authority"

# The cache columns the fold owns (mirrors ``verifier._CACHE_COMPARE_FIELDS``),
# plus the embedding, which the fold also writes back on every native mutation.
SEALED_FIELDS: tuple[str, ...] = (
    "project_id",
    "identifier",
    "title",
    "body_hash",
    "kind",
    "status",
    "severity",
    "external_refs",
    "diagnostic_keys",
    "frontmatter_version",
)


class SealError(RuntimeError):
    """The op-log could not be made to reproduce the cached row."""


def _embedding_list(value: Any) -> list[float] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [float(x) for x in json.loads(value)]
    return [float(x) for x in value]


def drift(conn: psycopg.Connection, row: dict) -> tuple[dict | None, list[str]]:
    """Return ``(folded_state, differing_fields)`` for a cache row.

    ``folded_state`` is ``None`` when the entity has no usable op chain; the
    field list is then ``["op_chain"]``. An empty list means the fold already
    reproduces the row.
    """
    folded = kernel.fold_work_item_state(conn, row["entity_id"])
    if folded is None:
        return None, ["op_chain"]
    diffs = [f for f in SEALED_FIELDS if folded.get(f) != row.get(f)]
    if not _common.embedding_equal(folded.get("embedding"), row.get("embedding")):
        diffs.append("embedding")
    return folded, diffs


def _create_op_id(conn: psycopg.Connection, entity_id: str) -> str | None:
    """The op_id of the entity's earliest ``create`` op, if it has one."""
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        "SELECT op_id FROM op_log WHERE entity_id = %s AND op_type = 'create' "
        "ORDER BY lamport, op_id LIMIT 1",
        (entity_id,),
    )
    row = cur.fetchone()
    return row["op_id"] if row is not None else None


def _max_lamport(conn: psycopg.Connection, entity_id: str) -> int:
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        "SELECT COALESCE(MAX(lamport), 0) AS m FROM op_log WHERE entity_id = %s",
        (entity_id,),
    )
    return int(cur.fetchone()["m"])


def seal(conn: psycopg.Connection, row: dict, actor_id: str) -> list[str]:
    """Make the op-log fold reproduce ``row``; return the fields that drifted.

    Runs on the caller's transaction and never commits. Returns ``[]`` (and
    writes nothing) when the fold already matches. Raises :class:`SealError`
    if the written ops still do not reproduce the row (e.g. a tombstoned
    entity that still has a cache row).
    """
    folded, diffs = drift(conn, row)
    if not diffs:
        return []

    entity_id = row["entity_id"]
    regista_id = row.get("regista_work_item_id")
    regista_id = str(regista_id) if regista_id is not None else None
    anchor = _create_op_id(conn, entity_id)
    if anchor is None:
        anchor = kernel.commit_op(
            conn,
            entity_id=entity_id,
            entity_type=ENTITY_TYPE,
            op_type="create",
            payload={"identifier": row["identifier"], "regista_work_item_id": regista_id},
            actor_id=actor_id,
        )["op_id"]

    sealed = {f: row.get(f) for f in SEALED_FIELDS}
    sealed["external_refs"] = dict(sealed["external_refs"] or {})
    sealed["diagnostic_keys"] = dict(sealed["diagnostic_keys"] or {})
    # The vector is large; carry it only when the fold would not already
    # reproduce it (a chainless row, or a drifted embedding).
    if folded is None or "embedding" in diffs:
        sealed["embedding"] = _embedding_list(row.get("embedding"))
    kernel.commit_op(
        conn,
        entity_id=entity_id,
        entity_type=ENTITY_TYPE,
        op_type="snapshot",
        payload={
            "sealed_state": sealed,
            "reason": SEAL_REASON,
            "regista_work_item_id": regista_id,
            # Distinguishes a re-seal of an identical state at a later point.
            "after_lamport": _max_lamport(conn, entity_id),
        },
        parent_op_ids=[anchor],
        actor_id=actor_id,
    )

    _after, remaining = drift(conn, row)
    if remaining:
        raise SealError(
            f"op-log for work item {row['identifier']!r} still differs from the "
            f"cache after sealing: {', '.join(remaining)}"
        )
    return diffs


def lock_row(conn: psycopg.Connection, project_id: int, identifier: str) -> dict | None:
    """Lock and return the cache row for the rest of the caller's transaction.

    Native writers take this lock before reading the row they fold, so a seal
    never compares the op-log with a row another writer has since replaced.
    """
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        "SELECT * FROM work_items WHERE project_id = %s AND identifier = %s FOR UPDATE",
        (project_id, identifier),
    )
    row = cur.fetchone()
    return dict(row) if row is not None else None


def seal_for_write(
    conn: psycopg.Connection, project_id: int, identifier: str, actor_id: str
) -> None:
    """Lock the row (if present) and seal it before a native mutation folds it."""
    row = lock_row(conn, project_id, identifier)
    if row is not None:
        seal(conn, row, actor_id)
