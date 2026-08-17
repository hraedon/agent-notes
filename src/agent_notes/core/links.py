"""Generic cross-kind link table operations (decisions 10, 14, 16).

Public surface:
- `add_link`     — idempotent INSERT with change_log row.
- `remove_link`  — DELETE with change_log row; returns True if deleted.
- `trace_graph`  — kind-local recursive CTE traversal (decision 10).

`trace_graph` is kind-local by design. Cross-kind traversal (`trace_graph_all`)
lives in the search server (Phase 4), which uses LATERAL UNION ALL against
per-kind row sources (decision 10 / §6).

Index hint: queries hit `idx_links_from` / `idx_links_to`, which order
node-identifier columns BEFORE `relationship` (Kimi round-3 #3 / §4.1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from psycopg.rows import dict_row

# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


@dataclass
class LinkedNode:
    kind: str
    workspace_id: int
    project_id: int
    identifier: str
    relationship: str
    depth: int
    title: str | None = None
    status: str | None = None
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _conn():
    from agent_notes.core.db import _conn as _db_conn

    return _db_conn()


def _resolved_actor(actor: str | None) -> str:
    """The actor to stamp on a link change_log row (WI-069).

    Callers that already resolved (and gated) an identity pass it through —
    e.g. ``add_cross_project_link`` passes the lineage-gated actor. Everyone
    else (the plain ``link add``/``link remove`` CLI) gets the env-resolved
    default actor, so the audit row is attributed instead of NULL. Resolution
    only — no lineage gate here: links span every kind (memories, breadcrumbs),
    not just the gated work-item verbs.
    """
    if actor is not None:
        return actor
    from agent_notes.core.face_factory import default_actor

    resolved: str = default_actor().actor_id
    return resolved


# ---------------------------------------------------------------------------
# add_link / remove_link
# ---------------------------------------------------------------------------


def add_link(
    from_kind: str,
    from_workspace: int,
    from_project: int,
    from_identifier: str,
    to_kind: str,
    to_workspace: int,
    to_project: int,
    to_identifier: str,
    relationship: str,
    actor: str | None = None,
) -> None:
    """Idempotent INSERT into links; writes a change_log row (decision 20).

    The change_log row is attributed to *actor* (or the env-resolved default
    when None) instead of NULL (WI-069).
    """
    from agent_notes.core.change_log import write_change

    resolved_actor = _resolved_actor(actor)
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO links
                (from_kind, from_workspace, from_project, from_identifier,
                 to_kind,   to_workspace,   to_project,   to_identifier, relationship)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (
                from_kind,
                from_workspace,
                from_project,
                from_identifier,
                to_kind,
                to_workspace,
                to_project,
                to_identifier,
                relationship,
            ),
        )
        if cur.rowcount > 0:
            write_change(
                conn,
                kind=from_kind,
                workspace_id=from_workspace,
                project_id=from_project,
                identifier=from_identifier,
                event="link_added",
                payload={
                    "to_kind": to_kind,
                    "to_identifier": to_identifier,
                    "relationship": relationship,
                },
                actor=resolved_actor,
            )
        conn.commit()


def remove_link(
    from_kind: str,
    from_workspace: int,
    from_project: int,
    from_identifier: str,
    to_kind: str,
    to_workspace: int,
    to_project: int,
    to_identifier: str,
    relationship: str,
    actor: str | None = None,
) -> bool:
    """DELETE from links; writes a change_log row. Returns True if deleted.

    The change_log row is attributed to *actor* (or the env-resolved default
    when None) instead of NULL (WI-069).
    """
    from agent_notes.core.change_log import write_change

    resolved_actor = _resolved_actor(actor)
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            DELETE FROM links
            WHERE from_kind = %s AND from_workspace = %s AND from_project = %s
              AND from_identifier = %s AND to_kind = %s AND to_workspace = %s
              AND to_project = %s AND to_identifier = %s AND relationship = %s
            """,
            (
                from_kind,
                from_workspace,
                from_project,
                from_identifier,
                to_kind,
                to_workspace,
                to_project,
                to_identifier,
                relationship,
            ),
        )
        deleted = cur.rowcount > 0
        if deleted:
            write_change(
                conn,
                kind=from_kind,
                workspace_id=from_workspace,
                project_id=from_project,
                identifier=from_identifier,
                event="link_removed",
                payload={
                    "to_kind": to_kind,
                    "to_identifier": to_identifier,
                    "relationship": relationship,
                },
                actor=resolved_actor,
            )
            conn.commit()
        return deleted


# ---------------------------------------------------------------------------
# trace_graph
# ---------------------------------------------------------------------------


def trace_graph(
    kind: str,
    workspace: int,
    project: int,
    identifier: str,
    direction: Literal["dependents", "dependencies"],
    max_depth: int = 3,
    relationship_kinds: list[str] | None = None,
) -> list[LinkedNode]:
    """Kind-local recursive CTE traversal (decision 10).

    `direction='dependencies'` follows outgoing edges (from → to).
    `direction='dependents'` follows incoming edges (to → from).

    Dangling links are returned as-is (§10 risk register).
    """
    if direction == "dependencies":
        # Follow outgoing edges: anchor is the FROM node.
        start_col = ("from_kind", "from_workspace", "from_project", "from_identifier")
        next_col = ("to_kind", "to_workspace", "to_project", "to_identifier")
    else:
        # Follow incoming edges: anchor is the TO node.
        start_col = ("to_kind", "to_workspace", "to_project", "to_identifier")
        next_col = ("from_kind", "from_workspace", "from_project", "from_identifier")

    (sk, sw, sp, si) = start_col
    (nk, nw, np_, ni) = next_col

    # rel_filter appears twice (anchor + recursive step), so relationship_kinds
    # params must be appended twice to match placeholder count.
    rel_filter = ""
    rel_params: list[Any] = []
    if relationship_kinds:
        placeholders = ", ".join(["%s"] * len(relationship_kinds))
        rel_filter = f"AND l.relationship IN ({placeholders})"
        rel_params = list(relationship_kinds)

    # Parameter order matches SQL placeholder positions:
    # anchor WHERE: kind, workspace, project, identifier + rel_params
    # recursive WHERE: max_depth + rel_params
    params: list[Any] = [kind, workspace, project, identifier, *rel_params, max_depth, *rel_params]

    # Recursive CTE: walk the link graph up to max_depth hops.
    # idx_links_from / idx_links_to are hit by anchoring on the start node
    # columns before filtering by relationship (Kimi round-3 #3).
    sql = f"""
        WITH RECURSIVE graph AS (
            -- Anchor: direct neighbours of the start node.
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

            -- Recursive step: neighbours of current frontier.
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
            node_kind, node_workspace, node_project, node_id, relationship, depth
        FROM graph
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
        )
        for r in rows
    ]
