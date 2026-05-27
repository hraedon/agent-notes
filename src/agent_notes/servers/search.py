"""Cross-kind search MCP server (Phase 4 / §6).

Uses model layer extracted into `agent_notes.core.search` (Plan 004 Phase 9a).
"""

from __future__ import annotations

from agent_notes.core.db import list_projects, list_workspaces
from agent_notes.core.search import search_all_notes as model_search_all
from agent_notes.core.search import trace_graph_all as model_trace_graph_all
from agent_notes.core.server import Server


class SearchServer(Server):
    """MCP server exposing cross-kind search tools."""

    name = "agent-notes-search"
    version = "0.1.0"

    def __init__(self) -> None:
        super().__init__()
        self._register_search_tools()

    # ------------------------------------------------------------------
    # Tool registration
    # ------------------------------------------------------------------

    def _register_search_tools(self) -> None:
        self.register_tool(
            "search_all_notes",
            {
                "description": (
                    "Semantic search across all note kinds (breadcrumbs, memories). "
                    "Returns kind, identifier, title, and similarity score."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Natural language search query"},
                        "kinds": {"type": "array", "items": {"type": "string"}},
                        "workspaces": {"type": "array", "items": {"type": "string"}},
                        "projects": {"type": "array", "items": {"type": "string"}},
                        "since": {"type": "string", "description": "ISO timestamp"},
                        "limit": {"type": "integer", "default": 20},
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
                    "Cross-kind link graph traversal, slower than kind-local trace_graph."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "from_kind": {"type": "string", "description": "Kind of the starting node"},
                        "workspace": {"type": "string"},
                        "project": {"type": "string"},
                        "identifier": {"type": "string"},
                        "direction": {
                            "type": "string",
                            "enum": ["dependents", "dependencies"],
                            "default": "dependencies",
                        },
                        "max_depth": {"type": "integer", "default": 3},
                        "relationship_kinds": {"type": "array", "items": {"type": "string"}},
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

        vec = embed(query, task="query")

        workspace_ids = None
        if workspace_slugs:
            all_ws = {w.slug: w.id for w in list_workspaces()}
            workspace_ids = [all_ws[s] for s in workspace_slugs if s in all_ws]
            if not workspace_ids:
                return "No matching notes found across kinds."

        project_ids = None
        if project_slugs:
            all_projects = list_projects()
            if workspace_ids:
                ws_ids = set(workspace_ids)
                all_projects = [p for p in all_projects if p.workspace_id in ws_ids]
            slug_to_id = {p.slug: p.id for p in all_projects}
            project_ids = [slug_to_id[s] for s in project_slugs if s in slug_to_id]
            if not project_ids:
                return "No matching notes found across kinds."

        rows = model_search_all(
            query_vec=vec.tolist(),
            kinds=kinds,
            workspace_ids=workspace_ids,
            project_ids=project_ids,
            since_str=since_str,
            limit=limit,
        )

        if not rows:
            return "No matching notes found across kinds."

        lines = [f"{len(rows)} note(s) matched across kinds:"]
        for r in rows:
            updated = r["updated_at"].strftime("%Y-%m-%d %H:%M") if r.get("updated_at") else "?"
            kind = r["kind"]
            ident = r["identifier"]
            title = r["title"]
            score = r["score"]
            lines.append(f"- [{kind}] **{ident}** — {title} (score={score:.3f}, updated={updated})")
        return "\n".join(lines)

    def _tool_trace_graph_all(self, args: dict) -> str:
        from_kind = args["from_kind"]
        workspace_slug = args["workspace"]
        project_slug = args["project"]
        identifier = args["identifier"]
        direction = args.get("direction", "dependencies")
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

        nodes = model_trace_graph_all(
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
