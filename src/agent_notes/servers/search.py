"""Cross-kind search MCP server (Phase 4 / §6).

Tools: search_all_notes, trace_graph_all.
Inherits from core: list_workspaces, list_projects, list_vocabulary,
       archive_vocabulary, changes_since, history, add_link, remove_link.

Design decisions:
- search_all_notes queries the all_notes_search_v view (UNION ALL across
  breadcrumbs + memories) ranked by cosine similarity. Returns kind,
  identifier, title, score; clients fetch full bodies through kind-specific
  tools (Kimi D — search-only scope).
- trace_graph_all performs cross-kind link traversal via the same recursive
  CTE as core trace_graph, but enriches each discovered node with title/status
  from the actual kind tables (decision 10 / §6). Acknowledged-slower path;
  callers explicitly opt in.
- Uses updated_at consistently for the since filter (both tables have it).
- Embedding call precedes DB transaction (decision 26).
"""

from __future__ import annotations

from typing import Any, Literal

from psycopg.rows import dict_row

from agent_notes.core.db import _conn
from agent_notes.core.links import LinkedNode
from agent_notes.core.server import Server


class SearchServer(Server):
    """MCP server exposing cross-kind search tools."""

    name = "agent-notes-search"
    version = "0.1.0"

    def __init__(self) -> None:
        super().__init__()
        self._register_search_tools()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_workspace(self, slug: str):
        from agent_notes.core.db import list_workspaces

        ws = next((w for w in list_workspaces() if w.slug == slug), None)
        if ws is None:
            raise ValueError(f"workspace '{slug}' not found")
        return ws

    def _resolve_project(self, workspace_id: int, slug: str):
        from agent_notes.core.db import list_projects

        p = next(
            (p for p in list_projects(workspace_id=workspace_id) if p.slug == slug),
            None,
        )
        if p is None:
            raise ValueError(f"project '{slug}' not found")
        return p

    # ------------------------------------------------------------------
    # Tool registration
    # ------------------------------------------------------------------

    def _register_search_tools(self) -> None:
        self.register_tool(
            "search_all_notes",
            {
                "description": (
                    "Semantic search across all note kinds (breadcrumbs, memories). "
                    "Returns kind, identifier, title, and similarity score. "
                    "Use kind-specific tools to fetch full bodies."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Natural language search query",
                        },
                        "kinds": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Filter to these kinds (e.g. ['breadcrumb', 'memory'])",
                        },
                        "workspaces": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Filter to these workspace slugs",
                        },
                        "projects": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Filter to these project slugs",
                        },
                        "since": {
                            "type": "string",
                            "description": "ISO timestamp; only notes updated after this time",
                        },
                        "limit": {
                            "type": "integer",
                            "default": 20,
                            "description": "Max results (default 20, max 100)",
                        },
                    },
                    "required": ["query"],
                },
            },
            self._tool_search_all_notes,
        )
        self.register_tool(
            "trace_graph_all",
            {
                "description": (
                    "Cross-kind link graph traversal (decision 10). Follows "
                    "relationships across breadcrumbs, memories, and future kinds. "
                    "Slower than kind-local trace_graph; isolated here so callers "
                    "explicitly opt in."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "from_kind": {
                            "type": "string",
                            "description": "Kind of the starting node",
                        },
                        "workspace": {"type": "string"},
                        "project": {"type": "string"},
                        "identifier": {
                            "type": "string",
                            "description": "Identifier of the starting node",
                        },
                        "direction": {
                            "type": "string",
                            "enum": ["dependents", "dependencies"],
                            "default": "dependencies",
                        },
                        "max_depth": {"type": "integer", "default": 3},
                        "relationship_kinds": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Filter to these relationship types",
                        },
                    },
                    "required": ["from_kind", "workspace", "project", "identifier"],
                },
            },
            self._tool_trace_graph_all,
        )

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    def _tool_search_all_notes(self, args: dict) -> str:
        from agent_notes.core.embed import embed

        query = args["query"]
        kinds = args.get("kinds")
        workspace_slugs = args.get("workspaces")
        project_slugs = args.get("projects")
        since_str = args.get("since")
        limit = min(int(args.get("limit", 20)), 100)

        # Decision 26: embed BEFORE transaction.
        vec = embed(query, task="query")

        # Build filters.
        conditions: list[str] = ["embedding IS NOT NULL"]
        params: list[Any] = []

        if kinds:
            placeholders = ", ".join(["%s"] * len(kinds))
            conditions.append(f"kind IN ({placeholders})")
            params.extend(kinds)

        if workspace_slugs:
            workspace_ids = self._resolve_workspace_ids(workspace_slugs)
            if workspace_ids:
                placeholders = ", ".join(["%s"] * len(workspace_ids))
                conditions.append(f"workspace_id IN ({placeholders})")
                params.extend(workspace_ids)
            else:
                conditions.append("1 = 0")

        if project_slugs:
            project_ids = self._resolve_project_ids(workspace_slugs, project_slugs)
            if project_ids:
                placeholders = ", ".join(["%s"] * len(project_ids))
                conditions.append(f"project_id IN ({placeholders})")
                params.extend(project_ids)
            else:
                conditions.append("1 = 0")

        if since_str:
            conditions.append("updated_at >= %s")
            params.append(since_str)

        where = " AND ".join(conditions)

        # Query the view with cosine similarity ranking.
        # Placeholder order: vec for distance, then all filter params, then vec again for ORDER BY.
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
                [vec.tolist()] + params + [vec.tolist(), limit],
            )
            rows = cur.fetchall()

        if not rows:
            return "No matching notes found across kinds."

        lines = [f"{len(rows)} note(s) matched across kinds:"]
        for r in rows:
            updated = r["updated_at"].strftime("%Y-%m-%d %H:%M") if r.get("updated_at") else "?"
            lines.append(
                f"- [{r['kind']}] **{r['identifier']}** — {r['title']} "
                f"(score={r['score']:.3f}, updated={updated})"
            )
        return "\n".join(lines)

    def _tool_trace_graph_all(self, args: dict) -> str:
        from agent_notes.core.db import list_projects, list_workspaces

        from_kind = args["from_kind"]
        workspace_slug = args["workspace"]
        project_slug = args["project"]
        identifier = args["identifier"]
        direction: Literal["dependents", "dependencies"] = args.get("direction", "dependencies")
        max_depth = min(int(args.get("max_depth", 3)), 10)
        rel_kinds = args.get("relationship_kinds")

        ws = next((w for w in list_workspaces() if w.slug == workspace_slug), None)
        if ws is None:
            return f"Error: workspace '{workspace_slug}' not found"
        proj = next(
            (p for p in list_projects(workspace_id=ws.id) if p.slug == project_slug),
            None,
        )
        if proj is None:
            return f"Error: project '{project_slug}' not found"

        nodes = _trace_graph_all(
            kind=from_kind,
            workspace=ws.id,
            project=proj.id,
            identifier=identifier,
            direction=direction,
            max_depth=max_depth,
            relationship_kinds=rel_kinds,
        )

        if not nodes:
            return f"No linked notes found for {from_kind}/{identifier} ({direction})."

        lines = [f"{len(nodes)} linked note(s) for {from_kind}/{identifier} ({direction}):"]
        for n in nodes:
            title_str = f" — {n.title}" if n.title else ""
            lines.append(
                f"- [{n.kind}] {n.identifier}{title_str} "
                f"(relationship={n.relationship}, depth={n.depth})"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Filter helpers
    # ------------------------------------------------------------------

    def _resolve_workspace_ids(self, slugs: list[str]) -> list[int]:
        from agent_notes.core.db import list_workspaces

        all_ws = {w.slug: w.id for w in list_workspaces()}
        return [all_ws[s] for s in slugs if s in all_ws]

    def _resolve_project_ids(
        self,
        workspace_slugs: list[str] | None,
        project_slugs: list[str],
    ) -> list[int]:
        from agent_notes.core.db import list_projects

        all_projects = list_projects()
        if workspace_slugs:
            ws_ids = set(self._resolve_workspace_ids(workspace_slugs))
            all_projects = [p for p in all_projects if p.workspace_id in ws_ids]
        slug_to_id = {p.slug: p.id for p in all_projects}
        return [slug_to_id[s] for s in project_slugs if s in slug_to_id]


# ---------------------------------------------------------------------------
# Cross-kind trace_graph implementation (decision 10)
# ---------------------------------------------------------------------------


def _trace_graph_all(
    kind: str,
    workspace: int,
    project: int,
    identifier: str,
    direction: Literal["dependents", "dependencies"],
    max_depth: int = 3,
    relationship_kinds: list[str] | None = None,
) -> list[LinkedNode]:
    """Cross-kind recursive CTE traversal with title enrichment.

    Same recursive CTE as core trace_graph (which already follows cross-kind
    links in the recursive step), but enriches each discovered node with
    title/status from the actual kind tables using LATERAL (decision 10).
    """
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

    # Recursive CTE over the links table; then LEFT JOIN LATERAL to
    # per-kind tables to enrich with title and kind-specific metadata.
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
