"""Memory server model — encapsulates DB operations for the memory kind (Phase 3, 3.6).

Pulled from `agent_notes.servers.memory` during Plan 004 Phase 9a (decision 50).
The model layer is presentation-agnostic: it returns dicts/lists, not formatted
strings. CLI and legacy MCP server both consume the same model.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import psycopg
from psycopg.rows import dict_row

from agent_notes.core.change_log import write_change
from agent_notes.core.db import _conn

_log = logging.getLogger(__name__)

_KIND = "memory"

_WIKILINK_RE = re.compile(r"(?<!`)\[\[([^\]`]+)\]\](?!`)")

_GAP_SECTION_RE = re.compile(
    r"^##\s+Gaps?\s+to\s+flag\s*\n(.*?)(?=\n##|\Z)",
    re.IGNORECASE | re.DOTALL | re.MULTILINE,
)

_GAP_LINE_RE = re.compile(r"^\s*-\s+\[\[([^\]]+)\]\]\s*:\s*(.+)$", re.MULTILINE)


def _validate_memory_type(workspace_id: int, memory_type: str) -> None:
    from agent_notes.core.db import list_vocabulary

    vocabs = list_vocabulary(workspace_id, kind_namespace="memory_type")
    if not any(v.name == memory_type for v in vocabs):
        raise ValueError(
            f"memory_type '{memory_type}' not found in vocabularies. "
            f"Available: {', '.join(v.name for v in vocabs) or '(none)'}"
        )


def parse_wikilinks(body: str) -> list[str]:
    """Extract [[name]] references from body, skipping inline-code spans."""
    return _WIKILINK_RE.findall(body)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def add_memory(
    workspace_id: int,
    project_id: int,
    name: str,
    memory_type: str,
    body: str,
    attributes: dict | None = None,
    embedding: Any | None = None,
) -> dict:
    _validate_memory_type(workspace_id, memory_type)

    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)

        # Check for existing active memory with the same name — supersede it.
        cur.execute(
            "SELECT id FROM memories WHERE project_id = %s AND name = %s AND active = true",
            (project_id, name),
        )
        existing = cur.fetchone()

        old_id = None
        if existing:
            old_id = existing["id"]
            cur.execute("UPDATE memories SET active = false WHERE id = %s", (old_id,))

        cur.execute(
            """
            INSERT INTO memories
                (workspace_id, project_id, name, memory_type, body, embedding,
                 active, supersedes, attributes)
            VALUES (%s, %s, %s, %s, %s, %s, true, %s, %s)
            RETURNING id, workspace_id, project_id, name, memory_type, body,
                     active, supersedes, attributes, created_at, updated_at
            """,
            (
                workspace_id,
                project_id,
                name,
                memory_type,
                body,
                embedding,
                old_id,
                psycopg.types.json.Jsonb(attributes or {}),
            ),
        )
        row = cur.fetchone()
        new_id = row["id"]

        write_change(
            conn,
            kind=_KIND,
            workspace_id=workspace_id,
            project_id=project_id,
            identifier=name,
            event="filed",
            payload={"memory_type": memory_type, "id": new_id},
        )
        conn.commit()

    # Auto-create [[name]] relates_to links (Phase 3.3). Best-effort.
    _auto_create_wikilinks(workspace_id, project_id, name, body)

    return dict(row)


def _auto_create_wikilinks(
    workspace_id: int,
    project_id: int,
    name: str,
    body: str,
) -> None:
    from agent_notes.core.links import add_link

    for ref_name in set(parse_wikilinks(body)):
        try:
            add_link(
                from_kind=_KIND,
                from_workspace=workspace_id,
                from_project=project_id,
                from_identifier=name,
                to_kind=_KIND,
                to_workspace=workspace_id,
                to_project=project_id,
                to_identifier=ref_name,
                relationship="relates_to",
            )
        except (psycopg.Error, ValueError):
            _log.debug("wikilink auto-create skipped: %s -> %s", name, ref_name, exc_info=True)


def get_memory(workspace_id: int, project_id: int, name: str) -> dict | None:
    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT id, name, memory_type, body, attributes,
                   supersedes, created_at, updated_at
            FROM memories WHERE project_id = %s AND workspace_id = %s
            AND name = %s AND active = true
            """,
            (project_id, workspace_id, name),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def list_memories(
    workspace_id: int,
    project_id: int | None = None,
    memory_type: str | None = None,
    limit: int = 50,
) -> list[dict]:
    conditions = ["active = true"]
    params: list[Any] = []

    if workspace_id is not None:
        conditions.append("workspace_id = %s")
        params.append(workspace_id)
    if project_id is not None:
        conditions.append("project_id = %s")
        params.append(project_id)
    if memory_type is not None:
        conditions.append("memory_type = %s")
        params.append(memory_type)

    where = " AND ".join(conditions)
    params.append(min(limit, 200))

    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT id, name, memory_type, LEFT(body, 120) AS body_preview,
                   created_at, updated_at
            FROM memories
            WHERE {where}
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            params,
        )
        return [dict(r) for r in cur.fetchall()]


