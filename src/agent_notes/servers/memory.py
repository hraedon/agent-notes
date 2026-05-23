"""Memory MCP server — thin kind server composing core modules (§6, Phase 3).

Tools: add_memory, search_memory, list_memories, get_memory, delete_memory,
       trace_graph (kind-local, from core).
Inherits from core: list_workspaces, list_projects, list_vocabulary,
       archive_vocabulary, changes_since, history, add_link, remove_link.

Design decisions:
- Project is required (no default). Use 'global' project for cross-cutting
  memories (decision 13, Phase 0.2 note).
- Embedding call precedes DB transaction (decision 26).
- [[name]] references in body auto-create relates_to links (Phase 3.3).
- search_memory body-elision by default; include_body opt-in (Phase 3.4).
- Soft-delete via active=false; partial unique index on (project_id, name)
  WHERE active (Kimi round-5 #2).
"""

from __future__ import annotations

import re
from typing import Any

import psycopg
from psycopg.rows import dict_row

from agent_notes.core.change_log import write_change
from agent_notes.core.links import trace_graph as core_trace_graph
from agent_notes.core.server import Server

_KIND = "memory"

_WIKILINK_RE = re.compile(r"(?<!`)\[\[([^\]`]+)\]\](?!`)")

_GAP_SECTION_RE = re.compile(
    r"^##\s+Gaps?\s+to\s+flag\s*\n(.*?)(?=\n##|\Z)",
    re.IGNORECASE | re.DOTALL | re.MULTILINE,
)

_GAP_LINE_RE = re.compile(r"^\s*-\s+\[\[([^\]]+)\]\]\s*:\s*(.+)$", re.MULTILINE)


