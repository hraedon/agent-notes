"""Integration tests for Plan 008 P0 — work-log coordination kernel."""

from __future__ import annotations

import argparse
import json

import pytest

from agent_notes.core import db as coredb
from agent_notes.core import kernel
from agent_notes.core.work_item_model import WorkItemModel

# Import the fixture so pytest discovers it
from tests.conftest import ephemeral_db  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")


@pytest.fixture
def default_project():
    ws = coredb.get_or_create_workspace("default", "Default Workspace")
    proj = coredb.get_or_create_project(
        ws.id,
        slug="sf2",
        name="sf2",
        repo_root="/projects/sf2",
    )
    return proj


def _vec768():
    """Return a dummy 768-dim numpy-compatible vector (float list)."""
    return [0.0] * 768


# ---------------------------------------------------------------------------
# Kernel primitives
# ---------------------------------------------------------------------------


class TestKernelOps:
    def test_content_hash_idempotent(self):
        h1 = kernel.content_hash("hello world")
        h2 = kernel.content_hash("hello world")
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex

    def test_store_and_retrieve_blob(self, default_project):
        with kernel._conn() as conn:
            h = kernel.store_blob(conn, "test body content")
            assert h == kernel.content_hash("test body content")
            retrieved = kernel.get_blob(conn, h)
            assert retrieved == "test body content"

    def test_store_blob_idempotent(self, default_project):
        with kernel._conn() as conn:
            h1 = kernel.store_blob(conn, "duplicate content")
            h2 = kernel.store_blob(conn, "duplicate content")
            assert h1 == h2


# ---------------------------------------------------------------------------
# Work-item CRUD
# ---------------------------------------------------------------------------


class TestWorkItemFile:
    def test_file_work_item_roundtrip(self, default_project):
        wi = WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-001",
            title="Test WI",
            body="Body text",
            kind="bug",
            status="open",
            severity="high",
            embedding=_vec768(),
        )
        assert wi["identifier"] == "WI-001"
        assert wi["title"] == "Test WI"
        assert wi["status"] == "open"

        fetched = WorkItemModel.get_work_item(default_project.id, "WI-001")
        assert fetched is not None
        assert fetched["title"] == "Test WI"

    def test_file_work_item_auto_identifier(self, default_project):
        wi = WorkItemModel.file_work_item(
            project_id=default_project.id,
            title="Auto ID",
            embedding=_vec768(),
        )
        assert wi["identifier"].startswith("WI-")
        assert wi["identifier"] != "WI-001"  # Should be the first auto one

    def test_file_work_item_unknown_vocab(self, default_project):
        with pytest.raises(ValueError, match="Unknown wi_status"):
            WorkItemModel.file_work_item(
                project_id=default_project.id,
                identifier="WI-002",
                title="Bad status",
                status="nonexistent",
                embedding=_vec768(),
            )

    def test_file_work_item_body_as_blob(self, default_project):
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-003",
            title="Blob test",
            body="This is a content-addressed body",
            embedding=_vec768(),
        )
        body = WorkItemModel.get_work_item_body(default_project.id, "WI-003")
        assert body == "This is a content-addressed body"


class TestWorkItemUpdate:
    def test_update_status(self, default_project):
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-004",
            title="To be closed",
            status="open",
            embedding=_vec768(),
        )
        updated = WorkItemModel.update_work_item(
            project_id=default_project.id,
            identifier="WI-004",
            status="closed",
        )
        assert updated["status"] == "closed"
        assert updated["closed_at"] is not None

    def test_update_body_relogs_as_blob(self, default_project):
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-005",
            title="Body update",
            body="Original body",
            embedding=_vec768(),
        )
        WorkItemModel.update_work_item(
            project_id=default_project.id,
            identifier="WI-005",
            body="Updated body",
        )
        body = WorkItemModel.get_work_item_body(default_project.id, "WI-005")
        assert body == "Updated body"

    def test_update_title_and_embedding(self, default_project):
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-006",
            title="Old title",
            body="Body text",
            embedding=_vec768(),
        )
        updated = WorkItemModel.update_work_item(
            project_id=default_project.id,
            identifier="WI-006",
            title="New title",
        )
        assert updated["title"] == "New title"


class TestWorkItemClose:
    def test_close_work_item(self, default_project):
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-007",
            title="To close",
            status="open",
            embedding=_vec768(),
        )
        closed = WorkItemModel.close_work_item(default_project.id, "WI-007")
        assert closed["status"] == "closed"
        assert closed["closed_at"] is not None

    def test_close_nonexistent_raises(self, default_project):
        with pytest.raises(ValueError, match="not found"):
            WorkItemModel.close_work_item(default_project.id, "WI-NONEXISTENT")


class TestWorkItemDelete:
    def test_delete_work_item(self, default_project):
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-008",
            title="To delete",
            embedding=_vec768(),
        )
        deleted = WorkItemModel.delete_work_item(default_project.id, "WI-008")
        assert deleted is True
        assert WorkItemModel.get_work_item(default_project.id, "WI-008") is None


class TestWorkItemQuery:
    def test_query_by_status(self, default_project):
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-009",
            title="Open one",
            status="open",
            embedding=_vec768(),
        )
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-010",
            title="Closed one",
            status="closed",
            embedding=_vec768(),
        )
        rows = WorkItemModel.query_work_items(project_id=default_project.id, status="closed")
        identifiers = {r["identifier"] for r in rows}
        assert "WI-010" in identifiers

    def test_query_open_filter(self, default_project):
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-011",
            title="Open",
            status="open",
            embedding=_vec768(),
        )
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-012",
            title="Closed",
            status="closed",
            embedding=_vec768(),
        )
        open_rows = WorkItemModel.query_work_items(project_id=default_project.id, is_open=True)
        closed_rows = WorkItemModel.query_work_items(project_id=default_project.id, is_open=False)
        assert any(r["identifier"] == "WI-011" for r in open_rows)
        assert any(r["identifier"] == "WI-012" for r in closed_rows)


