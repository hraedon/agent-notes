"""Memory MCP server — thin kind server composing core modules (§6, Phase 3).

Uses the model layer extracted into `agent_notes.core.memory_model` (Plan 004 Phase 9a).
"""

from __future__ import annotations

from typing import Any

from agent_notes.core.links import trace_graph as core_trace_graph
from agent_notes.core.memory_model import (
    add_memory as model_add_memory,
)
from agent_notes.core.memory_model import (
    delete_memory as model_delete_memory,
)
from agent_notes.core.memory_model import (
    extract_gaps as model_extract_gaps,
)
from agent_notes.core.memory_model import (
    find_reflections as model_find_reflections,
)
from agent_notes.core.memory_model import (
    get_memory as model_get_memory,
)
from agent_notes.core.memory_model import (
    list_memories as model_list_memories,
)
from agent_notes.core.memory_model import (
    mark_gaps_filed as model_mark_gaps_filed,
)
from agent_notes.core.memory_model import (
    search_memory as model_search_memory,
)
from agent_notes.core.memory_model import (
    search_memory_with_body as model_search_memory_with_body,
)
from agent_notes.core.server import Server

_KIND = "memory"


def _conn():
    from agent_notes.core.db import _conn as _db_conn

    return _db_conn()


class MemoryServer(Server):
    """MCP server exposing the memory kind tool surface."""

    name = "agent-notes-memory"
    version = "0.1.0"

    def __init__(self) -> None:
        super().__init__()
        self._register_memory_tools()
        self._register_resource_handlers()

    def _validate_memory_type(self, workspace_id: int, memory_type: str) -> None:
        from agent_notes.core.db import list_vocabulary

        vocabs = list_vocabulary(workspace_id, kind_namespace="memory_type")
        if not any(v.name == memory_type for v in vocabs):
            raise ValueError(
                f"memory_type '{memory_type}' not found in vocabularies. "
                f"Available: {', '.join(v.name for v in vocabs) or '(none)'}"
            )

    # ------------------------------------------------------------------
    # Resource handlers
    # ------------------------------------------------------------------

    def _register_resource_handlers(self) -> None:
        from agent_notes.core.resources import build_uri, parse_uri

        def _list_fn(workspace_slug: str, project_slug: str) -> list[dict]:
            ws = self._resolve_workspace(workspace_slug)
            proj = self._resolve_project(ws.id, project_slug)
            rows = model_list_memories(workspace_id=ws.id, project_id=proj.id)
            return [
                {
                    "uri": build_uri("memory", workspace_slug, project_slug, r["name"]),
                    "name": r["name"],
                    "mimeType": "text/markdown",
                    "description": f"{r['memory_type']} — {(r.get('body_preview') or '')[:60]}",
                }
                for r in rows
            ]

        def _read_fn(workspace_slug: str, project_slug: str, name: str) -> str:
            ws = self._resolve_workspace(workspace_slug)
            proj = self._resolve_project(ws.id, project_slug)
            row = model_get_memory(ws.id, proj.id, name)
            if row is None:
                raise KeyError(f"Memory {name!r} not found")
            lines = [
                f"**{row['name']}** (type={row['memory_type']}, id={row['id']})",
                f"Created: {row['created_at'].strftime('%Y-%m-%d %H:%M')}",
                f"Updated: {row['updated_at'].strftime('%Y-%m-%d %H:%M')}",
            ]
            if row["supersedes"]:
                lines.append(f"Supersedes: id={row['supersedes']}")
            if row["attributes"]:
                for k, v in row["attributes"].items():
                    lines.append(f"  {k}: {v}")
            lines.append("")
            lines.append(row["body"])
            return "\n".join(lines)

        def _handler(action: str, uri_or_prefix: str) -> Any:
            if action == "list":
                resources: list[dict] = []
                from agent_notes.core.db import list_projects, list_workspaces

                for ws in list_workspaces():
                    for proj in list_projects(workspace_id=ws.id):
                        resources.extend(_list_fn(ws.slug, proj.slug))
                return resources
            if action == "read":
                parsed = parse_uri(uri_or_prefix)
                if parsed.kind != "memory":
                    raise ValueError(f"Expected memory URI, got {parsed.kind}")
                if parsed.project is None or parsed.identifier is None:
                    raise ValueError(f"URI must include project and identifier: {uri_or_prefix!r}")
                return _read_fn(parsed.workspace, parsed.project, parsed.identifier)
            raise ValueError(f"Unknown action: {action!r}")

        self.register_resource_handler("note://memory/", _handler)

    # ------------------------------------------------------------------
    # Tool registration
    # ------------------------------------------------------------------

    def _register_memory_tools(self) -> None:
        self.register_tool(
            "add_memory",
            {
                "description": (
                    "Add a new memory. "
                    "Requires explicit project slug (use 'global' for cross-cutting)."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workspace": {"type": "string"},
                        "project": {"type": "string"},
                        "name": {"type": "string"},
                        "memory_type": {"type": "string"},
                        "body": {"type": "string"},
                        "attributes": {"type": "object"},
                    },
                    "required": ["workspace", "project", "name", "memory_type", "body"],
                },
            },
            self._tool_add_memory,
        )
        self.register_tool(
            "search_memory",
            {
                "description": ("Search memories by semantic similarity. Body elided by default."),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workspace": {"type": "string"},
                        "project": {"type": "string"},
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "default": 10},
                        "include_body": {"type": "boolean", "default": False},
                        "memory_type": {"type": "string"},
                    },
                    "required": ["workspace", "query"],
                },
            },
            self._tool_search_memory,
        )
        self.register_tool(
            "list_memories",
            {
                "description": "List active memories in a project, newest first.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workspace": {"type": "string"},
                        "project": {"type": "string"},
                        "memory_type": {"type": "string"},
                        "limit": {"type": "integer", "default": 50},
                    },
                    "required": ["workspace", "project"],
                },
            },
            self._tool_list_memories,
        )
        self.register_tool(
            "get_memory",
            {
                "description": "Get a single memory by name (active only).",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workspace": {"type": "string"},
                        "project": {"type": "string"},
                        "name": {"type": "string"},
                    },
                    "required": ["workspace", "project", "name"],
                },
            },
            self._tool_get_memory,
        )
        self.register_tool(
            "delete_memory",
            {
                "description": "Soft-delete a memory (sets active=false).",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workspace": {"type": "string"},
                        "project": {"type": "string"},
                        "name": {"type": "string"},
                    },
                    "required": ["workspace", "project", "name"],
                },
            },
            self._tool_delete_memory,
        )
        self.register_tool(
            "trace_graph",
            {
                "description": "Kind-local link graph traversal for memories.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workspace": {"type": "string"},
                        "project": {"type": "string"},
                        "name": {"type": "string"},
                        "direction": {
                            "type": "string",
                            "enum": ["dependents", "dependencies"],
                            "default": "dependencies",
                        },
                        "max_depth": {"type": "integer", "default": 3},
                        "relationship_kinds": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["workspace", "project", "name"],
                },
            },
            self._tool_trace_graph,
        )
        self.register_tool(
            "find_reflections",
            {
                "description": "Find reflection memories (memory_type='reflection').",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workspace": {"type": "string"},
                        "project": {"type": "string"},
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "default": 10},
                        "include_body": {"type": "boolean", "default": False},
                    },
                    "required": ["workspace"],
                },
            },
            self._tool_find_reflections,
        )
        self.register_tool(
            "extract_gaps",
            {
                "description": "Parse 'Gaps to flag' from a reflection memory.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workspace": {"type": "string"},
                        "project": {"type": "string"},
                        "name": {"type": "string"},
                    },
                    "required": ["workspace", "project", "name"],
                },
            },
            self._tool_extract_gaps,
        )
        self.register_tool(
            "mark_gaps_filed",
            {
                "description": "Mark specific gaps from a reflection as filed.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workspace": {"type": "string"},
                        "project": {"type": "string"},
                        "name": {"type": "string"},
                        "filed_identifiers": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["workspace", "project", "name", "filed_identifiers"],
                },
            },
            self._tool_mark_gaps_filed,
        )

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    def _tool_add_memory(self, args: dict) -> str:
        from agent_notes.core.embed import embed

        workspace_slug = args["workspace"]
        project_slug = args["project"]
        name = args["name"]
        memory_type = args["memory_type"]
        body = args["body"]
        attributes = args.get("attributes") or {}

        ws = self._resolve_workspace(workspace_slug)
        self._validate_memory_type(ws.id, memory_type)
        proj = self._resolve_project(ws.id, project_slug)

        vec = embed(body, task="document")
        row = model_add_memory(
            workspace_id=ws.id,
            project_id=proj.id,
            name=name,
            memory_type=memory_type,
            body=body,
            attributes=attributes,
            embedding=vec.tolist(),
        )

        old_id = row.get("supersedes")
        superseded_msg = f" (superseded id={old_id})" if old_id else ""
        return (
            f"Memory '{name}' added "
            f"(id={row['id']}, type={memory_type}, project={project_slug})"
            f"{superseded_msg}"
        )

    def _tool_search_memory(self, args: dict) -> str:
        from agent_notes.core.embed import embed

        workspace_slug = args["workspace"]
        project_slug = args.get("project")
        query = args["query"]
        limit = min(int(args.get("limit", 10)), 50)
        include_body = bool(args.get("include_body", False))
        memory_type = args.get("memory_type")

        ws = self._resolve_workspace(workspace_slug)
        vec = embed(query, task="query")

        proj_id = self._resolve_project(ws.id, project_slug).id if project_slug else None

        rows = (
            model_search_memory_with_body(ws.id, vec.tolist(), proj_id, memory_type, limit)
            if include_body
            else model_search_memory(ws.id, vec.tolist(), proj_id, memory_type, limit)
        )

        if not rows:
            return "No matching memories found."

        lines = [f"{len(rows)} memory(ies) matched:"]
        for r in rows:
            line = f"- **{r['name']}** (type={r['memory_type']}, score={r['score']:.3f})"
            if include_body and r.get("body"):
                line += f"\n  {r['body'][:200]}"
            lines.append(line)
        return "\n".join(lines)

    def _tool_list_memories(self, args: dict) -> str:
        workspace_slug = args["workspace"]
        project_slug = args["project"]
        memory_type = args.get("memory_type")
        limit = min(int(args.get("limit", 50)), 200)

        ws = self._resolve_workspace(workspace_slug)
        proj = self._resolve_project(ws.id, project_slug)

        rows = model_list_memories(
            workspace_id=ws.id, project_id=proj.id, memory_type=memory_type, limit=limit
        )

        if not rows:
            return f"No memories in project '{project_slug}'."

        lines = [f"{len(rows)} memory(ies) in {project_slug}:"]
        for r in rows:
            preview = r["body_preview"].replace("\n", " ")[:80] if r.get("body_preview") else ""
            lines.append(f"- **{r['name']}** (type={r['memory_type']}) {preview}")
        return "\n".join(lines)

    def _tool_get_memory(self, args: dict) -> str:
        workspace_slug = args["workspace"]
        project_slug = args["project"]
        name = args["name"]

        ws = self._resolve_workspace(workspace_slug)
        proj = self._resolve_project(ws.id, project_slug)

        row = model_get_memory(ws.id, proj.id, name)
        if row is None:
            return f"Memory '{name}' not found in project '{project_slug}'."

        lines = [
            f"**{row['name']}** (type={row['memory_type']}, id={row['id']})",
            f"Created: {row['created_at'].strftime('%Y-%m-%d %H:%M')}",
            f"Updated: {row['updated_at'].strftime('%Y-%m-%d %H:%M')}",
        ]
        if row.get("supersedes"):
            lines.append(f"Supersedes: id={row['supersedes']}")
        if row.get("attributes"):
            for k, v in row["attributes"].items():
                lines.append(f"  {k}: {v}")
        lines.append("")
        lines.append(row["body"])
        return "\n".join(lines)

    def _tool_delete_memory(self, args: dict) -> str:
        workspace_slug = args["workspace"]
        project_slug = args["project"]
        name = args["name"]

        ws = self._resolve_workspace(workspace_slug)
        proj = self._resolve_project(ws.id, project_slug)

        row = model_delete_memory(ws.id, proj.id, name)
        if row is None:
            return f"Memory '{name}' not found (or already deleted) in project '{project_slug}'."

        return f"Memory '{name}' soft-deleted (id={row['id']})."

    def _tool_trace_graph(self, args: dict) -> str:
        ws = self._resolve_workspace(args["workspace"])
        proj = self._resolve_project(ws.id, args["project"])
        nodes = core_trace_graph(
            kind=_KIND,
            workspace=ws.id,
            project=proj.id,
            identifier=args["name"],
            direction=args.get("direction", "dependencies"),
            max_depth=min(int(args.get("max_depth", 3)), 10),
            relationship_kinds=args.get("relationship_kinds"),
        )
        if not nodes:
            return (
                "No linked notes found for memory "
                f"'{args['name']}' ({args.get('direction', 'dependencies')})."
            )
        lines = [f"{len(nodes)} linked note(s) for '{args['name']}':"]
        for n in nodes:
            lines.append(f"- {n.identifier} (relationship={n.relationship}, depth={n.depth})")
        return "\n".join(lines)

    def _tool_find_reflections(self, args: dict) -> str:
        from agent_notes.core.embed import embed

        ws = self._resolve_workspace(args["workspace"])
        project_slug = args.get("project")
        query = args.get("query")
        limit = min(int(args.get("limit", 10)), 50)
        include_body = bool(args.get("include_body", False))

        proj_id = None
        if project_slug:
            proj = self._resolve_project(ws.id, project_slug)
            proj_id = proj.id

        query_vec = embed(query, task="query").tolist() if query else None
        rows = model_find_reflections(
            workspace_id=ws.id,
            project_id=proj_id,
            query_vec=query_vec,
            limit=limit,
            include_body=include_body,
        )

        if not rows:
            return "No reflection memories found."

        lines = [f"{len(rows)} reflection(s) found:"]
        for r in rows:
            score_str = f", score={r['score']:.3f}" if "score" in r else ""
            line = f"- **{r['name']}**{score_str}"
            gaps_filed = (r.get("attributes") or {}).get("gaps_filed_as", [])
            if gaps_filed:
                line += f" (gaps_filed: {', '.join(gaps_filed)})"
            if include_body and r.get("body"):
                line += f"\n  {r['body'][:200]}"
            lines.append(line)
        return "\n".join(lines)

    def _tool_extract_gaps(self, args: dict) -> str:
        ws = self._resolve_workspace(args["workspace"])
        proj = self._resolve_project(ws.id, args["project"])
        result = model_extract_gaps(ws.id, proj.id, args["name"])
        if result.get("error"):
            return result["error"]

        lines = [f"{len(result['gaps'])} gap(s) extracted from '{args['name']}':"]
        for gap in result["gaps"]:
            status = " [FILED]" if gap["already_filed"] else ""
            lines.append(f"- **{gap['identifier']}**{status}: {gap['description']}")
        lines.append("")
        lines.append(
            "Propose filing as breadcrumbs by calling file_breadcrumb for each unfilled gap."
        )
        return "\n".join(lines)

    def _tool_mark_gaps_filed(self, args: dict) -> str:
        ws = self._resolve_workspace(args["workspace"])
        proj = self._resolve_project(ws.id, args["project"])
        result = model_mark_gaps_filed(ws.id, proj.id, args["name"], args["filed_identifiers"])
        if result.get("error"):
            return result["error"]
        return (
            f"Marked {len(args['filed_identifiers'])} gap(s) as filed in "
            f"'{args['name']}': {', '.join(args['filed_identifiers'])}"
        )
