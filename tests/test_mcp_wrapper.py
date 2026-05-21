"""Regression: MCP tool wrapper / schema layer tests (decision 20 compliance).

These tests drive the kind servers via stdin/stdout JSON-RPC (the same path
MCP clients use) rather than calling model helpers directly. This catches
schema/wrapper mismatches that model-layer tests miss — e.g. Bug 1 where
the JSON schema required 'identifier' but the model supported auto-allocation.

Pattern follows test_core_tools_audit.py.
"""

from __future__ import annotations

import io
import json
import sys
from datetime import datetime, timezone
from unittest.mock import patch

import numpy as np
import pytest

from agent_notes.core import change_log as cl
from agent_notes.core import db as coredb
from tests.conftest import ephemeral_db  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")


def _rpc(method: str, params: dict | None = None, req_id: int = 1) -> str:
    obj: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        obj["params"] = params
    return json.dumps(obj)


def _drive(server, messages: list[str]) -> list[dict]:
    stdin_text = "\n".join(messages) + "\n"
    orig_stdin, orig_stdout = sys.stdin, sys.stdout
    try:
        sys.stdin = io.StringIO(stdin_text)
        sys.stdout = io.StringIO()
        server.run()
        output = sys.stdout.getvalue()
    finally:
        sys.stdin, sys.stdout = orig_stdin, orig_stdout
    return [json.loads(line) for line in output.strip().split("\n") if line.strip()]


def _fake_embed(text, task="document"):
    return np.zeros(768, dtype=np.float32)


# ---------------------------------------------------------------------------
# Breadcrumb server — stdin-driven wrapper tests
# ---------------------------------------------------------------------------


class TestBreadcrumbMCPWrapper:
    """Drive BreadcrumbServer via JSON-RPC to test wrapper/schema layer."""

    @pytest.fixture(autouse=True)
    def _setup_project(self):
        self.ws = coredb.get_or_create_workspace("bc-mcp-ws", "BC MCP WS")
        self.proj = coredb.get_or_create_project(
            self.ws.id,
            slug="bc-mcp-proj",
            name="BC MCP Proj",
            repo_root="/tmp",
            breadcrumbs_dir="breadcrumbs",
        )
        for name in ("bug", "task", "todo"):
            coredb.add_vocabulary(self.ws.id, "bc_kind", name)
        for name, is_term, is_open, sort in [
            ("new", False, True, 10),
            ("open", False, True, 20),
            ("resolved", True, False, 100),
        ]:
            coredb.add_vocabulary(
                self.ws.id,
                "bc_status",
                name,
                is_terminal=is_term,
                is_open=is_open,
                sort_order=sort,
            )
        for name in ("low", "medium", "high"):
            coredb.add_vocabulary(self.ws.id, "bc_severity", name)

    def test_file_breadcrumb_auto_allocate_identifier(self):
        """Bug 1 regression: file_breadcrumb without identifier auto-allocates."""
        from agent_notes.servers.breadcrumbs import BreadcrumbServer

        server = BreadcrumbServer()
        with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
            responses = _drive(
                server,
                [
                    _rpc("initialize", {"protocolVersion": "2024-11-05"}),
                    _rpc(
                        "tools/call",
                        {
                            "name": "file_breadcrumb",
                            "arguments": {
                                "workspace": "bc-mcp-ws",
                                "project": "bc-mcp-proj",
                                "title": "Auto-allocated BC",
                                "kind": "bug",
                                "status": "new",
                            },
                        },
                        req_id=2,
                    ),
                ],
            )

        tool_resp = responses[-1]
        assert "result" in tool_resp, f"Unexpected response: {tool_resp}"
        text = tool_resp["result"]["content"][0]["text"]
        assert "BC-001" in text
        assert "bug" in text
        assert "new" in text
        assert "bc-mcp-proj" in text

        since = datetime(2000, 1, 1, tzinfo=timezone.utc)
        rows = cl.changes_since(since, workspace_id=self.ws.id, kind="breadcrumb")
        filed = [r for r in rows if r.event == "filed" and r.identifier == "BC-001"]
        assert filed, "file_breadcrumb via MCP must write a change_log row (decision 20)"

    def test_file_breadcrumb_with_explicit_identifier(self):
        """file_breadcrumb with explicit identifier still works after Bug 1 fix."""
        from agent_notes.servers.breadcrumbs import BreadcrumbServer

        server = BreadcrumbServer()
        with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
            responses = _drive(
                server,
                [
                    _rpc("initialize", {"protocolVersion": "2024-11-05"}),
                    _rpc(
                        "tools/call",
                        {
                            "name": "file_breadcrumb",
                            "arguments": {
                                "workspace": "bc-mcp-ws",
                                "project": "bc-mcp-proj",
                                "identifier": "BC-MCP-1",
                                "title": "Explicit ID BC",
                                "kind": "bug",
                                "status": "open",
                            },
                        },
                        req_id=2,
                    ),
                ],
            )

        tool_resp = responses[-1]
        text = tool_resp["result"]["content"][0]["text"]
        assert "BC-MCP-1" in text

        since = datetime(2000, 1, 1, tzinfo=timezone.utc)
        rows = cl.changes_since(since, workspace_id=self.ws.id, kind="breadcrumb")
        filed = [r for r in rows if r.event == "filed" and r.identifier == "BC-MCP-1"]
        assert filed

    def test_file_breadcrumb_schema_identifier_optional(self):
        """Verify the tool schema no longer requires 'identifier'."""
        from agent_notes.servers.breadcrumbs import BreadcrumbServer

        server = BreadcrumbServer()
        tools = server._registry.list_tools()
        file_bc = next(t for t in tools if t["name"] == "file_breadcrumb")
        required = file_bc["inputSchema"]["required"]
        assert "identifier" not in required, (
            "Bug 1: 'identifier' must not be in required array so auto-allocation is reachable"
        )


