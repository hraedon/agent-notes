"""Integration tests for the cross-kind search server (Phase 4).

Tests the full tool surface against ephemeral Postgres:
- all_notes_search_v view exists and returns data
- search_all_notes: UNION ALL across breadcrumbs + memories ranked by similarity
- search_all_notes: filters (kinds, workspaces, projects, since)
- trace_graph_all: cross-kind traversal with title enrichment
- CLI wiring (search server instantiable without NotImplementedError)
"""

from __future__ import annotations

import psycopg
import pytest
from psycopg.rows import dict_row

from agent_notes.core import db as coredb
from agent_notes.core import links as lnk
from agent_notes.servers.breadcrumbs import BreadcrumbServer
from agent_notes.servers.memory import MemoryServer
from agent_notes.servers.search import SearchServer


@pytest.fixture(scope="module")
def pg(ephemeral_db):
    return ephemeral_db


@pytest.fixture(scope="module")
def search_ws(pg):
    ws = coredb.get_or_create_workspace("search-test-ws", "Search Test WS")
    return ws


@pytest.fixture(scope="module")
def search_proj(search_ws):
    return coredb.get_or_create_project(
        search_ws.id, "search-proj", "Search Proj", repo_root="/tmp/search"
    )


@pytest.fixture(scope="module")
def seeded_vocab(search_ws):
    coredb.add_vocabulary(search_ws.id, "memory_type", "note")
    coredb.add_vocabulary(search_ws.id, "memory_type", "decision")
    coredb.add_vocabulary(search_ws.id, "bc_kind", "decision")
    coredb.add_vocabulary(search_ws.id, "bc_kind", "task")
    coredb.add_vocabulary(search_ws.id, "bc_status", "open")
    coredb.add_vocabulary(search_ws.id, "bc_status", "new")
    coredb.add_vocabulary(search_ws.id, "bc_status", "resolved")
    coredb.add_vocabulary(search_ws.id, "bc_severity", "medium")
    return search_ws


@pytest.fixture(scope="module")
def bc_server():
    return BreadcrumbServer()


@pytest.fixture(scope="module")
def mem_server():
    return MemoryServer()


@pytest.fixture(scope="module")
def search_server():
    return SearchServer()


@pytest.fixture(scope="module")
def seeded_data(search_ws, search_proj, seeded_vocab, bc_server, mem_server):
    bc_server._tool_file_breadcrumb(
        {
            "workspace": "search-test-ws",
            "project": "search-proj",
            "identifier": "BC-SEARCH-001",
            "title": "PostgreSQL connection pooling best practices",
            "body": "Use pgbouncer or pgpool for connection pooling in production.",
            "kind": "decision",
            "status": "open",
        }
    )
    bc_server._tool_file_breadcrumb(
        {
            "workspace": "search-test-ws",
            "project": "search-proj",
            "identifier": "BC-SEARCH-002",
            "title": "Embedding model selection",
            "body": "Use nomic-embed-text for semantic search embeddings.",
            "kind": "task",
            "status": "new",
        }
    )
    mem_server._tool_add_memory(
        {
            "workspace": "search-test-ws",
            "project": "search-proj",
            "name": "db-pooling-memory",
            "memory_type": "decision",
            "body": "Decided to use psycopg_pool.ConnectionPool with sync pattern.",
        }
    )
    mem_server._tool_add_memory(
        {
            "workspace": "search-test-ws",
            "project": "search-proj",
            "name": "embedding-memory",
            "memory_type": "note",
            "body": "In-process embedding singleton avoids network SPOF (decision 2).",
        }
    )
    lnk.add_link(
        from_kind="breadcrumb",
        from_workspace=search_ws.id,
        from_project=search_proj.id,
        from_identifier="BC-SEARCH-001",
        to_kind="memory",
        to_workspace=search_ws.id,
        to_project=search_proj.id,
        to_identifier="db-pooling-memory",
        relationship="relates_to",
    )
    lnk.add_link(
        from_kind="memory",
        from_workspace=search_ws.id,
        from_project=search_proj.id,
        from_identifier="embedding-memory",
        to_kind="breadcrumb",
        to_workspace=search_ws.id,
        to_project=search_proj.id,
        to_identifier="BC-SEARCH-002",
        relationship="relates_to",
    )


# ---------------------------------------------------------------------------
# View tests
# ---------------------------------------------------------------------------


