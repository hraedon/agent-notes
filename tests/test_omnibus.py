"""Omnibus mounting tests (BC-003 regression).

Exercises the omnibus deployment path: mounting breadcrumbs + memory + search
registries into a single Server. Catches tool-name collisions, resource-handler
dedupe gaps, and verifies the merged tool surface is correct.

BC-001 regression: non-core tool collisions are detected and returned.
BC-004 regression: resource handlers are deduplicated by URI prefix.
"""

from __future__ import annotations

import io
import json
import sys
from unittest.mock import patch

import numpy as np
import pytest

from agent_notes.core import db as coredb
from agent_notes.core.mcp import _CORE_TOOL_NAMES, ToolRegistry
from tests.conftest import ephemeral_db  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")


def _fake_embed(text, task="document"):
    return np.zeros(768, dtype=np.float32)


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


# ---------------------------------------------------------------------------
# Unit-level ToolRegistry.merge tests
# ---------------------------------------------------------------------------


class TestToolRegistryMerge:
    """Unit tests for ToolRegistry.merge collision handling (BC-001, BC-004)."""

    def test_core_tools_deduped_silently(self):
        r1 = ToolRegistry()
        r1.register("list_workspaces", {"description": "a"}, lambda args: "a")
        r2 = ToolRegistry()
        r2.register("list_workspaces", {"description": "a"}, lambda args: "b")
        collisions = r1.merge(r2)
        assert collisions == []
        assert len(r1.list_tools()) == 1

    def test_non_core_collision_returned(self):
        r1 = ToolRegistry()
        r1.register("trace_graph", {"description": "bc"}, lambda args: "bc")
        r2 = ToolRegistry()
        r2.register("trace_graph", {"description": "mem"}, lambda args: "mem")
        collisions = r1.merge(r2)
        assert collisions == ["trace_graph"]
        assert r1.call("trace_graph", {}) == "bc"

    def test_non_colliding_tools_merged(self):
        r1 = ToolRegistry()
        r1.register("list_workspaces", {"description": "a"}, lambda args: "a")
        r2 = ToolRegistry()
        r2.register("list_workspaces", {"description": "a"}, lambda args: "a2")
        r2.register("tool_b", {"description": "b"}, lambda args: "b")
        r2.register("trace_graph", {"description": "mem"}, lambda args: "mem")
        collisions = r1.merge(r2)
        assert collisions == []
        assert len(r1.list_tools()) == 3
        assert r1.call("tool_b", {}) == "b"
        assert r1.call("trace_graph", {}) == "mem"

    def test_resource_handlers_deduped_by_prefix(self):
        r1 = ToolRegistry()

        def handler_a(action, uri):
            return None

        r1.register_resource_handler("note://breadcrumb/", handler_a)
        r2 = ToolRegistry()
        r2.register_resource_handler("note://breadcrumb/", handler_a)
        r2.register_resource_handler("note://memory/", handler_a)
        r1.merge(r2)
        assert len(r1.list_resource_handlers()) == 2

    def test_merge_returns_empty_for_no_collisions(self):
        r1 = ToolRegistry()
        r1.register("tool_a", {"description": "a"}, lambda args: "a")
        r2 = ToolRegistry()
        r2.register("tool_b", {"description": "b"}, lambda args: "b")
        collisions = r1.merge(r2)
        assert collisions == []
        assert len(r1.list_tools()) == 2

    def test_multiple_collisions_returned(self):
        r1 = ToolRegistry()
        r1.register("trace_graph", {"description": "bc"}, lambda args: "bc")
        r1.register("custom_tool", {"description": "x"}, lambda args: "x")
        r2 = ToolRegistry()
        r2.register("trace_graph", {"description": "mem"}, lambda args: "mem")
        r2.register("custom_tool", {"description": "y"}, lambda args: "y")
        collisions = r1.merge(r2)
        assert set(collisions) == {"trace_graph", "custom_tool"}


# ---------------------------------------------------------------------------
# Integration-level omnibus mounting tests
# ---------------------------------------------------------------------------