def search_memory(
    workspace_id: int,
    query_vec: Any,
    project_id: int | None = None,
    memory_type: str | None = None,
    limit: int = 10,
) -> list[dict]:
    conditions = ["active = true", "workspace_id = %s", "embedding IS NOT NULL"]
    params: list[Any] = [workspace_id]

    if project_id is not None:
        conditions.append("project_id = %s")
        params.append(project_id)
    if memory_type:
        conditions.append("memory_type = %s")
        params.append(memory_type)

    where = " AND ".join(conditions)

    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT id, name, memory_type, LEFT(body, 0) AS body,
                   1 - (embedding <=> %s::vector) AS score,
                   created_at, updated_at
            FROM memories
            WHERE {where}
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            [query_vec] + params + [query_vec, min(limit, 50)],
        )
        return [dict(r) for r in cur.fetchall()]


def search_memory_with_body(
    workspace_id: int,
    query_vec: Any,
    project_id: int | None = None,
    memory_type: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """Same as `search_memory` but returns full body text."""
    conditions = ["active = true", "workspace_id = %s", "embedding IS NOT NULL"]
    params: list[Any] = [workspace_id]

    if project_id is not None:
        conditions.append("project_id = %s")
        params.append(project_id)
    if memory_type:
        conditions.append("memory_type = %s")
        params.append(memory_type)

    where = " AND ".join(conditions)

    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT id, name, memory_type, body,
                   1 - (embedding <=> %s::vector) AS score,
                   created_at, updated_at
            FROM memories
            WHERE {where}
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            [query_vec] + params + [query_vec, min(limit, 50)],
        )
        return [dict(r) for r in cur.fetchall()]


