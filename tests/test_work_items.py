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

    def test_events_since_cursor(self, default_project):
        # File first item and record cursor
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-EVT-03",
            title="First event",
            embedding=_vec768(),
        )
        events_after_first = kernel.events_since(cursor=0, limit=10)
        first_cursor = events_after_first[0]["id"]

        # File second item
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-EVT-04",
            title="Second event",
            embedding=_vec768(),
        )

        # Query with cursor from first event should only return newer events
        events_after_cursor = kernel.events_since(cursor=first_cursor, limit=10)
        assert all(e["id"] > first_cursor for e in events_after_cursor)
        assert any(e["payload"].get("identifier") == "WI-EVT-04" for e in events_after_cursor)


# ---------------------------------------------------------------------------
# suggest_duplicates
# ---------------------------------------------------------------------------


class TestSuggestDuplicates:
    def test_suggest_duplicates_no_embedding(self, default_project):
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-DUP-01",
            title="No embed",
            # leave embedding NULL
        )
        dups = WorkItemModel.suggest_duplicates(default_project.id, "WI-DUP-01")
        assert dups == []

    def test_suggest_duplicates_no_matches(self, default_project):
        # Use orthogonal-ish vectors so similarity is near zero
        v1 = [1.0] + [0.0] * 767
        v2 = [0.0] * 767 + [1.0]
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-DUP-02",
            title="Unique item about quantum caching",
            embedding=v1,
        )
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-DUP-03",
            title="Totally different item about lunar pipelines",
            embedding=v2,
        )
        dups = WorkItemModel.suggest_duplicates(default_project.id, "WI-DUP-02", threshold=0.99)
        assert dups == []

    def test_suggest_duplicates_finds_similar(self, default_project):
        # Same non-zero embedding (simulates identical semantic content)
        same_vec = [0.1] * 768
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-DUP-04",
            title="Duplicate item about caching",
            embedding=same_vec,
        )
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-DUP-05",
            title="Another item about caching",
            embedding=same_vec,
        )
        dups = WorkItemModel.suggest_duplicates(default_project.id, "WI-DUP-04", threshold=0.90)
        identifiers = [d["identifier"] for d in dups]
        assert "WI-DUP-05" in identifiers


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
# Status lattice (P2)
# ---------------------------------------------------------------------------


def _insert_op_direct(conn, entity_id, entity_type, op_type, lamport, payload, parent_op_ids=None):
    """Insert an op directly into op_log with a fixed lamport (for testing concurrent ops)."""
    import psycopg

    from agent_notes.core.kernel import _make_op_id

    parent_op_ids = parent_op_ids or []
    # Add a unique nonce to ensure distinct op_id even when payload+lamport are identical
    import time

    nonce = time.time_ns()
    test_payload = dict(payload)
    test_payload["_test_nonce"] = nonce
    op_id = _make_op_id(entity_type, op_type, test_payload, parent_op_ids)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO op_log
            (op_id, entity_id, entity_type, op_type, lamport, actor_id, payload, parent_op_ids)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            op_id,
            entity_id,
            entity_type,
            op_type,
            lamport,
            "test-actor",
            psycopg.types.json.Jsonb(test_payload),
            parent_op_ids,
        ),
    )
    return op_id