class TestOmnibusMounting:
    """BC-003: end-to-end omnibus mounting of breadcrumbs + memory + search."""

    @pytest.fixture(autouse=True)
    def _setup_project(self):
        self.ws = coredb.get_or_create_workspace("omnibus-ws", "Omnibus WS")
        self.proj = coredb.get_or_create_project(
            self.ws.id,
            slug="omnibus-proj",
            name="Omnibus Proj",
            repo_root="/tmp",
            breadcrumbs_dir="breadcrumbs",
        )
        for name in ("bug", "task"):
            coredb.add_vocabulary(self.ws.id, "bc_kind", name)
        for name, is_term, is_open, sort in [
            ("new", False, True, 10),
            ("open", False, True, 20),
            ("resolved", True, False, 100),
        ]:
            coredb.add_vocabulary(
                self.ws.id, "bc_status", name,
                is_terminal=is_term, is_open=is_open, sort_order=sort,
            )
        for name in ("low", "medium", "high"):
            coredb.add_vocabulary(self.ws.id, "bc_severity", name)
        coredb.add_vocabulary(self.ws.id, "memory_type", "note")

    def _build_omnibus(self):
        from agent_notes.core.server import Server
        from agent_notes.servers.breadcrumbs import BreadcrumbServer
        from agent_notes.servers.memory import MemoryServer
        from agent_notes.servers.search import SearchServer

        server = Server()
        server.merge_registry(BreadcrumbServer())
        server.merge_registry(MemoryServer())
        server.merge_registry(SearchServer())
        return server

    def test_trace_graph_collision_detected(self):
        from agent_notes.core.server import Server
        from agent_notes.servers.breadcrumbs import BreadcrumbServer
        from agent_notes.servers.memory import MemoryServer

        server = Server()
        server.merge_registry(BreadcrumbServer())
        collisions = server.merge_registry(MemoryServer())
        assert "trace_graph" in collisions

    def test_core_tools_appear_once(self):
        server = self._build_omnibus()
        tools = server._registry.list_tools()
        core_count = {}
        for t in tools:
            name = t["name"]
            if name in _CORE_TOOL_NAMES:
                core_count[name] = core_count.get(name, 0) + 1
        for name, count in core_count.items():
            assert count == 1, f"Core tool {name!r} registered {count} times"

    def test_resource_handlers_deduped(self):
        server = self._build_omnibus()
        handlers = server._registry.list_resource_handlers()
        prefixes = [prefix for prefix, _ in handlers]
        assert len(prefixes) == len(set(prefixes)), (
            f"Duplicate resource handler prefixes: {prefixes}"
        )

    def test_kind_specific_tools_present(self):
        server = self._build_omnibus()
        tool_names = {t["name"] for t in server._registry.list_tools()}
        assert "file_breadcrumb" in tool_names
        assert "add_memory" in tool_names
        assert "search_all_notes" in tool_names
        assert "trace_graph_all" in tool_names

    def test_omnibus_dispatch_breadcrumb(self):
        server = self._build_omnibus()
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
                                "workspace": "omnibus-ws",
                                "project": "omnibus-proj",
                                "identifier": "OMNI-BC-1",
                                "title": "Omnibus breadcrumb",
                                "kind": "bug",
                                "status": "new",
                            },
                        },
                        req_id=2,
                    ),
                ],
            )
        assert "result" in responses[-1]
        text = responses[-1]["result"]["content"][0]["text"]
        assert "OMNI-BC-1" in text

    def test_omnibus_dispatch_memory(self):
        server = self._build_omnibus()
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
                                "workspace": "omnibus-ws",
                                "project": "omnibus-proj",
                                "name": "omni-mem-1",
                                "memory_type": "note",
                                "body": "Omnibus memory test.",
                            },
                        },
                        req_id=2,
                    ),
                ],
            )
        assert "result" in responses[-1]
        text = responses[-1]["result"]["content"][0]["text"]
        assert "omni-mem-1" in text

    def test_omnibus_dispatch_search(self):
        server = self._build_omnibus()
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
                                "query": "test",
                                "workspaces": ["omnibus-ws"],
                            },
                        },
                        req_id=2,
                    ),
                ],
            )
        assert "result" in responses[-1]

    def test_file_breadcrumb_no_file_path_in_schema(self):
        """BC-005 regression: file_path must not appear in tool schema."""
        from agent_notes.servers.breadcrumbs import BreadcrumbServer

        server = BreadcrumbServer()
        tools = server._registry.list_tools()
        file_bc = next(t for t in tools if t["name"] == "file_breadcrumb")
        props = file_bc["inputSchema"]["properties"]
        assert "file_path" not in props