def delete_memory(workspace_id: int, project_id: int, name: str) -> dict | None:
    """Soft-delete a memory (sets active=false). Returns the deleted row or None."""
    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            UPDATE memories SET active = false
            WHERE project_id = %s AND workspace_id = %s AND name = %s AND active = true
            RETURNING id
            """,
            (project_id, workspace_id, name),
        )
        row = cur.fetchone()
        if row is None:
            return None

        write_change(
            conn,
            kind=_KIND,
            workspace_id=workspace_id,
            project_id=project_id,
            identifier=name,
            event="deleted",
            payload={"id": row["id"]},
        )
        conn.commit()
        return dict(row)


# ---------------------------------------------------------------------------
# Reflections spike helpers (Phase 3.6, 5)
# ---------------------------------------------------------------------------


def find_reflections(
    workspace_id: int,
    project_id: int | None = None,
    query_vec: Any | None = None,
    limit: int = 10,
    include_body: bool = False,
) -> list[dict]:
    """Find reflection-type memories.

    When `include_body=False` (default), the `body` column is returned as an
    empty string (cheap projection). When `include_body=True`, full body is
    returned. Pass `query_vec` for embedding-similarity ordering; otherwise
    rows are ordered by `updated_at DESC`.
    """
    conditions = ["active = true", "workspace_id = %s", "memory_type = 'reflection'"]
    params: list[Any] = [workspace_id]

    if project_id is not None:
        conditions.append("project_id = %s")
        params.append(project_id)

    if query_vec is not None:
        conditions.append("embedding IS NOT NULL")
        where = " AND ".join(conditions)
        order_clause = "ORDER BY embedding <=> %s::vector"
        select_score = ", 1 - (embedding <=> %s::vector) AS score"
        params_with_vec = [query_vec] + params + [query_vec, min(limit, 50)]
    else:
        where = " AND ".join(conditions)
        order_clause = "ORDER BY updated_at DESC"
        select_score = ""
        params_with_vec = params + [min(limit, 50)]

    body_expr = "body" if include_body else "LEFT(body, 0) AS body"

    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT id, name, {body_expr}, attributes,
                   created_at, updated_at{select_score}
            FROM memories
            WHERE {where}
            {order_clause}
            LIMIT %s
            """,
            params_with_vec,
        )
        return [dict(r) for r in cur.fetchall()]


def find_reflections_with_body(
    workspace_id: int,
    project_id: int | None = None,
    query_vec: Any | None = None,
    limit: int = 10,
) -> list[dict]:
    """Deprecated thin wrapper. Use `find_reflections(..., include_body=True)`."""
    return find_reflections(
        workspace_id=workspace_id,
        project_id=project_id,
        query_vec=query_vec,
        limit=limit,
        include_body=True,
    )


def extract_gaps(workspace_id: int, project_id: int, name: str) -> dict:
    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT id, name, body, attributes FROM memories "
            "WHERE project_id = %s AND workspace_id = %s AND name = %s "
            "AND active = true AND memory_type = 'reflection'",
            (project_id, workspace_id, name),
        )
        row = cur.fetchone()

    if row is None:
        return {"error": f"Reflection '{name}' not found"}

    body = row["body"]
    section_match = _GAP_SECTION_RE.search(body)
    if section_match is None:
        return {"error": f"No 'Gaps to flag' section found in '{name}'"}

    section_text = section_match.group(1)
    gaps = _GAP_LINE_RE.findall(section_text)
    if not gaps:
        return {"error": f"No structured gap entries found in '{name}'"}

    already_filed = set((row.get("attributes") or {}).get("gaps_filed_as", []))
    return {
        "name": name,
        "gaps": [
            {
                "identifier": identifier,
                "description": description.strip(),
                "already_filed": identifier in already_filed,
            }
            for identifier, description in gaps
        ],
    }


def mark_gaps_filed(
    workspace_id: int, project_id: int, name: str, filed_identifiers: list[str]
) -> dict:
    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT id, attributes FROM memories "
            "WHERE project_id = %s AND workspace_id = %s AND name = %s "
            "AND active = true AND memory_type = 'reflection'",
            (project_id, workspace_id, name),
        )
        row = cur.fetchone()
        if row is None:
            return {"error": f"Reflection '{name}' not found"}

        attrs = dict(row.get("attributes") or {})
        existing = set(attrs.get("gaps_filed_as", []))
        existing.update(filed_identifiers)
        attrs["gaps_filed_as"] = sorted(existing)

        cur.execute(
            "UPDATE memories SET attributes = %s WHERE id = %s",
            (psycopg.types.json.Jsonb(attrs), row["id"]),
        )
        write_change(
            conn,
            kind=_KIND,
            workspace_id=workspace_id,
            project_id=project_id,
            identifier=name,
            event="updated",
            payload={"gaps_filed_as": attrs["gaps_filed_as"]},
        )
        conn.commit()

    return {"name": name, "gaps_filed_as": sorted(existing)}