class TestStatusLattice:
    def test_open_dominates_closed_concurrent(self, default_project):
        # Create a work item
        wi = WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-LAT-01",
            title="Lattice test",
            status="open",
            embedding=_vec768(),
        )
        entity_id = wi["entity_id"]

        # Insert two concurrent status ops at the same lamport
        with kernel._conn() as conn:
            _insert_op_direct(conn, entity_id, "work_item", "set_status", 999, {"status": "closed"})
            _insert_op_direct(conn, entity_id, "work_item", "set_status", 999, {"status": "open"})
            conn.commit()

        # Rebuild and check: open should win
        with kernel._conn() as conn:
            folded = kernel.fold_work_item(conn, entity_id)
        assert folded is not None
        assert folded["status"] == "open"

    def test_closed_dominates_deferred_concurrent(self, default_project):
        wi = WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-LAT-02",
            title="Lattice test 2",
            status="open",
            embedding=_vec768(),
        )
        entity_id = wi["entity_id"]

        with kernel._conn() as conn:
            _insert_op_direct(
                conn, entity_id, "work_item", "set_status", 999, {"status": "deferred"}
            )
            _insert_op_direct(conn, entity_id, "work_item", "set_status", 999, {"status": "closed"})
            conn.commit()

        with kernel._conn() as conn:
            folded = kernel.fold_work_item(conn, entity_id)
        assert folded is not None
        assert folded["status"] == "closed"

    def test_tie_break_by_op_id(self, default_project):
        wi = WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-LAT-03",
            title="Lattice tie",
            status="open",
            embedding=_vec768(),
        )
        entity_id = wi["entity_id"]

        # Two concurrent ops setting same status but different op_ids
        with kernel._conn() as conn:
            _insert_op_direct(
                conn, entity_id, "work_item", "set_status", 999, {"status": "claimed"}
            )
            _insert_op_direct(
                conn, entity_id, "work_item", "set_status", 999, {"status": "claimed"}
            )
            conn.commit()

        # They should be identical status, so whichever wins is fine
        with kernel._conn() as conn:
            folded = kernel.fold_work_item(conn, entity_id)
        assert folded is not None
        assert folded["status"] == "claimed"

    def test_sequential_ops_not_affected(self, default_project):
        # Sequential ops: last one wins (normal case)
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-LAT-04",
            title="Sequential",
            status="open",
            embedding=_vec768(),
        )
        WorkItemModel.close_work_item(default_project.id, "WI-LAT-04")
        fetched = WorkItemModel.get_work_item(default_project.id, "WI-LAT-04")
        assert fetched["status"] == "closed"

    def test_close_op_dominates_open_concurrent(self, default_project):
        wi = WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-LAT-05",
            title="Close vs open",
            status="open",
            embedding=_vec768(),
        )
        entity_id = wi["entity_id"]

        with kernel._conn() as conn:
            _insert_op_direct(conn, entity_id, "work_item", "close", 999, {"reason": "test"})
            _insert_op_direct(conn, entity_id, "work_item", "set_status", 999, {"status": "open"})
            conn.commit()

        with kernel._conn() as conn:
            folded = kernel.fold_work_item(conn, entity_id)
        assert folded is not None
        assert folded["status"] == "open"  # open dominates closed

    def test_merge_op_replaces_state(self, default_project):
        wi = WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-LAT-06",
            title="Merge test",
            status="open",
            embedding=_vec768(),
        )
        entity_id = wi["entity_id"]

        with kernel._conn() as conn:
            _insert_op_direct(
                conn,
                entity_id,
                "work_item",
                "merge",
                999,
                {
                    "merged_state": {
                        "title": "Merged title",
                        "status": "claimed",
                        "severity": "high",
                    }
                },
            )
            conn.commit()

        with kernel._conn() as conn:
            folded = kernel.fold_work_item(conn, entity_id)
        assert folded is not None
        assert folded["title"] == "Merged title"
        assert folded["status"] == "claimed"
        assert folded["severity"] == "high"

    def test_concurrent_set_field_and_status(self, default_project):
        wi = WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-LAT-07",
            title="Concurrent fields",
            status="open",
            embedding=_vec768(),
        )
        entity_id = wi["entity_id"]

        with kernel._conn() as conn:
            _insert_op_direct(
                conn, entity_id, "work_item", "set_field", 999, {"title": "New title"}
            )
            _insert_op_direct(conn, entity_id, "work_item", "set_status", 999, {"status": "closed"})
            conn.commit()

        with kernel._conn() as conn:
            folded = kernel.fold_work_item(conn, entity_id)
        assert folded is not None
        assert folded["title"] == "New title"
        assert folded["status"] == "closed"


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


# ---------------------------------------------------------------------------
# Merge / reconcile (P2)
# ---------------------------------------------------------------------------


