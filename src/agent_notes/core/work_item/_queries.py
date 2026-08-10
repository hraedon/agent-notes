"""Read-side queries and delete for work items.

Read-only queries (``get``, ``query``, ``find``, ``suggest_duplicates``,
``diagnose``, ``get_work_item_body``), coordination queries (``ready``,
``claimable``), and the regista-path delete. Native-path delete lives in
``_native.delete_work_item``; the facade selects between them.
"""

from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row

from agent_notes.core import face_factory, kernel
from agent_notes.core.change_log import write_change
from agent_notes.core.db import _conn

from . import _common

KIND = "work_item"


def get_work_item(project_id: int, identifier: str) -> dict | None:
    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT * FROM work_items WHERE project_id = %s AND identifier = %s",
            (project_id, identifier),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def query_work_items(
    project_id: int | None = None,
    workspace_id: int | None = None,
    status: str | None = None,
    kind: str | None = None,
    severity: str | None = None,
    is_open: bool | None = None,
    identifier: str | None = None,
    limit: int = 50,
) -> list[dict]:
    conditions: list[str] = []
    params: list[Any] = []

    if project_id is not None:
        conditions.append("wi.project_id = %s")
        params.append(project_id)
    if workspace_id is not None:
        conditions.append(
            "EXISTS (SELECT 1 FROM projects p WHERE p.id = wi.project_id AND p.workspace_id = %s)"
        )
        params.append(workspace_id)
    if status is not None:
        conditions.append("wi.status = %s")
        params.append(status)
    if kind is not None:
        conditions.append("wi.kind = %s")
        params.append(kind)
    if severity is not None:
        conditions.append("wi.severity = %s")
        params.append(severity)
    if is_open is not None:
        if is_open:
            conditions.append("wi.closed_at IS NULL")
        else:
            conditions.append("wi.closed_at IS NOT NULL")
    if identifier is not None:
        conditions.append("wi.identifier = %s")
        params.append(identifier)

    where = " AND ".join(conditions) if conditions else "TRUE"
    params.append(min(limit, 200))

    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT wi.*,
                   p.slug AS project_slug,
                   p.workspace_id AS workspace_id
            FROM work_items wi
            JOIN projects p ON p.id = wi.project_id
            WHERE {where}
            ORDER BY wi.updated_at DESC
            LIMIT %s
            """,
            params,
        )
        return [dict(r) for r in cur.fetchall()]


def _like_pattern(text: str) -> str:
    """Escape LIKE metacharacters and wrap in wildcards (``ESCAPE '\\'``)."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def find_work_items(
    query_vec: Any,
    project_id: int | None = None,
    workspace_id: int | None = None,
    limit: int = 10,
    query_text: str | None = None,
) -> list[dict]:
    """Find work items by similarity, with exact title matches guaranteed (WI-052).

    The semantic leg is a top-k nearest-neighbour search: it always returns the
    *k* closest embeddings, so it has no notion of a "complete" match set — an
    item whose title literally contains the query can rank below the cut and
    silently vanish (the Plan 020 qualification lost 4/12 and 2/5 just-filed
    items to exactly this, and read it as index staleness). When ``query_text``
    is given, items whose title contains it (case-insensitive) are therefore
    selected lexically first — independent of embedding rank, and even when the
    row has no embedding — and the semantic k-NN fills the remaining slots.
    Rows carry ``match`` (``"title"`` / ``"semantic"``) so callers can see
    which leg produced them.
    """
    scope_conditions: list[str] = []
    scope_params: list[Any] = []

    if project_id is not None:
        scope_conditions.append("wi.project_id = %s")
        scope_params.append(project_id)
    if workspace_id is not None:
        scope_conditions.append(
            "EXISTS (SELECT 1 FROM projects p2 WHERE p2.id = wi.project_id"
            " AND p2.workspace_id = %s)"
        )
        scope_params.append(workspace_id)

    limit_val = min(limit, 50)
    rows: list[dict] = []
    seen: set[tuple[int, str]] = set()

    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)

        if query_text and query_text.strip():
            lex_where = " AND ".join(["wi.title ILIKE %s ESCAPE '\\'"] + scope_conditions)
            cur.execute(
                f"""
                SELECT wi.*,
                       p.slug AS project_slug,
                       p.workspace_id AS workspace_id,
                       wi.embedding <=> %s::vector AS distance,
                       'title' AS match
                FROM work_items wi
                JOIN projects p ON p.id = wi.project_id
                WHERE {lex_where}
                ORDER BY wi.updated_at DESC
                LIMIT %s
                """,
                [query_vec, _like_pattern(query_text.strip())] + scope_params + [limit_val],
            )
            for r in cur.fetchall():
                row = dict(r)
                rows.append(row)
                seen.add((row["project_id"], row["identifier"]))

        if len(rows) < limit_val:
            sem_where = " AND ".join(["wi.embedding IS NOT NULL"] + scope_conditions)
            cur.execute(
                f"""
                SELECT wi.*,
                       p.slug AS project_slug,
                       p.workspace_id AS workspace_id,
                       wi.embedding <=> %s::vector AS distance,
                       'semantic' AS match
                FROM work_items wi
                JOIN projects p ON p.id = wi.project_id
                WHERE {sem_where}
                ORDER BY wi.embedding <=> %s::vector
                LIMIT %s
                """,
                [query_vec] + scope_params + [query_vec, limit_val],
            )
            for r in cur.fetchall():
                key = (r["project_id"], r["identifier"])
                if key in seen:
                    continue
                rows.append(dict(r))
                if len(rows) >= limit_val:
                    break

    return rows