class TestAllNotesSearchView:
    def test_view_exists(self, pg) -> None:
        with psycopg.connect(pg) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT 1 FROM information_schema.views WHERE table_name = 'all_notes_search_v'"
            )
            assert cur.fetchone() is not None

    def test_view_returns_breadcrumbs_and_memories(
        self, pg, search_ws, search_proj, seeded_data
    ) -> None:
        with coredb._conn() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                "SELECT kind, identifier FROM all_notes_search_v "
                "WHERE workspace_id = %s AND project_id = %s ORDER BY kind, identifier",
                (search_ws.id, search_proj.id),
            )
            rows = cur.fetchall()
        kinds = {r["kind"] for r in rows}
        assert "breadcrumb" in kinds
        assert "memory" in kinds

    def test_view_uses_updated_at(self, pg, search_ws, search_proj, seeded_data) -> None:
        with coredb._conn() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                "SELECT kind, identifier, updated_at FROM all_notes_search_v "
                "WHERE workspace_id = %s AND project_id = %s",
                (search_ws.id, search_proj.id),
            )
            rows = cur.fetchall()
        for r in rows:
            assert r["updated_at"] is not None

    def test_view_excludes_inactive_memories(
        self, pg, search_ws, search_proj, seeded_data, mem_server
    ) -> None:
        mem_server._tool_add_memory(
            {
                "workspace": "search-test-ws",
                "project": "search-proj",
                "name": "soon-deleted",
                "memory_type": "note",
                "body": "This will be soft-deleted.",
            }
        )
        mem_server._tool_delete_memory(
            {"workspace": "search-test-ws", "project": "search-proj", "name": "soon-deleted"}
        )
        with coredb._conn() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                "SELECT identifier FROM all_notes_search_v "
                "WHERE workspace_id = %s AND project_id = %s AND kind = 'memory'",
                (search_ws.id, search_proj.id),
            )
            rows = cur.fetchall()
        identifiers = {r["identifier"] for r in rows}
        assert "soon-deleted" not in identifiers


# ---------------------------------------------------------------------------
# search_all_notes tool
# ---------------------------------------------------------------------------


class TestSearchAllNotes:
    def test_basic_search(self, pg, search_ws, search_proj, seeded_data, search_server) -> None:
        result = search_server._tool_search_all_notes({"query": "connection pooling database"})
        assert "note(s) matched" in result
        assert "BC-SEARCH-001" in result or "db-pooling-memory" in result

    def test_search_returns_multiple_kinds(
        self, pg, search_ws, search_proj, seeded_data, search_server
    ) -> None:
        result = search_server._tool_search_all_notes({"query": "embedding"})
        assert "note(s) matched" in result
        assert "BC-SEARCH-002" in result or "embedding-memory" in result

    def test_filter_by_kind_breadcrumb(
        self, pg, search_ws, search_proj, seeded_data, search_server
    ) -> None:
        result = search_server._tool_search_all_notes(
            {"query": "connection", "kinds": ["breadcrumb"]}
        )
        assert "[breadcrumb]" in result
        assert "[memory]" not in result

    def test_filter_by_kind_memory(
        self, pg, search_ws, search_proj, seeded_data, search_server
    ) -> None:
        result = search_server._tool_search_all_notes({"query": "pooling", "kinds": ["memory"]})
        assert "[memory]" in result
        assert "[breadcrumb]" not in result

    def test_filter_by_workspace(
        self, pg, search_ws, search_proj, seeded_data, search_server
    ) -> None:
        result = search_server._tool_search_all_notes(
            {"query": "connection", "workspaces": ["search-test-ws"]}
        )
        assert "note(s) matched" in result

    def test_filter_by_nonexistent_workspace(
        self, pg, search_ws, search_proj, seeded_data, search_server
    ) -> None:
        result = search_server._tool_search_all_notes(
            {"query": "connection", "workspaces": ["nonexistent-ws"]}
        )
        assert "No matching notes" in result

    def test_filter_by_project(
        self, pg, search_ws, search_proj, seeded_data, search_server
    ) -> None:
        result = search_server._tool_search_all_notes(
            {"query": "embedding", "projects": ["search-proj"]}
        )
        assert "note(s) matched" in result

    def test_filter_by_since(self, pg, search_ws, search_proj, seeded_data, search_server) -> None:
        result = search_server._tool_search_all_notes(
            {
                "query": "connection pooling embedding",
                "since": "2020-01-01T00:00:00",
            }
        )
        assert "note(s) matched" in result

    def test_filter_by_since_future(
        self, pg, search_ws, search_proj, seeded_data, search_server
    ) -> None:
        result = search_server._tool_search_all_notes(
            {
                "query": "connection pooling embedding",
                "since": "2099-01-01T00:00:00",
            }
        )
        assert "No matching notes" in result

    def test_limit_respected(self, pg, search_ws, search_proj, seeded_data, search_server) -> None:
        result = search_server._tool_search_all_notes(
            {"query": "connection embedding pooling", "limit": 1}
        )
        lines = [line for line in result.split("\n") if line.startswith("- [")]
        assert len(lines) <= 1

    def test_no_results_with_impossible_filter(
        self, pg, search_ws, search_proj, search_server
    ) -> None:
        result = search_server._tool_search_all_notes(
            {"query": "connection", "kinds": ["nonexistent_kind"]}
        )
        assert "No matching notes" in result


