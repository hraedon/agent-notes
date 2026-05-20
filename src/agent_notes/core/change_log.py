"""change_log write helpers and query interface (decision 7, 20).

`change_log` is the sole audit/history source. This module provides:
- `write_change` — INSERT a row; callers own the event type (decision 20).
- `changes_since` — query helper used by the `changes_since` core tool.
- `history` — chronological rows for a specific kind/identifier.

The NOTIFY trigger fires automatically on INSERT (defined in 000_core.sql).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


@dataclass
class ChangeLogRow:
    id: int
    kind: str
    workspace_id: int
    project_id: int | None
    identifier: str
    event: str
    payload: dict
    actor: str | None
    changed_at: datetime


def _row_to_cl(row: dict) -> ChangeLogRow:
    return ChangeLogRow(
        id=row["id"],
        kind=row["kind"],
        workspace_id=row["workspace_id"],
        project_id=row.get("project_id"),
        identifier=row["identifier"],
        event=row["event"],
        payload=row.get("payload") or {},
        actor=row.get("actor"),
        changed_at=row["changed_at"],
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def write_change(
    conn: psycopg.Connection,
    kind: str,
    workspace_id: int,
    project_id: int | None,
    identifier: str,
    event: str,
    payload: dict | None = None,
    actor: str | None = None,
) -> int:
    """INSERT a change_log row inside an already-open connection.

    Callers are responsible for the event type: 'filed' / 'updated' /
    'status_changed' / 'deleted' / 'projection_written' (decision 20).
    The NOTIFY trigger fires automatically on each INSERT.

    Returns the new row id.
    """
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        """
        INSERT INTO change_log
            (kind, workspace_id, project_id, identifier, event, payload, actor)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            kind,
            workspace_id,
            project_id,
            identifier,
            event,
            psycopg.types.json.Jsonb(payload or {}),
            actor,
        ),
    )
    row = cur.fetchone()
    return row["id"]


# ---------------------------------------------------------------------------
# Queries (pool-based, no open connection required)
# ---------------------------------------------------------------------------


def changes_since(
    since: datetime,
    kind: str | None = None,
    workspace_id: int | None = None,
    project_id: int | None = None,
    limit: int = 50,
) -> list[ChangeLogRow]:
    """Return change_log rows newer than `since`, newest-first."""
    from agent_notes.core.db import _conn

    conditions: list[str] = ["changed_at >= %s"]
    params: list[Any] = [since]

    if kind is not None:
        conditions.append("kind = %s")
        params.append(kind)
    if workspace_id is not None:
        conditions.append("workspace_id = %s")
        params.append(workspace_id)
    if project_id is not None:
        conditions.append("project_id = %s")
        params.append(project_id)

    where = " AND ".join(conditions)
    params.append(min(limit, 200))

    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"SELECT id, kind, workspace_id, project_id, identifier, event, payload, actor, "
            f"changed_at FROM change_log WHERE {where} ORDER BY changed_at DESC LIMIT %s",
            params,
        )
        return [_row_to_cl(r) for r in cur.fetchall()]


def history(
    kind: str,
    workspace_id: int,
    project_id: int,
    identifier: str,
    limit: int = 100,
) -> list[ChangeLogRow]:
    """Return change_log rows for a specific note in chronological order."""
    from agent_notes.core.db import _conn

    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT id, kind, workspace_id, project_id, identifier, event, payload, actor, "
            "changed_at FROM change_log "
            "WHERE kind = %s AND workspace_id = %s AND project_id = %s AND identifier = %s "
            "ORDER BY changed_at ASC LIMIT %s",
            (kind, workspace_id, project_id, identifier, min(limit, 500)),
        )
        return [_row_to_cl(r) for r in cur.fetchall()]