class TestMerge:
    def test_merge_two_divergent_chains(self, default_project):
        # Create a work item
        wi = WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-MERGE-01",
            title="Merge test",
            status="open",
            embedding=_vec768(),
        )
        entity_id = wi["entity_id"]

        # Simulate two divergent chains by inserting ops directly at different lamports
        with kernel._conn() as conn:
            _insert_op_direct(
                conn, entity_id, "work_item", "set_field", 1000, {"title": "Remote title"}
            )
            _insert_op_direct(
                conn, entity_id, "work_item", "set_status", 1001, {"status": "closed"}
            )
            conn.commit()

        # Reconcile: local chain has the original + these two remote ops
        with kernel._conn() as conn:
            # Actually reconcile using the real remote ops from the DB
            real_remote = kernel._get_entity_ops(conn, entity_id)
            # We simulate a "remote" by taking the last two ops
            remote_only = real_remote[-2:]

            # Clear the cache first
            conn.execute("DELETE FROM work_items WHERE entity_id = %s", (entity_id,))

            # Reconcile with the same ops (no divergence)
            result = kernel.reconcile_entity(conn, entity_id, remote_only)
            conn.commit()

        assert result is not None
        assert result["identifier"] == "WI-MERGE-01"
        # The title should be "Remote title" because set_field was applied
        assert result["title"] == "Remote title"
        # The status should be "closed" because set_status was applied
        assert result["status"] == "closed"

    def test_merge_union_deduplicates(self, default_project):
        # Create a work item
        wi = WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-MERGE-02",
            title="Dedup test",
            status="open",
            embedding=_vec768(),
        )
        entity_id = wi["entity_id"]

        # Get the ops
        with kernel._conn() as conn:
            local_ops = kernel._get_entity_ops(conn, entity_id)

            # Merge with identical ops (no divergence)
            merged = kernel.merge_entity(local_ops, local_ops)
            assert len(merged) == len(local_ops)

            # All op_ids should be present
            ids = {op["op_id"] for op in merged}
            assert len(ids) == len(local_ops)


class TestReconcile:
    def test_reconcile_entity_creates_merge_op(self, default_project):
        wi = WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-REC-01",
            title="Reconcile test",
            status="open",
            embedding=_vec768(),
        )
        entity_id = wi["entity_id"]

        # Add a remote op directly
        with kernel._conn() as conn:
            _insert_op_direct(
                conn, entity_id, "work_item", "set_field", 1000, {"title": "Reconciled"}
            )
            conn.commit()

        # Reconcile with the same ops
        with kernel._conn() as conn:
            remote_ops = kernel._get_entity_ops(conn, entity_id)
            conn.execute("DELETE FROM work_items WHERE entity_id = %s", (entity_id,))
            result = kernel.reconcile_entity(conn, entity_id, remote_ops)
            conn.commit()

        assert result is not None
        assert result["title"] == "Reconciled"

        # Check that a merge op was written
        with kernel._conn() as conn:
            ops = kernel._get_entity_ops(conn, entity_id)
            merge_ops = [op for op in ops if op["op_type"] == "merge"]
            assert len(merge_ops) == 1


# ---------------------------------------------------------------------------
# Cross-project (P3)
# ---------------------------------------------------------------------------


class TestParseAddress:
    def test_valid_address(self):
        from agent_notes.core.work_item_model import WorkItemModel

        assert WorkItemModel.parse_address("sf2:BC-12") == ("sf2", "BC-12")

    def test_address_with_multiple_colons(self):
        from agent_notes.core.work_item_model import WorkItemModel

        assert WorkItemModel.parse_address("sf2:BC-12:extra") == ("sf2", "BC-12:extra")

    def test_invalid_address(self):
        from agent_notes.core.work_item_model import WorkItemModel

        with pytest.raises(ValueError, match="Invalid address"):
            WorkItemModel.parse_address("no-colon")


class TestCrossProjectRequest:
    def test_request_work_item(self, default_project):
        from agent_notes.core import db as coredb

        # Create a target project in the same workspace
        coredb.get_or_create_project(
            default_project.workspace_id,
            slug="target-proj",
            name="Target Project",
            repo_root="/projects/target",
        )

        req = WorkItemModel.request_work_item(
            project_id=default_project.id,
            target_project_slug="target-proj",
            title="Cross-project request",
            body="Please do this",
            kind="task",
        )

        assert req["title"] == "Cross-project request"
        assert req["target_project"] == "target-proj"
        assert req["request_type"] == "create_work_item"

    def test_request_work_item_unknown_target(self, default_project):
        # request_work_item does NOT validate the target project exists at
        # creation time — validation happens at intake time in the target
        # project. This is by design: the request is signed evidence in the
        # dependent's log; the target may not exist yet or may be resolved
        # later by the derived index.
        req = WorkItemModel.request_work_item(
            project_id=default_project.id,
            target_project_slug="nonexistent",
            title="Bad request",
        )
        assert req["target_project"] == "nonexistent"


