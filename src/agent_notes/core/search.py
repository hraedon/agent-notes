"""Cross-kind search model layer (Phase 4, Plan 004 Phase 9a).

Pulled from `agent_notes.servers.search` during Plan 004 Phase 9a (decision 50).
Presentation-agnostic functions shared by CLI and legacy MCP server.
"""

from __future__ import annotations

from typing import Any, Literal

from psycopg.rows import dict_row

from agent_notes.core.db import _conn
from agent_notes.core.links import LinkedNode


def search_all_notes(
    query_vec: Any,
    kinds: list[str] | None = None,
    workspace_ids: list[int] | None = None,
    project_ids: list[int] | None = None,
    since_str: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Cross-kind semantic search via all_notes_search_v."""
    conditions: list[str] = ["embedding IS NOT NULL"]
    params: list[Any] = []

    if kinds:
        placeholders = ", ".join(["%s"] * len(kinds))
        conditions.append(f"kind IN ({placeholders})")
        params.extend(kinds)

    if workspace_ids:
        placeholders = ", ".join(["%s"] * len(workspace_ids))
        conditions.append(f"workspace_id IN ({placeholders})")
        params.extend(workspace_ids)

    if project_ids:
        placeholders = ", ".join(["%s"] * len(project_ids))
        conditions.append(f"project_id IN ({placeholders})")
        params.extend(project_ids)

    if since_str:
        conditions.append("updated_at >= %s")
        params.append(since_str)

    where = " AND ".join(conditions)

    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT kind, workspace_id, project_id, identifier, title,
                   1 - (embedding <=> %s::vector) AS score,
                   updated_at
            FROM all_notes_search_v
            WHERE {where}
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            [query_vec] + params + [query_vec, min(limit, 100)],
        )
        return [dict(r) for r in cur.fetchall()]


def trace_graph_all(
    kind: str,
    workspace: int,
    project: int,
    identifier: str,
    direction: Literal["dependents", "dependencies"] = "dependencies",
    max_depth: int = 3,
    relationship_kinds: list[str] | None = None,
) -> list[LinkedNode]:
    """Cross-kind recursive CTE traversal with title enrichment."""
    if direction == "dependencies":
        start_col = ("from_kind", "from_workspace", "from_project", "from_identifier")
        next_col = ("to_kind", "to_workspace", "to_project", "to_identifier")
    else:
        start_col = ("to_kind", "to_workspace", "to_project", "to_identifier")
        next_col = ("from_kind", "from_workspace", "from_project", "from_identifier")

    (sk, sw, sp, si) = start_col
    (nk, nw, np_, ni) = next_col

    rel_filter = ""
    rel_params: list[Any] = []
    if relationship_kinds:
        placeholders = ", ".join(["%s"] * len(relationship_kinds))
        rel_filter = f"AND l.relationship IN ({placeholders})"
        rel_params = list(relationship_kinds)

    params: list[Any] = [
        kind,
        workspace,
        project,
        identifier,
        *rel_params,
        max_depth,
        *rel_params,
    ]

    sql = f"""
        WITH RECURSIVE graph AS (
            SELECT
                l.{nk}  AS node_kind,
                l.{nw}  AS node_workspace,
                l.{np_} AS node_project,
                l.{ni}  AS node_id,
                l.relationship,
                1        AS depth
            FROM links l
            WHERE l.{sk} = %s
              AND l.{sw} = %s
              AND l.{sp} = %s
              AND l.{si} = %s
              {rel_filter}

            UNION ALL

            SELECT
                l.{nk},
                l.{nw},
                l.{np_},
                l.{ni},
                l.relationship,
                g.depth + 1
            FROM links l
            JOIN graph g
              ON l.{sk} = g.node_kind
             AND l.{sw} = g.node_workspace
             AND l.{sp} = g.node_project
             AND l.{si} = g.node_id
            WHERE g.depth < %s
              {rel_filter}
        )
        SELECT DISTINCT ON (node_kind, node_workspace, node_project, node_id, relationship)
            g.node_kind, g.node_workspace, g.node_project, g.node_id,
            g.relationship, g.depth,
            kd.title, kd.kind_status
        FROM graph g
        LEFT JOIN LATERAL (
            SELECT b.title, b.status AS kind_status
            FROM breadcrumbs b
            WHERE b.project_id = g.node_project AND b.identifier = g.node_id
            UNION ALL
            SELECT m.name AS title, m.memory_type AS kind_status
            FROM memories m
            WHERE m.project_id = g.node_project
              AND m.name = g.node_id AND m.active = true
        ) kd ON true
        ORDER BY node_kind, node_workspace, node_project, node_id, relationship, depth
    """

    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(sql, params)
        rows = cur.fetchall()

    return [
        LinkedNode(
            kind=r["node_kind"],
            workspace_id=r["node_workspace"],
            project_id=r["node_project"],
            identifier=r["node_id"],
            relationship=r["relationship"],
            depth=r["depth"],
            title=r.get("title"),
            status=r.get("kind_status"),
        )
        for r in rows
    ]