# ---------------------------------------------------------------------------
# Memory server — stdin-driven wrapper tests
# ---------------------------------------------------------------------------


class TestMemoryMCPWrapper:
    """Drive MemoryServer via JSON-RPC to test wrapper/schema layer."""

    @pytest.fixture(autouse=True)
    def _setup_project(self):
        self.ws = coredb.get_or_create_workspace("mem-mcp-ws", "Mem MCP WS")
        self.proj = coredb.get_or_create_project(
            self.ws.id,
            slug="mem-mcp-proj",
            name="Mem MCP Proj",
        )
        coredb.add_vocabulary(self.ws.id, "memory_type", "note")

    def test_add_memory_writes_change_log(self):
        from agent_notes.servers.memory import MemoryServer

        server = MemoryServer()
        with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
            responses = _drive(
                server,
                [
                    _rpc("initialize", {"protocolVersion": "2024-11-05"}),
                    _rpc(
                        "tools/call",
                        {
                            "name": "add_memory",
                            "arguments": {
                                "workspace": "mem-mcp-ws",
                                "project": "mem-mcp-proj",
                                "name": "mcp-add-test",
                                "memory_type": "note",
                                "body": "Added via JSON-RPC.",
                            },
                        },
                        req_id=2,
                    ),
                ],
            )

        tool_resp = responses[-1]
        text = tool_resp["result"]["content"][0]["text"]
        assert "mcp-add-test" in text

        since = datetime(2000, 1, 1, tzinfo=timezone.utc)
        rows = cl.changes_since(since, workspace_id=self.ws.id, kind="memory")
        filed = [r for r in rows if r.event == "filed" and r.identifier == "mcp-add-test"]
        assert filed, "add_memory via MCP must write a change_log row (decision 20)"

    def test_delete_memory_writes_change_log(self):
        from agent_notes.servers.memory import MemoryServer

        server = MemoryServer()
        with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
            _drive(
                server,
                [
                    _rpc("initialize", {"protocolVersion": "2024-11-05"}),
                    _rpc(
                        "tools/call",
                        {
                            "name": "add_memory",
                            "arguments": {
                                "workspace": "mem-mcp-ws",
                                "project": "mem-mcp-proj",
                                "name": "mcp-del-test",
                                "memory_type": "note",
                                "body": "To be deleted via JSON-RPC.",
                            },
                        },
                        req_id=2,
                    ),
                    _rpc(
                        "tools/call",
                        {
                            "name": "delete_memory",
                            "arguments": {
                                "workspace": "mem-mcp-ws",
                                "project": "mem-mcp-proj",
                                "name": "mcp-del-test",
                            },
                        },
                        req_id=3,
                    ),
                ],
            )

        since = datetime(2000, 1, 1, tzinfo=timezone.utc)
        rows = cl.changes_since(since, workspace_id=self.ws.id, kind="memory")
        deleted = [r for r in rows if r.event == "deleted" and r.identifier == "mcp-del-test"]
        assert deleted, "delete_memory via MCP must write a change_log row (decision 20)"


