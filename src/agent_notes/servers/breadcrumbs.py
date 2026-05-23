"""Breadcrumbs MCP server — thin kind server composing core modules (Phase 2a).

Tools: file_breadcrumb, update_breadcrumb, query_breadcrumbs, find_breadcrumbs,
       get_breadcrumb, suggest_duplicates, diagnose, trace_graph (kind-local),
       render_index.
Inherits from core: list_workspaces, list_projects, list_vocabulary,
       archive_vocabulary, changes_since, history, add_link, remove_link.

Decision 22 (sync) and 26 (embed before txn) apply throughout.
"""

from __future__ import annotations

from typing import Any

from agent_notes.core.db import list_projects, list_workspaces
from agent_notes.core.links import trace_graph as core_trace_graph
from agent_notes.core.server import Server

from .breadcrumbs_model import BreadcrumbModel

_KIND = "breadcrumb"


class BreadcrumbServer(Server):
    """MCP server exposing the breadcrumb kind tool surface."""

    name = "agent-notes-breadcrumbs"
    version = "0.1.0"

    def __init__(self) -> None:
        super().__init__()
        self._register_breadcrumb_tools()
        self._register_resource_handlers()

    def _resolve_workspace(self, slug: str):
        ws = next((w for w in list_workspaces() if w.slug == slug), None)
        if ws is None:
            raise ValueError(f"workspace '{slug}' not found")
        return ws

    def _resolve_project(self, workspace_id: int, slug: str):
        p = next((p for p in list_projects(workspace_id=workspace_id) if p.slug == slug), None)
        if p is None:
            raise ValueError(f"project '{slug}' not found")
        return p

    # ------------------------------------------------------------------
    # Resource handlers (Phase 6.1)
    # ------------------------------------------------------------------

    def _register_resource_handlers(self) -> None:
        from agent_notes.core.resources import build_uri, parse_uri

        def _list_fn(workspace_slug: str, project_slug: str) -> list[dict]:
            ws = self._resolve_workspace(workspace_slug)
            proj = self._resolve_project(ws.id, project_slug)
            rows = BreadcrumbModel.query_breadcrumbs(
                project_id=proj.id,
                limit=1000,
            )
            return [
                {
                    "uri": build_uri("breadcrumb", workspace_slug, project_slug, r["identifier"]),
                    "name": r["identifier"],
                    "mimeType": "text/markdown",
                    "description": f"{r['kind']} / {r['status']} — {r['title']}",
                }
                for r in rows
            ]

        def _read_fn(workspace_slug: str, project_slug: str, identifier: str) -> str:
            ws = self._resolve_workspace(workspace_slug)
            proj = self._resolve_project(ws.id, project_slug)
            row = BreadcrumbModel.get_breadcrumb(proj.id, identifier)
            if row is None:
                raise KeyError(f"Breadcrumb {identifier!r} not found")
            return _format_breadcrumb(row)

        def _handler(action: str, uri_or_prefix: str):
            if action == "list":
                resources: list[dict] = []
                from agent_notes.core.db import list_projects, list_workspaces

                for ws in list_workspaces():
                    for proj in list_projects(workspace_id=ws.id):
                        resources.extend(_list_fn(ws.slug, proj.slug))
                return resources
            if action == "read":
                parsed = parse_uri(uri_or_prefix)
                if parsed.kind != "breadcrumb":
                    raise ValueError(f"Expected breadcrumb URI, got {parsed.kind}")
                if parsed.project is None or parsed.identifier is None:
                    raise ValueError(f"URI must include project and identifier: {uri_or_prefix!r}")
                return _read_fn(parsed.workspace, parsed.project, parsed.identifier)
            raise ValueError(f"Unknown action: {action!r}")

        self.register_resource_handler("note://breadcrumb/", _handler)

    # ------------------------------------------------------------------
    # Tool registration
    # ------------------------------------------------------------------

    def _register_breadcrumb_tools(self) -> None:
        self.register_tool(
            "file_breadcrumb",
            {
                "description": (
                    "File (create or upsert) a breadcrumb. Embedding is computed automatically."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workspace": {"type": "string"},
                        "project": {"type": "string"},
                        "identifier": {"type": "string"},
                        "title": {"type": "string"},
                        "body": {"type": "string", "default": ""},
                        "kind": {
                            "type": "string",
                            "description": "Breadcrumb kind (must exist in bc_kind vocab)",
                        },
                        "status": {
                            "type": "string",
                            "description": "Breadcrumb status (must exist in bc_status vocab)",
                        },
                        "severity": {
                            "type": "string",
                            "default": "medium",
                            "description": "Breadcrumb severity (must exist in bc_severity vocab)",
                        },
                        "external_refs": {"type": "object"},
                        "diagnostic_keys": {"type": "object"},
                    },
                    "required": ["workspace", "project", "title", "kind", "status"],
                },
            },
            self._tool_file_breadcrumb,
        )
        self.register_tool(
            "update_breadcrumb",
            {
                "description": (
                    "Update mutable fields on a breadcrumb. Any None fields are left unchanged."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workspace": {"type": "string"},
                        "project": {"type": "string"},
                        "identifier": {"type": "string"},
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "kind": {"type": "string"},
                        "status": {"type": "string"},
                        "severity": {"type": "string"},
                        "external_refs": {"type": "object"},
                        "diagnostic_keys": {"type": "object"},
                    },
                    "required": ["workspace", "project", "identifier"],
                },
            },
            self._tool_update_breadcrumb,
        )
        self.register_tool(
            "query_breadcrumbs",
            {
                "description": "List breadcrumbs with optional filters.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workspace": {"type": "string"},
                        "project": {"type": "string"},
                        "status": {"type": "string"},
                        "kind": {"type": "string"},
                        "severity": {"type": "string"},
                        "is_open": {"type": "boolean"},
                        "limit": {"type": "integer", "default": 50},
                    },
                    "required": [],
                },
            },
            self._tool_query_breadcrumbs,
        )
        self.register_tool(
            "find_breadcrumbs",
            {
                "description": "Semantic search over breadcrumb embeddings.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workspace": {"type": "string"},
                        "project": {"type": "string"},
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "default": 10},
                    },
                    "required": ["workspace", "query"],
                },
            },
            self._tool_find_breadcrumbs,
        )
        self.register_tool(
            "get_breadcrumb",
            {
                "description": "Get a single breadcrumb by identifier.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workspace": {"type": "string"},
                        "project": {"type": "string"},
                        "identifier": {"type": "string"},
                    },
                    "required": ["workspace", "project", "identifier"],
                },
            },
            self._tool_get_breadcrumb,
        )
        self.register_tool(
            "suggest_duplicates",
            {
                "description": ("Suggest potential duplicate breadcrumbs by embedding similarity."),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workspace": {"type": "string"},
                        "project": {"type": "string"},
                        "identifier": {"type": "string"},
                        "threshold": {"type": "number", "default": 0.95},
                    },
                    "required": ["workspace", "project", "identifier"],
                },
            },
            self._tool_suggest_duplicates,
        )
        self.register_tool(
            "diagnose",
            {
                "description": ("Return diagnostic_keys and recent change_log for a breadcrumb."),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workspace": {"type": "string"},
                        "project": {"type": "string"},
                        "identifier": {"type": "string"},
                    },
                    "required": ["workspace", "project", "identifier"],
                },
            },
            self._tool_diagnose,
        )
        self.register_tool(
            "trace_graph",
            {
                "description": ("Kind-local link graph traversal for breadcrumbs (decision 10)."),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workspace": {"type": "string"},
                        "project": {"type": "string"},
                        "identifier": {"type": "string"},
                        "direction": {
                            "type": "string",
                            "enum": ["dependents", "dependencies"],
                            "default": "dependencies",
                        },
                        "max_depth": {"type": "integer", "default": 3},
                        "relationship_kinds": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["workspace", "project", "identifier"],
                },
            },
            self._tool_trace_graph,
        )
        self.register_tool(
            "render_index",
            {
                "description": "Render a markdown index table for a project's breadcrumbs.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workspace": {"type": "string"},
                        "project": {"type": "string"},
                        "status_filter": {"type": "string"},
                    },
                    "required": ["workspace", "project"],
                },
            },
            self._tool_render_index,
        )

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    def _tool_file_breadcrumb(self, args: dict) -> str:
        from agent_notes.core.embed import embed

        ws_slug = args["workspace"]
        proj_slug = args["project"]
        ws = self._resolve_workspace(ws_slug)
        proj = self._resolve_project(ws.id, proj_slug)

        identifier = args.get("identifier")
        title = args["title"]
        body = args.get("body", "")
        kind = args["kind"]
        status = args["status"]
        severity = args.get("severity", "medium")
        external_refs = args.get("external_refs")
        diagnostic_keys = args.get("diagnostic_keys")

        vec = embed(title + " " + body, task="document")

        row = BreadcrumbModel.file_breadcrumb(
            project_id=proj.id,
            identifier=identifier,
            title=title,
            body=body,
            kind=kind,
            status=status,
            severity=severity,
            external_refs=external_refs,
            diagnostic_keys=diagnostic_keys,
            embedding=vec.tolist(),
        )
        allocated_id = row["identifier"]
        return (
            f"Breadcrumb filed: **{allocated_id}** "
            f"({row['kind']} / {row['status']}) in project {proj_slug}"
        )

    def _tool_update_breadcrumb(self, args: dict) -> str:
        from agent_notes.core.embed import embed

        ws_slug = args["workspace"]
        proj_slug = args["project"]
        identifier = args["identifier"]
        ws = self._resolve_workspace(ws_slug)
        proj = self._resolve_project(ws.id, proj_slug)

        fields: dict[str, Any] = {}
        for key in (
            "title",
            "body",
            "kind",
            "status",
            "severity",
            "external_refs",
            "diagnostic_keys",
        ):
            if key in args and args[key] is not None:
                fields[key] = args[key]

        if "body" in fields or "title" in fields:
            old = BreadcrumbModel.get_breadcrumb(proj.id, identifier)
            text = (
                fields.get("title", old.get("title", ""))
                + " "
                + fields.get("body", old.get("body", ""))
            )
            fields["embedding"] = embed(text, task="document").tolist()

        row = BreadcrumbModel.update_breadcrumb(
            project_id=proj.id,
            identifier=identifier,
            **fields,
        )
        return f"Breadcrumb updated: **{row['identifier']}** ({row['kind']} / {row['status']})"

    def _tool_query_breadcrumbs(self, args: dict) -> str:
        ws_slug = args.get("workspace")
        proj_slug = args.get("project")

        workspace_id = None
        project_id = None
        if ws_slug:
            ws = self._resolve_workspace(ws_slug)
            workspace_id = ws.id
            if proj_slug:
                proj = self._resolve_project(ws.id, proj_slug)
                project_id = proj.id
        elif proj_slug:
            pass

        status = args.get("status")
        kind = args.get("kind")
        severity = args.get("severity")
        is_open = args.get("is_open")
        limit = min(int(args.get("limit", 50)), 200)

        rows = BreadcrumbModel.query_breadcrumbs(
            project_id=project_id,
            workspace_id=workspace_id,
            status=status,
            kind=kind,
            severity=severity,
            is_open=is_open,
            limit=limit,
        )
        if not rows:
            return "No breadcrumbs found."
        lines = [f"{len(rows)} breadcrumb(s) found:"]
        for r in rows:
            lines.append(f"- **{r['identifier']}** ({r['kind']} / {r['status']}) — {r['title']}")
        return "\n".join(lines)

    def _tool_find_breadcrumbs(self, args: dict) -> str:
        from agent_notes.core.embed import embed

        ws_slug = args["workspace"]
        proj_slug = args.get("project")
        query = args["query"]
        limit = min(int(args.get("limit", 10)), 50)

        ws = self._resolve_workspace(ws_slug)
        workspace_id = ws.id
        project_id = None
        if proj_slug:
            proj = self._resolve_project(ws.id, proj_slug)
            project_id = proj.id

        vec = embed(query, task="query")
        rows = BreadcrumbModel.find_breadcrumbs(
            query_vec=vec.tolist(),
            project_id=project_id,
            workspace_id=workspace_id,
            limit=limit,
        )
        if not rows:
            return "No matching breadcrumbs found."
        lines = [f"{len(rows)} breadcrumb(s) matched:"]
        for r in rows:
            lines.append(
                f"- **{r['identifier']}** ({r['kind']}) — {r['title']} (sim={r['distance']:.3f})"
            )
        return "\n".join(lines)

    def _tool_get_breadcrumb(self, args: dict) -> str:
        ws_slug = args["workspace"]
        proj_slug = args["project"]
        identifier = args["identifier"]
        ws = self._resolve_workspace(ws_slug)
        proj = self._resolve_project(ws.id, proj_slug)
        row = BreadcrumbModel.get_breadcrumb(proj.id, identifier)
        if row is None:
            return f"Breadcrumb '{identifier}' not found in project '{proj_slug}'."
        return _format_breadcrumb(row)

    def _tool_suggest_duplicates(self, args: dict) -> str:
        ws_slug = args["workspace"]
        proj_slug = args["project"]
        identifier = args["identifier"]
        threshold = float(args.get("threshold", 0.95))
        ws = self._resolve_workspace(ws_slug)
        proj = self._resolve_project(ws.id, proj_slug)
        dups = BreadcrumbModel.suggest_duplicates(proj.id, identifier, threshold=threshold)
        if not dups:
            return f"No duplicates suggested for '{identifier}'."
        lines = [f"{len(dups)} potential duplicate(s) for '{identifier}':"]
        for d in dups:
            lines.append(f"- **{d['identifier']}** — {d['title']} (sim={d['similarity']:.3f})")
        return "\n".join(lines)

    def _tool_diagnose(self, args: dict) -> str:
        ws_slug = args["workspace"]
        proj_slug = args["project"]
        identifier = args["identifier"]
        ws = self._resolve_workspace(ws_slug)
        proj = self._resolve_project(ws.id, proj_slug)
        diag = BreadcrumbModel.diagnose(proj.id, identifier)
        bc = diag["breadcrumb"]
        changes = diag["recent_changes"]
        lines = [
            f"**{bc['identifier']}** — {bc['title']}",
            f"Status: {bc['status']} | Severity: {bc['severity']}",
        ]
        dkeys = diag["diagnostic_keys"]
        if dkeys:
            lines.append("Diagnostic keys:")
            for k, v in dkeys.items():
                lines.append(f"  {k}: {v}")
        lines.append(f"Recent changes ({len(changes)}):")
        for c in changes:
            lines.append(f"- {c['event']} at {c['changed_at']}")
        return "\n".join(lines)

    def _tool_trace_graph(self, args: dict) -> str:
        ws_slug = args["workspace"]
        proj_slug = args["project"]
        identifier = args["identifier"]
        direction = args.get("direction", "dependencies")
        max_depth = min(int(args.get("max_depth", 3)), 10)
        rel_kinds = args.get("relationship_kinds")
        ws = self._resolve_workspace(ws_slug)
        proj = self._resolve_project(ws.id, proj_slug)
        nodes = core_trace_graph(
            kind=_KIND,
            workspace=ws.id,
            project=proj.id,
            identifier=identifier,
            direction=direction,
            max_depth=max_depth,
            relationship_kinds=rel_kinds,
        )
        if not nodes:
            return f"No linked notes found for breadcrumb '{identifier}' ({direction})."
        lines = [f"{len(nodes)} linked note(s) for '{identifier}' ({direction}):"]
        for n in nodes:
            lines.append(f"- {n.identifier} (relationship={n.relationship}, depth={n.depth})")
        return "\n".join(lines)

    def _tool_render_index(self, args: dict) -> str:
        ws_slug = args["workspace"]
        proj_slug = args["project"]
        ws = self._resolve_workspace(ws_slug)
        proj = self._resolve_project(ws.id, proj_slug)
        status_filter = args.get("status_filter")
        rows = BreadcrumbModel.query_breadcrumbs(
            project_id=proj.id,
            status=status_filter,
            limit=1000,
        )
        return _render_index_table(rows)


def _format_breadcrumb(row: dict) -> str:
    lines = [
        f"**{row['identifier']}** — {row['title']}",
        f"Kind: {row['kind']} | Status: {row['status']} | Severity: {row['severity']}",
        f"Created: {row['created_at']}",
        f"Updated: {row['updated_at']}",
        "",
        row.get("body", ""),
    ]
    return "\n".join(lines)


def _render_index_table(rows: list[dict]) -> str:
    if not rows:
        return "| Identifier | Title | Kind | Status |\n|---|---|---|---|\n"
    lines = ["| Identifier | Title | Kind | Status |", "|---|---|---|---|"]
    for row in rows:
        ident = row.get("identifier", "")
        title = row.get("title", "")
        kind = row.get("kind", "")
        status = row.get("status", "")
        lines.append(f"| {ident} | {title} | {kind} | {status} |")
    return "\n".join(lines) + "\n"