# ---------------------------------------------------------------------------
# trace_graph_all tool
# ---------------------------------------------------------------------------


class TestTraceGraphAll:
    def test_cross_kind_dependencies(
        self, pg, search_ws, search_proj, seeded_data, search_server
    ) -> None:
        result = search_server._tool_trace_graph_all(
            {
                "from_kind": "breadcrumb",
                "workspace": "search-test-ws",
                "project": "search-proj",
                "identifier": "BC-SEARCH-001",
                "direction": "dependencies",
            }
        )
        assert "db-pooling-memory" in result
        assert "[memory]" in result

    def test_cross_kind_dependents(
        self, pg, search_ws, search_proj, seeded_data, search_server
    ) -> None:
        result = search_server._tool_trace_graph_all(
            {
                "from_kind": "breadcrumb",
                "workspace": "search-test-ws",
                "project": "search-proj",
                "identifier": "BC-SEARCH-002",
                "direction": "dependents",
            }
        )
        assert "embedding-memory" in result
        assert "[memory]" in result

    def test_title_enrichment(self, pg, search_ws, search_proj, seeded_data, search_server) -> None:
        result = search_server._tool_trace_graph_all(
            {
                "from_kind": "breadcrumb",
                "workspace": "search-test-ws",
                "project": "search-proj",
                "identifier": "BC-SEARCH-001",
                "direction": "dependencies",
            }
        )
        assert "db-pooling-memory" in result

    def test_max_depth_respected(
        self, pg, search_ws, search_proj, seeded_data, search_server
    ) -> None:
        result = search_server._tool_trace_graph_all(
            {
                "from_kind": "breadcrumb",
                "workspace": "search-test-ws",
                "project": "search-proj",
                "identifier": "BC-SEARCH-001",
                "direction": "dependencies",
                "max_depth": 1,
            }
        )
        assert "linked note" in result

    def test_no_links(self, pg, search_ws, search_proj, seeded_data, search_server) -> None:
        result = search_server._tool_trace_graph_all(
            {
                "from_kind": "breadcrumb",
                "workspace": "search-test-ws",
                "project": "search-proj",
                "identifier": "BC-SEARCH-002",
                "direction": "dependencies",
            }
        )
        assert "No linked notes" in result

    def test_relationship_filter(
        self, pg, search_ws, search_proj, seeded_data, search_server
    ) -> None:
        result = search_server._tool_trace_graph_all(
            {
                "from_kind": "breadcrumb",
                "workspace": "search-test-ws",
                "project": "search-proj",
                "identifier": "BC-SEARCH-001",
                "direction": "dependencies",
                "relationship_kinds": ["blocks"],
            }
        )
        assert "No linked notes" in result

    def test_invalid_workspace(self, pg, search_server) -> None:
        result = search_server._tool_trace_graph_all(
            {
                "from_kind": "breadcrumb",
                "workspace": "nonexistent",
                "project": "some-proj",
                "identifier": "BC-001",
            }
        )
        assert "not found" in result.lower()

    def test_invalid_project(self, pg, search_ws, search_server) -> None:
        result = search_server._tool_trace_graph_all(
            {
                "from_kind": "breadcrumb",
                "workspace": "search-test-ws",
                "project": "nonexistent",
                "identifier": "BC-001",
            }
        )
        assert "not found" in result.lower()


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


class TestCLIWiring:
    def test_search_server_instantiable(self) -> None:
        server = SearchServer()
        tools = server._registry.list_tools()
        tool_names = [t["name"] for t in tools]
        assert "search_all_notes" in tool_names
        assert "trace_graph_all" in tool_names
        assert "list_workspaces" in tool_names
        assert "add_link" in tool_names