# ---------------------------------------------------------------------------
# Search server — stdin-driven wrapper tests (Phase 4)
# ---------------------------------------------------------------------------


class TestSearchMCPWrapper:
    """Drive SearchServer via JSON-RPC to test search wrapper/schema layer."""

    @pytest.fixture(autouse=True)
    def _setup_project(self):
        self.ws = coredb.get_or_create_workspace("search-mcp-ws", "Search MCP WS")
        self.proj = coredb.get_or_create_project(
            self.ws.id,
            slug="search-mcp-proj",
            name="Search MCP Proj",
        )
        coredb.add_vocabulary(self.ws.id, "memory_type", "note")
        for name in ("bug", "task"):
            coredb.add_vocabulary(self.ws.id, "bc_kind", name)
        for name in ("new", "open"):
            coredb.add_vocabulary(self.ws.id, "bc_status", name)
        for name in ("low", "medium"):
            coredb.add_vocabulary(self.ws.id, "bc_severity", name)

    def test_search_all_notes_wrapper(self):
        from agent_notes.servers.search import SearchServer

        server = SearchServer()
        with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
            responses = _drive(
                server,
                [
                    _rpc("initialize", {"protocolVersion": "2024-11-05"}),
                    _rpc(
                        "tools/call",
                        {
                            "name": "search_all_notes",
                            "arguments": {
                                "query": "test search",
                                "workspaces": ["search-mcp-ws"],
                            },
                        },
                        req_id=2,
                    ),
                ],
            )

        tool_resp = responses[-1]
        assert "result" in tool_resp, f"Unexpected response: {tool_resp}"
        text = tool_resp["result"]["content"][0]["text"]
        assert isinstance(text, str)

    def test_trace_graph_all_wrapper(self):
        from agent_notes.servers.search import SearchServer

        server = SearchServer()
        with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
            responses = _drive(
                server,
                [
                    _rpc("initialize", {"protocolVersion": "2024-11-05"}),
                    _rpc(
                        "tools/call",
                        {
                            "name": "trace_graph_all",
                            "arguments": {
                                "from_kind": "breadcrumb",
                                "workspace": "search-mcp-ws",
                                "project": "search-mcp-proj",
                                "identifier": "BC-FAKE",
                                "direction": "dependencies",
                            },
                        },
                        req_id=2,
                    ),
                ],
            )

        tool_resp = responses[-1]
        assert "result" in tool_resp, f"Unexpected response: {tool_resp}"
        text = tool_resp["result"]["content"][0]["text"]
        assert isinstance(text, str)


# ---------------------------------------------------------------------------
# Memory server — Phase 5 reflection wrapper tests
# ---------------------------------------------------------------------------