def suggest_duplicates(project_id: int, identifier: str, threshold: float = 0.95) -> list[dict]:
    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT embedding FROM work_items
            WHERE project_id = %s AND identifier = %s AND embedding IS NOT NULL
            """,
            (project_id, identifier),
        )
        row = cur.fetchone()
        if row is None or row["embedding"] is None:
            return []

        vec = row["embedding"]
        cur.execute(
            """
            SELECT identifier, title, status,
                   1 - (embedding <=> %s::vector) AS similarity
            FROM work_items
            WHERE project_id = %s
              AND identifier != %s
              AND embedding IS NOT NULL
              AND embedding <=> %s::vector <= 1 - %s
            ORDER BY similarity DESC
            LIMIT 10
            """,
            (vec, project_id, identifier, vec, threshold),
        )
        return [dict(r) for r in cur.fetchall()]


def diagnose(project_id: int, identifier: str, kind: str = KIND) -> dict:
    from agent_notes.core.change_log import history as cl_history
    from agent_notes.core.kernel import _get_entity_ops

    wi = get_work_item(project_id, identifier)
    if wi is None:
        raise ValueError(f"Work item not found: {identifier!r}")

    with _conn() as conn:
        ws_id = _common.resolve_workspace_for_project(conn, project_id)
        entity_id = wi["entity_id"]
        ops = _get_entity_ops(conn, entity_id)

    cl_rows = cl_history(kind, ws_id, project_id, identifier, limit=20)
    return {
        "work_item": wi,
        "diagnostic_keys": wi.get("diagnostic_keys") or {},
        "ops": [
            {
                "op_type": op["op_type"],
                "lamport": op["lamport"],
                "created_at": op["created_at"].isoformat(),
            }
            for op in ops
        ],
        "recent_changes": [
            {
                "event": r.event,
                "changed_at": r.changed_at.isoformat(),
                "payload": r.payload,
            }
            for r in cl_rows
        ],
    }


def get_work_item_body(project_id: int, identifier: str) -> str | None:
    """Return the body text for a work item by looking up its blob."""
    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT body_hash FROM work_items WHERE project_id = %s AND identifier = %s",
            (project_id, identifier),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return kernel.get_blob(conn, row["body_hash"])


def ready_work_items(
    project_id: int | None = None,
    workspace_id: int | None = None,
    limit: int = 50,
) -> list[dict]:
    return kernel.ready_work_items(project_id, workspace_id, limit)


def claimable_work_items(
    project_id: int | None = None,
    workspace_id: int | None = None,
    limit: int = 50,
) -> list[dict]:
    return kernel.claimable_work_items(project_id, workspace_id, limit)


def delete_work_item_regista(project_id: int, identifier: str) -> bool:
    """Regista-path delete: drops local cache + leases; regista retains the item."""
    with _conn() as conn:
        workspace_id = _common.resolve_workspace_for_project(conn, project_id)
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT * FROM work_items WHERE project_id = %s AND identifier = %s",
            (project_id, identifier),
        )
        old = cur.fetchone()
        if old is None:
            return False
        cur.execute(
            "DELETE FROM work_item_leases WHERE entity_id = %s",
            (old["entity_id"],),
        )
        cur.execute(
            "DELETE FROM work_items WHERE project_id = %s AND identifier = %s",
            (project_id, identifier),
        )
        write_change(
            conn,
            kind=KIND,
            workspace_id=workspace_id,
            project_id=project_id,
            identifier=identifier,
            event="deleted",
            payload={"title": old["title"], "regista_retained": True},
            actor=face_factory.actor_with_overrides(operation="work-item delete").actor_id,
        )
        conn.commit()
    return True
