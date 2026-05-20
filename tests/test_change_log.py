"""Integration tests for core/change_log.py against real Postgres."""

from __future__ import annotations

from datetime import datetime, timezone

import psycopg
import pytest

from agent_notes.core import change_log as cl
from agent_notes.core import db as coredb


@pytest.fixture(scope="module")
def pg(ephemeral_db):
    """Module-scoped alias so tests can request ephemeral_db via the module fixture."""
    return ephemeral_db


class TestWriteChange:
    def test_write_change_returns_id(self, pg: str) -> None:
        ws = coredb.get_or_create_workspace("cl-write-ws", "CL Write WS")
        p = coredb.get_or_create_project(ws.id, "cl-write-proj", "CL Write Proj")
        with psycopg.connect(pg) as conn:
            row_id = cl.write_change(
                conn,
                kind="breadcrumb",
                workspace_id=ws.id,
                project_id=p.id,
                identifier="BC-CL-001",
                event="filed",
                payload={"title": "test"},
                actor="test-agent",
            )
            conn.commit()
        assert isinstance(row_id, int)
        assert row_id > 0

    def test_write_change_payload_persisted(self, pg: str) -> None:
        ws = coredb.get_or_create_workspace("cl-payload-ws", "CL Payload WS")
        p = coredb.get_or_create_project(ws.id, "cl-payload-proj", "CL Payload Proj")
        with psycopg.connect(pg) as conn:
            cl.write_change(
                conn,
                kind="memory",
                workspace_id=ws.id,
                project_id=p.id,
                identifier="mem-payload-001",
                event="filed",
                payload={"note": "integration test"},
            )
            conn.commit()
        since = datetime(2000, 1, 1, tzinfo=timezone.utc)
        rows = cl.changes_since(since, workspace_id=ws.id, kind="memory")
        assert any(r.identifier == "mem-payload-001" for r in rows)
        target = next(r for r in rows if r.identifier == "mem-payload-001")
        assert target.payload == {"note": "integration test"}


class TestChangesSince:
    def test_filter_by_kind(self, pg: str) -> None:
        ws = coredb.get_or_create_workspace("cl-kind-ws", "CL Kind WS")
        p = coredb.get_or_create_project(ws.id, "cl-kind-proj", "CL Kind Proj")
        with psycopg.connect(pg) as conn:
            cl.write_change(conn, "breadcrumb", ws.id, p.id, "BC-KIND-1", "filed")
            cl.write_change(conn, "memory", ws.id, p.id, "MEM-KIND-1", "filed")
            conn.commit()
        since = datetime(2000, 1, 1, tzinfo=timezone.utc)
        bc_rows = cl.changes_since(since, workspace_id=ws.id, kind="breadcrumb")
        mem_rows = cl.changes_since(since, workspace_id=ws.id, kind="memory")
        assert all(r.kind == "breadcrumb" for r in bc_rows)
        assert all(r.kind == "memory" for r in mem_rows)

    def test_filter_by_project(self, pg: str) -> None:
        ws = coredb.get_or_create_workspace("cl-proj-ws", "CL Proj WS")
        p1 = coredb.get_or_create_project(ws.id, "p1", "Project 1")
        p2 = coredb.get_or_create_project(ws.id, "p2", "Project 2")
        with psycopg.connect(pg) as conn:
            cl.write_change(conn, "bc", ws.id, p1.id, "X1", "filed")
            cl.write_change(conn, "bc", ws.id, p2.id, "X2", "filed")
            conn.commit()
        since = datetime(2000, 1, 1, tzinfo=timezone.utc)
        rows = cl.changes_since(since, workspace_id=ws.id, project_id=p1.id)
        identifiers = {r.identifier for r in rows}
        assert "X1" in identifiers
        assert "X2" not in identifiers

    def test_limit_respected(self, pg: str) -> None:
        ws = coredb.get_or_create_workspace("cl-limit-ws", "CL Limit WS")
        p = coredb.get_or_create_project(ws.id, "cl-limit-proj", "CL Limit Proj")
        with psycopg.connect(pg) as conn:
            for i in range(10):
                cl.write_change(conn, "bc", ws.id, p.id, f"LIMIT-{i}", "filed")
            conn.commit()
        since = datetime(2000, 1, 1, tzinfo=timezone.utc)
        rows = cl.changes_since(since, workspace_id=ws.id, limit=3)
        assert len(rows) <= 3


class TestHistory:
    def test_history_chronological(self, pg: str) -> None:
        ws = coredb.get_or_create_workspace("hist-ws", "History WS")
        p = coredb.get_or_create_project(ws.id, "hist-proj", "History Proj")
        with psycopg.connect(pg) as conn:
            cl.write_change(conn, "breadcrumb", ws.id, p.id, "HIST-001", "filed")
            cl.write_change(conn, "breadcrumb", ws.id, p.id, "HIST-001", "updated")
            cl.write_change(conn, "breadcrumb", ws.id, p.id, "HIST-001", "status_changed")
            conn.commit()
        rows = cl.history("breadcrumb", ws.id, p.id, "HIST-001")
        assert len(rows) >= 3
        events = [r.event for r in rows]
        # History is chronological: filed before updated before status_changed.
        assert events.index("filed") < events.index("updated") < events.index("status_changed")

    def test_history_isolates_identifier(self, pg: str) -> None:
        ws = coredb.get_or_create_workspace("hist-iso-ws", "History Iso WS")
        p = coredb.get_or_create_project(ws.id, "hist-iso-proj", "History Iso Proj")
        with psycopg.connect(pg) as conn:
            cl.write_change(conn, "bc", ws.id, p.id, "ISO-A", "filed")
            cl.write_change(conn, "bc", ws.id, p.id, "ISO-B", "filed")
            conn.commit()
        rows_a = cl.history("bc", ws.id, p.id, "ISO-A")
        assert all(r.identifier == "ISO-A" for r in rows_a)