class TestReflectionMCPWrapper:
    """Drive memory server reflection tools via JSON-RPC (Phase 5 wrappers)."""

    @pytest.fixture(autouse=True)
    def _setup_project(self):
        self.ws = coredb.get_or_create_workspace("refl-mcp-ws", "Refl MCP WS")
        self.proj = coredb.get_or_create_project(
            self.ws.id,
            slug="refl-mcp-proj",
            name="Refl MCP Proj",
        )
        coredb.add_vocabulary(self.ws.id, "memory_type", "reflection")
        coredb.add_vocabulary(self.ws.id, "memory_type", "note")

    def test_find_reflections_wrapper(self):
        from agent_notes.servers.memory import MemoryServer

        server = MemoryServer()
        responses = _drive(
            server,
            [
                _rpc("initialize", {"protocolVersion": "2024-11-05"}),
                _rpc(
                    "tools/call",
                    {
                        "name": "find_reflections",
                        "arguments": {
                            "workspace": "refl-mcp-ws",
                            "project": "refl-mcp-proj",
                        },
                    },
                    req_id=2,
                ),
            ],
        )

        tool_resp = responses[-1]
        assert "result" in tool_resp, f"Unexpected response: {tool_resp}"
        text = tool_resp["result"]["content"][0]["text"]
        assert isinstance(text, str)

    def test_extract_gaps_wrapper(self):
        from agent_notes.servers.memory import MemoryServer

        server = MemoryServer()
        with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
            seed = _drive(
                server,
                [
                    _rpc("initialize", {"protocolVersion": "2024-11-05"}),
                    _rpc(
                        "tools/call",
                        {
                            "name": "add_memory",
                            "arguments": {
                                "workspace": "refl-mcp-ws",
                                "project": "refl-mcp-proj",
                                "name": "reflection-mcp-test",
                                "memory_type": "reflection",
                                "body": (
                                    "# Reflection\n\n## Gaps to flag\n- [[BC-MCP-1]]: Test gap.\n"
                                ),
                            },
                        },
                        req_id=2,
                    ),
                ],
            )
            assert "result" in seed[-1]

        server2 = MemoryServer()
        responses = _drive(
            server2,
            [
                _rpc("initialize", {"protocolVersion": "2024-11-05"}),
                _rpc(
                    "tools/call",
                    {
                        "name": "extract_gaps",
                        "arguments": {
                            "workspace": "refl-mcp-ws",
                            "project": "refl-mcp-proj",
                            "name": "reflection-mcp-test",
                        },
                    },
                    req_id=2,
                ),
            ],
        )

        tool_resp = responses[-1]
        text = tool_resp["result"]["content"][0]["text"]
        assert "BC-MCP-1" in text

    def test_mark_gaps_filed_wrapper(self):
        from agent_notes.servers.memory import MemoryServer

        server = MemoryServer()
        with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
            seed = _drive(
                server,
                [
                    _rpc("initialize", {"protocolVersion": "2024-11-05"}),
                    _rpc(
                        "tools/call",
                        {
                            "name": "add_memory",
                            "arguments": {
                                "workspace": "refl-mcp-ws",
                                "project": "refl-mcp-proj",
                                "name": "reflection-mcp-mark",
                                "memory_type": "reflection",
                                "body": "# Reflection\n\n## Gaps to flag\n- [[BC-X]]: Gap.\n",
                            },
                        },
                        req_id=2,
                    ),
                ],
            )
            assert "result" in seed[-1]

        server2 = MemoryServer()
        responses = _drive(
            server2,
            [
                _rpc("initialize", {"protocolVersion": "2024-11-05"}),
                _rpc(
                    "tools/call",
                    {
                        "name": "mark_gaps_filed",
                        "arguments": {
                            "workspace": "refl-mcp-ws",
                            "project": "refl-mcp-proj",
                            "name": "reflection-mcp-mark",
                            "filed_identifiers": ["BC-X"],
                        },
                    },
                    req_id=2,
                ),
            ],
        )

        tool_resp = responses[-1]
        text = tool_resp["result"]["content"][0]["text"]
        assert "Marked 1 gap" in text

        since = datetime(2000, 1, 1, tzinfo=timezone.utc)
        rows = cl.changes_since(since, workspace_id=self.ws.id, kind="memory")
        updated = [
            r for r in rows if r.event == "updated" and r.identifier == "reflection-mcp-mark"
        ]
        assert updated, "mark_gaps_filed via MCP must write a change_log row (decision 20)"