# ---------------------------------------------------------------------------
# Ready / claimable
# ---------------------------------------------------------------------------


class TestReadyQuery:
    def test_ready_excludes_deferred(self, default_project):
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-READY-01",
            title="Open item",
            status="open",
            embedding=_vec768(),
        )
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-READY-02",
            title="Deferred item",
            status="deferred",
            embedding=_vec768(),
        )
        ready = WorkItemModel.ready_work_items(project_id=default_project.id)
        ids = {r["identifier"] for r in ready}
        assert "WI-READY-01" in ids
        assert "WI-READY-02" not in ids

    def test_ready_excludes_blocked(self, default_project):
        from agent_notes.core.links import add_link

        # A blocks B
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-BLOCKER",
            title="Blocker",
            status="open",
            embedding=_vec768(),
        )
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-BLOCKED",
            title="Blocked",
            status="open",
            embedding=_vec768(),
        )
        add_link(
            from_kind="work_item",
            from_workspace=default_project.workspace_id,
            from_project=default_project.id,
            from_identifier="WI-BLOCKED",
            to_kind="work_item",
            to_workspace=default_project.workspace_id,
            to_project=default_project.id,
            to_identifier="WI-BLOCKER",
            relationship="blocks",
        )
        ready = WorkItemModel.ready_work_items(project_id=default_project.id)
        ids = {r["identifier"] for r in ready}
        assert "WI-BLOCKER" in ids  # blocker is open, not blocked
        assert "WI-BLOCKED" not in ids  # blocked because blocker is open


# ---------------------------------------------------------------------------
# Event surface
# ---------------------------------------------------------------------------


class TestEventSurface:
    def test_events_since(self, default_project):
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-EVT-01",
            title="Event test",
            embedding=_vec768(),
        )
        events = kernel.events_since(cursor=0, limit=10)
        assert len(events) >= 1
        assert events[0]["event_type"] == "item.created"

    def test_events_filter_by_type(self, default_project):
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-EVT-02",
            title="Event filter",
            embedding=_vec768(),
        )
        WorkItemModel.close_work_item(default_project.id, "WI-EVT-02")
        created_events = kernel.events_since(cursor=0, event_type="item.created", limit=10)
        closed_events = kernel.events_since(cursor=0, event_type="item.closed", limit=10)
        assert any(e["event_type"] == "item.created" for e in created_events)
        assert any(e["event_type"] == "item.closed" for e in closed_events)


# ---------------------------------------------------------------------------
# Diagnose
# ---------------------------------------------------------------------------


class TestDiagnose:
    def test_diagnose_returns_ops(self, default_project):
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-DIAG-01",
            title="Diagnose me",
            status="open",
            embedding=_vec768(),
        )
        WorkItemModel.close_work_item(default_project.id, "WI-DIAG-01")
        result = WorkItemModel.diagnose(default_project.id, "WI-DIAG-01")
        assert result["work_item"]["identifier"] == "WI-DIAG-01"
        assert len(result["ops"]) >= 2  # create + close
        op_types = [op["op_type"] for op in result["ops"]]
        assert "create" in op_types
        assert "close" in op_types


# ---------------------------------------------------------------------------
# Fold / rebuild
# ---------------------------------------------------------------------------


class TestFold:
    def test_fold_all_rebuilds_cache(self, default_project):
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-FOLD-01",
            title="Fold test",
            embedding=_vec768(),
        )
        # Manually clear the cache and rebuild.
        with kernel._conn() as conn:
            conn.execute("DELETE FROM work_items")
            count = kernel.fold_all_work_items(conn)
            conn.commit()
        assert count >= 1
        fetched = WorkItemModel.get_work_item(default_project.id, "WI-FOLD-01")
        assert fetched is not None


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------


class TestCliWorkItem:
    def test_cli_file_work_item(self, default_project, capsys):
        from agent_notes.cli.work_items import cmd_wi_file

        ns = argparse.Namespace(
            workspace=None,
            project=None,
            path="/projects/sf2",
            json=True,
            identifier="WI-CLI-01",
            title="CLI test",
            body="",
            type="todo",
            status="open",
            severity="medium",
            external_refs=None,
            diagnostic_keys=None,
        )
        assert cmd_wi_file(ns) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["work_item"]["identifier"] == "WI-CLI-01"

    def test_cli_ready(self, default_project, capsys):
        from agent_notes.cli.work_items import cmd_wi_ready

        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-CLI-READY",
            title="Ready CLI",
            status="open",
            embedding=_vec768(),
        )
        ns = argparse.Namespace(
            workspace=None,
            project=None,
            path="/projects/sf2",
            json=True,
            limit=50,
        )
        assert cmd_wi_ready(ns) == 0
        data = json.loads(capsys.readouterr().out)
        ids = {r["identifier"] for r in data["ready_work_items"]}
        assert "WI-CLI-READY" in ids


class TestCliEvents:
    def test_cli_events_tail(self, default_project, capsys):
        from agent_notes.cli.events import cmd_events_tail

        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-CLI-EVT",
            title="Event CLI",
            embedding=_vec768(),
        )
        ns = argparse.Namespace(
            cursor=0,
            event_type=None,
            limit=50,
            json=True,
        )
        assert cmd_events_tail(ns) == 0
        data = json.loads(capsys.readouterr().out)
        assert len(data["events"]) >= 1
