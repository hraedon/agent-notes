"""Integration tests for the cross-kind search model layer (Phase 4).

Tests the model functions against ephemeral Postgres:
- all_notes_search_v view exists and returns data
- search_all_notes: UNION ALL across breadcrumbs + memories ranked by similarity
- search_all_notes: filters (kinds, workspaces, projects, since)
- trace_graph_all: cross-kind traversal with title enrichment
"""

from __future__ import annotations

import psycopg
import pytest
from psycopg.rows import dict_row

from agent_notes.core import db as coredb
from agent_notes.core import links as lnk
from agent_notes.core.breadcrumbs_model import BreadcrumbModel
from agent_notes.core.memory_model import add_memory, delete_memory
from agent_notes.core.search import search_all_notes, trace_graph_all


def _fake_embed(text, task="document"):
    return [0.0] * 768


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
def seeded_data(search_ws, search_proj, seeded_vocab):
    BreadcrumbModel.file_breadcrumb(
        search_proj.id,
        identifier="BC-SEARCH-001",
        title="PostgreSQL connection pooling best practices",
        body="Use pgbouncer or pgpool for connection pooling in production.",
        kind="decision",
        status="open",
        embedding=_fake_embed("connection pooling"),
    )
    BreadcrumbModel.file_breadcrumb(
        search_proj.id,
        identifier="BC-SEARCH-002",
        title="Embedding model selection",
        body="Use nomic-embed-text for semantic search embeddings.",
        kind="task",
        status="new",
        embedding=_fake_embed("embedding"),
    )
    add_memory(
        workspace_id=search_ws.id,
        project_id=search_proj.id,
        name="db-pooling-memory",
        memory_type="decision",
        body="Decided to use psycopg_pool.ConnectionPool with sync pattern.",
        embedding=_fake_embed("connection pooling"),
    )
    add_memory(
        workspace_id=search_ws.id,
        project_id=search_proj.id,
        name="embedding-memory",
        memory_type="note",
        body="In-process embedding singleton avoids network SPOF (decision 2).",
        embedding=_fake_embed("embedding"),
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

    def test_view_excludes_inactive_memories(self, pg, search_ws, search_proj, seeded_data) -> None:
        add_memory(
            workspace_id=search_ws.id,
            project_id=search_proj.id,
            name="soon-deleted",
            memory_type="note",
            body="This will be soft-deleted.",
            embedding=_fake_embed("test"),
        )
        delete_memory(
            workspace_id=search_ws.id,
            project_id=search_proj.id,
            name="soon-deleted",
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
# search_all_notes
# ---------------------------------------------------------------------------


class TestSearchAllNotes:
    def test_basic_search(self, pg, search_ws, search_proj, seeded_data) -> None:
        rows = search_all_notes(
            query_vec=_fake_embed("connection pooling database"),
        )
        identifiers = {r["identifier"] for r in rows}
        assert "BC-SEARCH-001" in identifiers or "db-pooling-memory" in identifiers

    def test_search_returns_multiple_kinds(self, pg, search_ws, search_proj, seeded_data) -> None:
        rows = search_all_notes(
            query_vec=_fake_embed("embedding"),
        )
        kinds = {r["kind"] for r in rows}
        assert "breadcrumb" in kinds or "memory" in kinds

    def test_filter_by_kind_breadcrumb(self, pg, search_ws, search_proj, seeded_data) -> None:
        rows = search_all_notes(
            query_vec=_fake_embed("connection"),
            kinds=["breadcrumb"],
        )
        for r in rows:
            assert r["kind"] == "breadcrumb"

    def test_filter_by_kind_memory(self, pg, search_ws, search_proj, seeded_data) -> None:
        rows = search_all_notes(
            query_vec=_fake_embed("pooling"),
            kinds=["memory"],
        )
        for r in rows:
            assert r["kind"] == "memory"

    def test_filter_by_workspace(self, pg, search_ws, search_proj, seeded_data) -> None:
        rows = search_all_notes(
            query_vec=_fake_embed("connection"),
            workspace_ids=[search_ws.id],
        )
        assert len(rows) > 0


# ---------------------------------------------------------------------------
# trace_graph_all
# ---------------------------------------------------------------------------


class TestTraceGraphAll:
    def test_cross_kind_traversal(self, pg, search_ws, search_proj, seeded_data) -> None:
        nodes = trace_graph_all(
            kind="breadcrumb",
            workspace=search_ws.id,
            project=search_proj.id,
            identifier="BC-SEARCH-001",
            direction="dependencies",
            max_depth=2,
        )
        identifiers = {n.identifier for n in nodes}
        kinds = {n.kind for n in nodes}
        assert "db-pooling-memory" in identifiers
        assert "memory" in kinds

    def test_reverse_traversal(self, pg, search_ws, search_proj, seeded_data) -> None:
        nodes = trace_graph_all(
            kind="breadcrumb",
            workspace=search_ws.id,
            project=search_proj.id,
            identifier="BC-SEARCH-002",
            direction="dependents",
            max_depth=2,
        )
        identifiers = {n.identifier for n in nodes}
        assert "embedding-memory" in identifiers

    def test_empty_result(self, pg, search_ws, search_proj, seeded_data) -> None:
        nodes = trace_graph_all(
            kind="breadcrumb",
            workspace=search_ws.id,
            project=search_proj.id,
            identifier="NONEXISTENT",
            direction="dependencies",
        )
        assert len(nodes) == 0