def _parse_wikilinks(body: str) -> list[str]:
    """Extract [[name]] references from body, skipping fenced code spans.

    A simple heuristic: if the wikilink is adjacent to backticks (inline code),
    skip it. For full fenced-code-block skipping, a more elaborate parser would
    be needed, but this covers the dominant case (decision §4.3: "skip fenced
    code spans").
    """
    return _WIKILINK_RE.findall(body)


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

    def _register_memory_tools(self) -> None:
        self.register_tool(
            "add_memory",
            {
                "description": (
                    "Add a new memory. Requires explicit project slug "
                    "(use 'global' for cross-cutting memories). "
                    "Embeds the body text and auto-creates [[name]] relates_to links."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workspace": {"type": "string", "description": "Workspace slug"},
                        "project": {
                            "type": "string",
                            "description": (
                                "Project slug (required; use 'global' for cross-cutting)"
                            ),
                        },
                        "name": {
                            "type": "string",
                            "description": "Short unique name for the memory",
                        },
                        "memory_type": {
                            "type": "string",
                            "description": (
                                "Memory type (must exist in vocabularies as memory_type)"
                            ),
                        },
                        "body": {"type": "string", "description": "Full body text"},
                        "attributes": {
                            "type": "object",
                            "description": "Optional free-form metadata",
                        },
                    },
                    "required": ["workspace", "project", "name", "memory_type", "body"],
                },
            },
            self._tool_add_memory,
        )
        self.register_tool(
            "search_memory",
            {
                "description": (
                    "Search memories by semantic similarity. Returns name, type, "
                    "score, and (optionally) body. Body is elided by default to "
                    "save tokens (Phase 3.4)."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workspace": {"type": "string"},
                        "project": {"type": "string"},
                        "query": {"type": "string", "description": "Search query text"},
                        "limit": {"type": "integer", "default": 10},
                        "include_body": {"type": "boolean", "default": False},
                        "memory_type": {"type": "string", "description": "Filter by memory_type"},
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
                "description": (
                    "Soft-delete a memory (sets active=false). "
                    "Writes event='deleted' to change_log."
                ),
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
                "description": (
                    "Kind-local link graph traversal for memories (decision 10). "
                    "Follows relationships in the links table up to max_depth hops."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workspace": {"type": "string"},
                        "project": {"type": "string"},
                        "name": {"type": "string", "description": "Memory name to start from"},
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
                    "required": ["workspace", "project", "name"],
                },
            },
            self._tool_trace_graph,
        )
        self.register_tool(
            "find_reflections",
            {
                "description": (
                    "Find reflection memories (memory_type='reflection'). "
                    "Returns matching reflections ranked by semantic similarity. "
                    "Phase 5.1 / decision 25: reflections are stored as memories."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workspace": {"type": "string"},
                        "project": {"type": "string"},
                        "query": {"type": "string", "description": "Search query (optional)"},
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
                "description": (
                    "Parse the 'Gaps to flag' section from a reflection memory and "
                    "return structured gap entries. Each gap has an identifier and "
                    "description. The agent can then confirm and call file_breadcrumb "
                    "for each gap (Phase 5.3)."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workspace": {"type": "string"},
                        "project": {"type": "string"},
                        "name": {"type": "string", "description": "Reflection memory name"},
                    },
                    "required": ["workspace", "project", "name"],
                },
            },
            self._tool_extract_gaps,
        )
        self.register_tool(
            "mark_gaps_filed",
            {
                "description": (
                    "Mark specific gaps from a reflection as filed (decision 19). "
                    "Updates the reflection memory's attributes.gaps_filed_as list. "
                    "Body remains immutable (append-only)."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workspace": {"type": "string"},
                        "project": {"type": "string"},
                        "name": {"type": "string", "description": "Reflection memory name"},
                        "filed_identifiers": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of BC identifiers that gaps were filed as",
                        },
                    },
                    "required": ["workspace", "project", "name", "filed_identifiers"],
                },
            },
            self._tool_mark_gaps_filed,
        )

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

    def _validate_memory_type(self, workspace_id: int, memory_type: str) -> None:
        from agent_notes.core.db import list_vocabulary

        vocabs = list_vocabulary(workspace_id, kind_namespace="memory_type")
        if not any(v.name == memory_type for v in vocabs):
            raise ValueError(
                f"memory_type '{memory_type}' not found in vocabularies. "
                f"Available: {', '.join(v.name for v in vocabs) or '(none)'}"
            )

    # ------------------------------------------------------------------
    # Resource handlers (Phase 6.1)
    # ------------------------------------------------------------------

    def _register_resource_handlers(self) -> None:
        from agent_notes.core.resources import build_uri, parse_uri

        def _list_fn(workspace_slug: str, project_slug: str) -> list[dict]:
            ws = self._resolve_workspace(workspace_slug)
            proj = self._resolve_project(ws.id, project_slug)
            rows = self._list_active_memories_raw(proj.id, ws.id)
            return [
                {
                    "uri": build_uri("memory", workspace_slug, project_slug, r["name"]),
                    "name": r["name"],
                    "mimeType": "text/markdown",
                    "description": f"{r['memory_type']} — {r['body_preview'][:60]}",
                }
                for r in rows
            ]

        def _read_fn(workspace_slug: str, project_slug: str, name: str) -> str:
            ws = self._resolve_workspace(workspace_slug)
            proj = self._resolve_project(ws.id, project_slug)
            with _conn() as conn:
                cur = conn.cursor(row_factory=dict_row)
                cur.execute(
                    """
                    SELECT id, name, memory_type, body, attributes,
                           supersedes, created_at, updated_at
                    FROM memories
                    WHERE project_id = %s AND workspace_id = %s AND name = %s AND active = true
                    """,
                    (proj.id, ws.id, name),
                )
                row = cur.fetchone()
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

    def _list_active_memories_raw(self, project_id: int, workspace_id: int) -> list[dict]:
        with _conn() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                """
                SELECT id, name, memory_type, LEFT(body, 120) AS body_preview,
                       created_at, updated_at
                FROM memories
                WHERE project_id = %s AND workspace_id = %s AND active = true
                ORDER BY updated_at DESC
                """,
                (project_id, workspace_id),
            )
            return cur.fetchall()

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

        # Embed BEFORE transaction (decision 26).
        vec = embed(body, task="document")

        with _conn() as conn:
            cur = conn.cursor(row_factory=dict_row)

            # Check for existing active memory with the same name — supersede it.
            cur.execute(
                "SELECT id FROM memories WHERE project_id = %s AND name = %s AND active = true",
                (proj.id, name),
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
                RETURNING id
                """,
                (
                    ws.id,
                    proj.id,
                    name,
                    memory_type,
                    body,
                    vec.tolist(),
                    old_id,
                    psycopg.types.json.Jsonb(attributes),
                ),
            )
            new_id = cur.fetchone()["id"]

            # The supersedes chain is tracked via the memories.supersedes column
            # and surfaced via the history tool / change_log. It is NOT mirrored
            # into the links table because all revisions share the same name — a
            # link with from_identifier=name, to_identifier=name would be a
            # self-referencing loop, useless for traversal.

            # Write change_log: event='filed' (decision 20).
            write_change(
                conn,
                kind=_KIND,
                workspace_id=ws.id,
                project_id=proj.id,
                identifier=name,
                event="filed",
                payload={"memory_type": memory_type, "id": new_id},
            )

            conn.commit()

        # Auto-create [[name]] relates_to links (Phase 3.3).
        # Done outside the main transaction to keep it simple — link creation
        # is best-effort and idempotent (ON CONFLICT DO NOTHING).
        wikilink_names = _parse_wikilinks(body)
        if wikilink_names:
            from agent_notes.core import links as lnk

            for ref_name in set(wikilink_names):
                try:
                    lnk.add_link(
                        from_kind=_KIND,
                        from_workspace=ws.id,
                        from_project=proj.id,
                        from_identifier=name,
                        to_kind=_KIND,
                        to_workspace=ws.id,
                        to_project=proj.id,
                        to_identifier=ref_name,
                        relationship="relates_to",
                    )
                except Exception:
                    pass

        superseded_msg = f" (superseded id={old_id})" if old_id else ""
        return (
            f"Memory '{name}' added (id={new_id}, type={memory_type}"
            f", project={project_slug}){superseded_msg}"
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

        conditions = [
            "active = true",
            "workspace_id = %s",
            "embedding IS NOT NULL",
        ]
        params: list[Any] = [ws.id]

        if project_slug:
            proj = self._resolve_project(ws.id, project_slug)
            conditions.append("project_id = %s")
            params.append(proj.id)

        if memory_type:
            conditions.append("memory_type = %s")
            params.append(memory_type)

        where = " AND ".join(conditions)

        body_col = "body" if include_body else "LEFT(body, 0) AS body"

        with _conn() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                f"""
                SELECT id, name, memory_type, {body_col},
                       1 - (embedding <=> %s::vector) AS score,
                       created_at, updated_at
                FROM memories
                WHERE {where}
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                [vec.tolist()] + params + [vec.tolist(), limit],
            )
            rows = cur.fetchall()

        if not rows:
            return "No matching memories found."

        lines = [f"{len(rows)} memory(ies) matched:"]
        for r in rows:
            line = f"- **{r['name']}** (type={r['memory_type']}, score={r['score']:.3f})"
            if include_body and r["body"]:
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

        conditions = ["active = true", "project_id = %s", "workspace_id = %s"]
        params: list[Any] = [proj.id, ws.id]

        if memory_type:
            conditions.append("memory_type = %s")
            params.append(memory_type)

        where = " AND ".join(conditions)
        params.append(limit)

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
            rows = cur.fetchall()

        if not rows:
            return f"No memories in project '{project_slug}'."

        lines = [f"{len(rows)} memory(ies) in {project_slug}:"]
        for r in rows:
            preview = r["body_preview"].replace("\n", " ")[:80] if r["body_preview"] else ""
            lines.append(f"- **{r['name']}** (type={r['memory_type']}) {preview}")
        return "\n".join(lines)

    def _tool_get_memory(self, args: dict) -> str:
        workspace_slug = args["workspace"]
        project_slug = args["project"]
        name = args["name"]

        ws = self._resolve_workspace(workspace_slug)
        proj = self._resolve_project(ws.id, project_slug)

        with _conn() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                """
                SELECT id, name, memory_type, body, attributes,
                       supersedes, created_at, updated_at
                FROM memories
                WHERE project_id = %s AND workspace_id = %s AND name = %s AND active = true
                """,
                (proj.id, ws.id, name),
            )
            row = cur.fetchone()

        if row is None:
            return f"Memory '{name}' not found in project '{project_slug}'."

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

    def _tool_delete_memory(self, args: dict) -> str:
        workspace_slug = args["workspace"]
        project_slug = args["project"]
        name = args["name"]

        ws = self._resolve_workspace(workspace_slug)
        proj = self._resolve_project(ws.id, project_slug)

        with _conn() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                """
                UPDATE memories SET active = false
                WHERE project_id = %s AND workspace_id = %s AND name = %s AND active = true
                RETURNING id
                """,
                (proj.id, ws.id, name),
            )
            row = cur.fetchone()
            if row is None:
                conn.rollback()
                return (
                    f"Memory '{name}' not found (or already deleted) in project '{project_slug}'."
                )

            write_change(
                conn,
                kind=_KIND,
                workspace_id=ws.id,
                project_id=proj.id,
                identifier=name,
                event="deleted",
                payload={"id": row["id"]},
            )
            conn.commit()

        return f"Memory '{name}' soft-deleted (id={row['id']})."

    def _tool_trace_graph(self, args: dict) -> str:
        workspace_slug = args["workspace"]
        project_slug = args["project"]
        name = args["name"]
        direction = args.get("direction", "dependencies")
        max_depth = min(int(args.get("max_depth", 3)), 10)
        rel_kinds = args.get("relationship_kinds")

        ws = self._resolve_workspace(workspace_slug)
        proj = self._resolve_project(ws.id, project_slug)

        nodes = core_trace_graph(
            kind=_KIND,
            workspace=ws.id,
            project=proj.id,
            identifier=name,
            direction=direction,
            max_depth=max_depth,
            relationship_kinds=rel_kinds,
        )

        if not nodes:
            return f"No linked notes found for memory '{name}' ({direction})."

        lines = [f"{len(nodes)} linked note(s) for '{name}' ({direction}):"]
        for n in nodes:
            lines.append(f"- {n.identifier} (relationship={n.relationship}, depth={n.depth})")
        return "\n".join(lines)

    def _tool_find_reflections(self, args: dict) -> str:
        from agent_notes.core.embed import embed

        workspace_slug = args["workspace"]
        project_slug = args.get("project")
        query = args.get("query")
        limit = min(int(args.get("limit", 10)), 50)
        include_body = bool(args.get("include_body", False))

        ws = self._resolve_workspace(workspace_slug)

        conditions = [
            "active = true",
            "workspace_id = %s",
            "memory_type = 'reflection'",
            "embedding IS NOT NULL",
        ]
        params: list[Any] = [ws.id]

        if project_slug:
            proj = self._resolve_project(ws.id, project_slug)
            conditions.append("project_id = %s")
            params.append(proj.id)

        where = " AND ".join(conditions)

        if query:
            vec = embed(query, task="query")
            body_col = "body" if include_body else "LEFT(body, 0) AS body"
            order_clause = "ORDER BY embedding <=> %s::vector"
            select_score = ", 1 - (embedding <=> %s::vector) AS score"
            params_with_vec = [vec.tolist()] + params + [vec.tolist(), limit]
        else:
            body_col = "body" if include_body else "LEFT(body, 0) AS body"
            order_clause = "ORDER BY updated_at DESC"
            select_score = ""
            params_with_vec = params + [limit]

        with _conn() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                f"""
                SELECT id, name, {body_col}, attributes,
                       created_at, updated_at{select_score}
                FROM memories
                WHERE {where}
                {order_clause}
                LIMIT %s
                """,
                params_with_vec,
            )
            rows = cur.fetchall()

        if not rows:
            return "No reflection memories found."

        lines = [f"{len(rows)} reflection(s) found:"]
        for r in rows:
            score_str = f", score={r['score']:.3f}" if "score" in r else ""
            line = f"- **{r['name']}**{score_str}"
            gaps_filed = (r.get("attributes") or {}).get("gaps_filed_as", [])
            if gaps_filed:
                line += f" (gaps_filed: {', '.join(gaps_filed)})"
            if include_body and r["body"]:
                line += f"\n  {r['body'][:200]}"
            lines.append(line)
        return "\n".join(lines)

    def _tool_extract_gaps(self, args: dict) -> str:
        workspace_slug = args["workspace"]
        project_slug = args["project"]
        name = args["name"]

        ws = self._resolve_workspace(workspace_slug)
        proj = self._resolve_project(ws.id, project_slug)

        with _conn() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                "SELECT id, name, body, attributes FROM memories "
                "WHERE project_id = %s AND workspace_id = %s AND name = %s "
                "AND active = true AND memory_type = 'reflection'",
                (proj.id, ws.id, name),
            )
            row = cur.fetchone()

        if row is None:
            return f"Reflection '{name}' not found in project '{project_slug}'."

        body = row["body"]
        section_match = _GAP_SECTION_RE.search(body)
        if section_match is None:
            return f"No 'Gaps to flag' section found in reflection '{name}'."

        section_text = section_match.group(1)
        gaps = _GAP_LINE_RE.findall(section_text)

        if not gaps:
            return f"No structured gap entries (format: '- [[ID]]: description') found in '{name}'."

        already_filed = set((row.get("attributes") or {}).get("gaps_filed_as", []))
        lines = [f"{len(gaps)} gap(s) extracted from '{name}':"]
        for identifier, description in gaps:
            status = " [FILED]" if identifier in already_filed else ""
            lines.append(f"- **{identifier}**{status}: {description.strip()}")
        lines.append("")
        lines.append(
            "Propose filing as breadcrumbs by calling file_breadcrumb for each unfilled gap."
        )
        return "\n".join(lines)

    def _tool_mark_gaps_filed(self, args: dict) -> str:
        workspace_slug = args["workspace"]
        project_slug = args["project"]
        name = args["name"]
        filed_identifiers = args["filed_identifiers"]

        ws = self._resolve_workspace(workspace_slug)
        proj = self._resolve_project(ws.id, project_slug)

        with _conn() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                "SELECT id, attributes FROM memories "
                "WHERE project_id = %s AND workspace_id = %s AND name = %s "
                "AND active = true AND memory_type = 'reflection'",
                (proj.id, ws.id, name),
            )
            row = cur.fetchone()
            if row is None:
                conn.rollback()
                return f"Reflection '{name}' not found in project '{project_slug}'."

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
                workspace_id=ws.id,
                project_id=proj.id,
                identifier=name,
                event="updated",
                payload={"gaps_filed_as": attrs["gaps_filed_as"]},
            )
            conn.commit()

        return (
            f"Marked {len(filed_identifiers)} gap(s) as filed in "
            f"'{name}': {', '.join(filed_identifiers)}"
        )