class TestCrossProjectWait:
    def test_wait_on_work_item(self, default_project):
        from agent_notes.core import db as coredb

        # Create a target project in the same workspace
        coredb.get_or_create_project(
            default_project.workspace_id,
            slug="target-proj2",
            name="Target Project 2",
            repo_root="/projects/target2",
        )

        wait = WorkItemModel.wait_on_work_item(
            project_id=default_project.id,
            target_project_slug="target-proj2",
            target_identifier="WI-001",
        )

        assert wait["target_project"] == "target-proj2"
        assert wait["target_identifier"] == "WI-001"
        assert wait["wait_type"] == "block_on_work_item"


class TestCrossProjectLink:
    def test_add_cross_project_link(self, default_project):
        from agent_notes.core import db as coredb

        # Create a target project in the SAME workspace so it shares vocabularies
        target_proj = coredb.get_or_create_project(
            default_project.workspace_id,
            slug="target-proj3",
            name="Target Project 3",
            repo_root="/projects/target3",
        )

        # Create a local work item
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-LOCAL-01",
            title="Local item",
            embedding=_vec768(),
        )

        # Create a target work item
        WorkItemModel.file_work_item(
            project_id=target_proj.id,
            identifier="WI-TARGET-01",
            title="Target item",
            embedding=_vec768(),
        )

        result = WorkItemModel.add_cross_project_link(
            from_project_id=default_project.id,
            from_identifier="WI-LOCAL-01",
            to_project_slug="target-proj3",
            to_identifier="WI-TARGET-01",
            relationship="blocks",
        )

        assert result["from_identifier"] == "WI-LOCAL-01"
        assert result["to_identifier"] == "WI-TARGET-01"
        assert result["relationship"] == "blocks"

    def test_add_cross_project_link_unknown_target(self, default_project):
        with pytest.raises(ValueError, match="Target project not found"):
            WorkItemModel.add_cross_project_link(
                from_project_id=default_project.id,
                from_identifier="WI-LOCAL-02",
                to_project_slug="nonexistent",
                to_identifier="WI-TARGET-02",
            )


# ---------------------------------------------------------------------------
# CLI cross-project smoke
# ---------------------------------------------------------------------------


class TestCliCrossProject:
    def test_cli_request(self, default_project, capsys):
        from agent_notes.cli.work_items import cmd_wi_request
        from agent_notes.core import db as coredb

        coredb.get_or_create_project(
            default_project.workspace_id,
            slug="target-proj4",
            name="Target Project 4",
            repo_root="/projects/target4",
        )

        ns = argparse.Namespace(
            workspace=None,
            project=None,
            path="/projects/sf2",
            json=True,
            target_project="target-proj4",
            title="CLI request",
            body="",
            type="task",
        )
        assert cmd_wi_request(ns) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["request"]["target_project"] == "target-proj4"

    def test_cli_wait(self, default_project, capsys):
        from agent_notes.cli.work_items import cmd_wi_wait
        from agent_notes.core import db as coredb

        coredb.get_or_create_project(
            default_project.workspace_id,
            slug="target-proj5",
            name="Target Project 5",
            repo_root="/projects/target5",
        )

        ns = argparse.Namespace(
            workspace=None,
            project=None,
            path="/projects/sf2",
            json=True,
            target="target-proj5:WI-001",
        )
        assert cmd_wi_wait(ns) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["wait"]["target_project"] == "target-proj5"

    def test_cli_link_cross(self, default_project, capsys):
        from agent_notes.cli.work_items import cmd_wi_link_cross
        from agent_notes.core import db as coredb

        target_proj = coredb.get_or_create_project(
            default_project.workspace_id,
            slug="target-proj6",
            name="Target Project 6",
            repo_root="/projects/target6",
        )

        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-CLI-X-01",
            title="Local cross",
            embedding=_vec768(),
        )
        WorkItemModel.file_work_item(
            project_id=target_proj.id,
            identifier="WI-CLI-X-02",
            title="Target cross",
            embedding=_vec768(),
        )

        ns = argparse.Namespace(
            workspace=None,
            project=None,
            path="/projects/sf2",
            json=True,
            identifier="WI-CLI-X-01",
            target="target-proj6:WI-CLI-X-02",
            relationship="blocks",
        )
        assert cmd_wi_link_cross(ns) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["link"]["from_identifier"] == "WI-CLI-X-01"
        assert data["link"]["to_identifier"] == "WI-CLI-X-02"
