"""Server base class: composable class-registry pattern (decisions 12, 22)."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from agent_notes.core.mcp import ToolNotFoundError, ToolRegistry, _err, _ok, _send

_log = logging.getLogger(__name__)

PROTOCOL_VERSION = "2024-11-05"


class Server:
    """Base MCP server.

    Subclass and call `register_tool` / `register_resource_handler` in
    `__init__` (or at class body level via `_class_registry`) to expose tools.
    Multiple subclass registries can be merged into one omnibus server via
    `merge_registry` (decision 12).

    The `run()` method starts the blocking stdin loop (decision 22 — sync).
    """

    name: str = "agent-notes"
    version: str = "0.1.0"

    def __init__(self) -> None:
        self._registry = ToolRegistry()
        self._register_core_tools()

    # ------------------------------------------------------------------
    # Registration API
    # ------------------------------------------------------------------

    def register_tool(self, name: str, schema: dict, fn: Any) -> None:
        """Register a tool at runtime (composable; called from subclass __init__)."""
        self._registry.register(name, schema, fn)

    def register_resource_handler(self, uri_prefix: str, fn: Any) -> None:
        self._registry.register_resource_handler(uri_prefix, fn)

    def merge_registry(self, other: "Server") -> list[str]:
        """Merge another server's registry into this one (omnibus mode, decision 12).

        Returns a list of non-core tool name collisions (empty if no conflicts).
        """
        return self._registry.merge(other._registry)

    # ------------------------------------------------------------------
    # Core tools wired automatically on every server (decision 6 / §6)
    # ------------------------------------------------------------------

    def _register_core_tools(self) -> None:
        self.register_tool(
            "list_workspaces",
            {
                "description": "List all workspaces.",
                "inputSchema": {"type": "object", "properties": {}, "required": []},
            },
            self._tool_list_workspaces,
        )
        self.register_tool(
            "list_projects",
            {
                "description": "List projects, optionally filtered by workspace.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workspace": {"type": "string", "description": "Workspace slug (optional)"},
                    },
                    "required": [],
                },
            },
            self._tool_list_projects,
        )
        self.register_tool(
            "resolve_project",
            {
                "description": (
                    "Resolve a filesystem path to a registered project. "
                    "Returns workspace, project, and repo_root."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Filesystem path (absolute or relative)",
                        },
                    },
                    "required": ["path"],
                },
            },
            self._tool_resolve_project,
        )
        self.register_tool(
            "list_vocabulary",
            {
                "description": "List vocabulary entries for a workspace and optional kind namespace.",  # noqa: E501
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workspace": {"type": "string"},
                        "kind_namespace": {"type": "string", "description": "e.g. 'bc_status'"},
                        "include_archived": {"type": "boolean", "default": False},
                    },
                    "required": ["workspace"],
                },
            },
            self._tool_list_vocabulary,
        )
        self.register_tool(
            "archive_vocabulary",
            {
                "description": (
                    "Soft-deprecate a vocabulary entry "
                    "(hides from pick-lists, keeps existing rows valid)."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workspace": {"type": "string"},
                        "kind_namespace": {"type": "string"},
                        "name": {"type": "string"},
                    },
                    "required": ["workspace", "kind_namespace", "name"],
                },
            },
            self._tool_archive_vocabulary,
        )
        self.register_tool(
            "changes_since",
            {
                "description": "List change_log entries since a given timestamp.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "since": {"type": "string", "description": "ISO timestamp"},
                        "workspace": {"type": "string"},
                        "project": {"type": "string"},
                        "kind": {"type": "string"},
                        "limit": {"type": "integer", "default": 50},
                    },
                    "required": ["since"],
                },
            },
            self._tool_changes_since,
        )
        self.register_tool(
            "add_link",
            {
                "description": "Add a typed link between two notes (decision 16).",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "from_kind": {"type": "string"},
                        "from_workspace": {"type": "integer"},
                        "from_project": {"type": "integer"},
                        "from_identifier": {"type": "string"},
                        "to_kind": {"type": "string"},
                        "to_workspace": {"type": "integer"},
                        "to_project": {"type": "integer"},
                        "to_identifier": {"type": "string"},
                        "relationship": {"type": "string"},
                    },
                    "required": [
                        "from_kind",
                        "from_workspace",
                        "from_project",
                        "from_identifier",
                        "to_kind",
                        "to_workspace",
                        "to_project",
                        "to_identifier",
                        "relationship",
                    ],
                },
            },
            self._tool_add_link,
        )
        self.register_tool(
            "remove_link",
            {
                "description": "Remove a typed link between two notes (decision 16).",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "from_kind": {"type": "string"},
                        "from_workspace": {"type": "integer"},
                        "from_project": {"type": "integer"},
                        "from_identifier": {"type": "string"},
                        "to_kind": {"type": "string"},
                        "to_workspace": {"type": "integer"},
                        "to_project": {"type": "integer"},
                        "to_identifier": {"type": "string"},
                        "relationship": {"type": "string"},
                    },
                    "required": [
                        "from_kind",
                        "from_workspace",
                        "from_project",
                        "from_identifier",
                        "to_kind",
                        "to_workspace",
                        "to_project",
                        "to_identifier",
                        "relationship",
                    ],
                },
            },
            self._tool_remove_link,
        )
        self.register_tool(
            "history",
            {
                "description": (
                    "Return change_log rows for a specific note in chronological order."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string"},
                        "workspace": {"type": "string", "description": "Workspace slug"},
                        "project": {"type": "string", "description": "Project slug"},
                        "identifier": {"type": "string"},
                        "limit": {"type": "integer", "default": 100},
                    },
                    "required": ["kind", "workspace", "project", "identifier"],
                },
            },
            self._tool_history,
        )

    # ------------------------------------------------------------------
    # Core tool implementations — thin wrappers over core/db.py
    # ------------------------------------------------------------------

    def _tool_list_workspaces(self, args: dict) -> str:
        from agent_notes.core import db

        workspaces = db.list_workspaces()
        if not workspaces:
            return "No workspaces registered."
        lines = [f"{len(workspaces)} workspace(s):"]
        for w in workspaces:
            lines.append(f"- **{w.slug}** — {w.name} (id={w.id})")
        return "\n".join(lines)

    def _tool_list_projects(self, args: dict) -> str:
        from agent_notes.core import db

        workspace_slug = args.get("workspace")
        workspace_id = None
        if workspace_slug:
            ws = next((w for w in db.list_workspaces() if w.slug == workspace_slug), None)
            if ws is None:
                return f"Error: workspace '{workspace_slug}' not found"
            workspace_id = ws.id
        projects = db.list_projects(workspace_id=workspace_id)
        if not projects:
            return "No projects found."
        lines = [f"{len(projects)} project(s):"]
        for p in projects:
            repo = f" repo={p.repo_root}" if p.repo_root else ""
            lines.append(f"- **{p.slug}** — {p.name}{repo} (workspace_id={p.workspace_id})")
        return "\n".join(lines)

    def _tool_resolve_project(self, args: dict) -> str:
        from agent_notes.core import db

        path = args.get("path", "")
        if not path:
            return "Error: path is required"
        try:
            result = db.resolve_project(path)
            return (
                f"Project resolved from {path!r}:\n"
                f"- Workspace: {result['workspace']}\n"
                f"- Project: {result['project']}\n"
                f"- Repo root: {result['repo_root']}\n"
            )
        except ValueError as exc:
            return f"Error: {exc}"

    def _tool_list_vocabulary(self, args: dict) -> str:
        from agent_notes.core import db

        workspace_slug = args.get("workspace", "")
        ws = next((w for w in db.list_workspaces() if w.slug == workspace_slug), None)
        if ws is None:
            return f"Error: workspace '{workspace_slug}' not found"
        kind_ns = args.get("kind_namespace")
        include_archived = bool(args.get("include_archived", False))
        vocabs = db.list_vocabulary(
            ws.id, kind_namespace=kind_ns, include_archived=include_archived
        )
        if not vocabs:
            return "No vocabulary entries found."
        lines = [f"{len(vocabs)} vocab entries:"]
        for v in vocabs:
            flags = []
            if v.is_terminal:
                flags.append("terminal")
            if not v.is_open:
                flags.append("closed")
            if v.archived:
                flags.append("archived")
            flag_str = f" [{', '.join(flags)}]" if flags else ""
            lines.append(f"- {v.kind_namespace}/{v.name}{flag_str} (sort={v.sort_order})")
        return "\n".join(lines)

    def _tool_archive_vocabulary(self, args: dict) -> str:
        from agent_notes.core import db

        workspace_slug = args.get("workspace", "")
        ws = next((w for w in db.list_workspaces() if w.slug == workspace_slug), None)
        if ws is None:
            return f"Error: workspace '{workspace_slug}' not found"
        kind_ns = args.get("kind_namespace", "")
        name = args.get("name", "")
        db.archive_vocabulary(ws.id, kind_ns, name)
        return f"Archived vocabulary entry: {kind_ns}/{name}"

    def _tool_changes_since(self, args: dict) -> str:
        from datetime import datetime

        from agent_notes.core import db

        since_str = args.get("since", "").strip()
        if not since_str:
            return "Error: 'since' is required (ISO timestamp)"
        try:
            since = datetime.fromisoformat(since_str)
        except ValueError:
            return "Error: invalid timestamp; use ISO format e.g. '2025-05-20T00:00:00'"
        workspace_slug = args.get("workspace")
        workspace_id = None
        if workspace_slug:
            ws = next((w for w in db.list_workspaces() if w.slug == workspace_slug), None)
            if ws is None:
                return f"Error: workspace '{workspace_slug}' not found"
            workspace_id = ws.id
        project_id = None
        project_slug = args.get("project")
        if project_slug and workspace_id:
            projects = db.list_projects(workspace_id=workspace_id)
            p = next((x for x in projects if x.slug == project_slug), None)
            if p is None:
                return f"Error: project '{project_slug}' not found"
            project_id = p.id
        kind = args.get("kind")
        limit = min(int(args.get("limit", 50)), 200)
        rows = db.changes_since(
            since, workspace_id=workspace_id, project_id=project_id, kind=kind, limit=limit
        )
        if not rows:
            return f"No changes since {since_str}."
        lines = [f"{len(rows)} change(s) since {since_str}:"]
        for r in rows:
            ts = r["changed_at"].strftime("%Y-%m-%d %H:%M")
            lines.append(f"- [{r['kind']}] {r['identifier']} event={r['event']} at {ts}")
        return "\n".join(lines)

    def _tool_add_link(self, args: dict) -> str:
        # Route through links.add_link (not db.add_link) so the change_log row
        # is written in the same transaction — decision 20.
        from agent_notes.core import links

        try:
            links.add_link(
                from_kind=args["from_kind"],
                from_workspace=int(args["from_workspace"]),
                from_project=int(args["from_project"]),
                from_identifier=args["from_identifier"],
                to_kind=args["to_kind"],
                to_workspace=int(args["to_workspace"]),
                to_project=int(args["to_project"]),
                to_identifier=args["to_identifier"],
                relationship=args["relationship"],
            )
        except Exception as exc:
            return f"Error: {exc}"
        fk, fi = args["from_kind"], args["from_identifier"]
        tk, ti, rel = args["to_kind"], args["to_identifier"], args["relationship"]
        return f"Link added: {fk}/{fi} --{rel}--> {tk}/{ti}"

    def _tool_history(self, args: dict) -> str:
        from agent_notes.core import change_log, db

        workspace_slug = args.get("workspace", "")
        ws = next((w for w in db.list_workspaces() if w.slug == workspace_slug), None)
        if ws is None:
            return f"Error: workspace '{workspace_slug}' not found"
        project_slug = args.get("project", "")
        projects = db.list_projects(workspace_id=ws.id)
        p = next((x for x in projects if x.slug == project_slug), None)
        if p is None:
            return f"Error: project '{project_slug}' not found"
        kind = args.get("kind", "")
        identifier = args.get("identifier", "")
        limit = min(int(args.get("limit", 100)), 500)
        rows = change_log.history(kind, ws.id, p.id, identifier, limit=limit)
        if not rows:
            return f"No history for {kind}/{identifier}."
        lines = [f"{len(rows)} change(s) for {kind}/{identifier}:"]
        for r in rows:
            lines.append(f"- event={r.event} at {r.changed_at.strftime('%Y-%m-%d %H:%M')}")
        return "\n".join(lines)

    def _tool_remove_link(self, args: dict) -> str:
        # Route through links.remove_link (not db.remove_link) so the
        # change_log row is written in the same transaction — decision 20.
        from agent_notes.core import links

        removed = links.remove_link(
            from_kind=args["from_kind"],
            from_workspace=int(args["from_workspace"]),
            from_project=int(args["from_project"]),
            from_identifier=args["from_identifier"],
            to_kind=args["to_kind"],
            to_workspace=int(args["to_workspace"]),
            to_project=int(args["to_project"]),
            to_identifier=args["to_identifier"],
            relationship=args["relationship"],
        )
        if removed:
            fk, fi = args["from_kind"], args["from_identifier"]
            tk, ti, rel = args["to_kind"], args["to_identifier"], args["relationship"]
            return f"Link removed: {fk}/{fi} --{rel}--> {tk}/{ti}"
        return "No such link found."

    # ------------------------------------------------------------------
    # MCP protocol handlers
    # ------------------------------------------------------------------

    def _handle_initialize(self, req_id: Any) -> None:
        _send(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}, "resources": {}},
                    "serverInfo": {"name": self.name, "version": self.version},
                },
            }
        )

    def _handle_tools_list(self, req_id: Any) -> None:
        _send({"jsonrpc": "2.0", "id": req_id, "result": {"tools": self._registry.list_tools()}})

    def _handle_tools_call(self, req_id: Any, params: dict) -> None:
        name = params.get("name", "")
        tool_args = params.get("arguments", {}) or {}
        try:
            text = self._registry.call(name, tool_args)
            _send(_ok(req_id, text))
        except ToolNotFoundError:
            _send(_err(req_id, -32601, f"Unknown tool: {name}"))
        except KeyError as exc:
            _send(_err(req_id, -32601, f"Tool argument missing: {exc}"))
        except Exception:
            _log.exception("Unhandled error in tool %r", name)
            _send(_err(req_id, -32603, "Internal error"))

    def _handle_resources_list(self, req_id: Any) -> None:
        resources: list[dict] = []
        for prefix, fn in self._registry.list_resource_handlers():
            try:
                listed = fn("list", prefix)
                if isinstance(listed, list):
                    resources.extend(listed)
            except Exception:
                _log.warning("Resource handler %r raised during list", prefix, exc_info=True)
        _send({"jsonrpc": "2.0", "id": req_id, "result": {"resources": resources}})

    def _handle_resources_read(self, req_id: Any, params: dict) -> None:
        uri = params.get("uri", "")
        for prefix, fn in self._registry.list_resource_handlers():
            if uri.startswith(prefix):
                try:
                    content = fn("read", uri)
                    _send(
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {
                                "contents": [
                                    {"uri": uri, "mimeType": "text/plain", "text": content}
                                ]
                            },
                        }
                    )
                except Exception as exc:
                    _send(_err(req_id, -32603, str(exc)))
                return
        _send(_err(req_id, -32601, f"No resource handler for URI: {uri}"))

    # ------------------------------------------------------------------
    # Main loop (sync, decision 22)
    # ------------------------------------------------------------------

    def run(self) -> None:
        for raw_line in sys.stdin:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                req = json.loads(raw_line)
            except json.JSONDecodeError:
                _log.warning("Invalid JSON on stdin (len=%d)", len(raw_line))
                continue

            req_id = req.get("id")
            method = req.get("method", "")
            params = req.get("params", {}) or {}

            if method == "initialize":
                self._handle_initialize(req_id)
            elif method == "notifications/initialized":
                pass
            elif method == "tools/list":
                self._handle_tools_list(req_id)
            elif method == "tools/call":
                self._handle_tools_call(req_id, params)
            elif method == "resources/list":
                self._handle_resources_list(req_id)
            elif method == "resources/read":
                self._handle_resources_read(req_id, params)
            elif method == "ping":
                _send({"jsonrpc": "2.0", "id": req_id, "result": {}})
            else:
                _send(_err(req_id, -32601, f"Method not found: {method}"))
