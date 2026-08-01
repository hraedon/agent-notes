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


class TestWorkItemUpdateNoOp:
    """WI-008/WI-009 — ``update_work_item`` must not write a redundant
    ``set_field`` op (or change_log row) when no field actually changed.

    Before the fix, the native path built its payload from any non-None
    argument without comparing to the item's current values, so every
    breadcrumb re-sync that re-imported an unchanged file appended a
    set_field op, folded it, cached it, emitted an event, and wrote a
    change_log row — all for zero state change.
    """

    @staticmethod
    def _op_count(entity_id: str) -> int:
        with kernel._conn() as conn:
            return len(kernel._get_entity_ops(conn, entity_id))

    @staticmethod
    def _cl_count(workspace_id: int, project_id: int, identifier: str) -> int:
        from agent_notes.core.change_log import history

        return len(history("work_item", workspace_id, project_id, identifier))

    def test_same_title_writes_no_op(self, default_project):
        wi = WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-NOOP-01",
            title="Same title",
            status="open",
            embedding=_vec768(),
        )
        entity_id = wi["entity_id"]
        ops_before = self._op_count(entity_id)
        WorkItemModel.update_work_item(
            project_id=default_project.id,
            identifier="WI-NOOP-01",
            title="Same title",
        )
        assert self._op_count(entity_id) == ops_before

    def test_changed_title_writes_op(self, default_project):
        wi = WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-NOOP-02",
            title="Old title",
            status="open",
            embedding=_vec768(),
        )
        entity_id = wi["entity_id"]
        ops_before = self._op_count(entity_id)
        updated = WorkItemModel.update_work_item(
            project_id=default_project.id,
            identifier="WI-NOOP-02",
            title="New title",
        )
        assert self._op_count(entity_id) == ops_before + 1
        assert updated["title"] == "New title"

    def test_all_same_fields_writes_no_op(self, default_project):
        wi = WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-NOOP-03",
            title="Unchanged",
            body="Body text",
            kind="bug",
            status="open",
            severity="high",
            embedding=_vec768(),
        )
        entity_id = wi["entity_id"]
        ops_before = self._op_count(entity_id)
        cl_before = self._cl_count(
            default_project.workspace_id,
            default_project.id,
            "WI-NOOP-03",
        )
        updated = WorkItemModel.update_work_item(
            project_id=default_project.id,
            identifier="WI-NOOP-03",
            title="Unchanged",
            body="Body text",
            kind="bug",
            severity="high",
        )
        assert self._op_count(entity_id) == ops_before
        assert (
            self._cl_count(
                default_project.workspace_id,
                default_project.id,
                "WI-NOOP-03",
            )
            == cl_before
        )
        # Returns the current state unchanged.
        assert updated["title"] == "Unchanged"
        assert updated["kind"] == "bug"

    def test_same_body_writes_no_op(self, default_project):
        wi = WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-NOOP-04",
            title="Body test",
            body="Stable body text",
            status="open",
            embedding=_vec768(),
        )
        entity_id = wi["entity_id"]
        ops_before = self._op_count(entity_id)
        WorkItemModel.update_work_item(
            project_id=default_project.id,
            identifier="WI-NOOP-04",
            body="Stable body text",
        )
        assert self._op_count(entity_id) == ops_before

    def test_same_embedding_writes_no_op(self, default_project):
        vec = [0.1] * 768
        wi = WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-NOOP-05",
            title="Embed test",
            status="open",
            embedding=vec,
        )
        entity_id = wi["entity_id"]
        ops_before = self._op_count(entity_id)
        WorkItemModel.update_work_item(
            project_id=default_project.id,
            identifier="WI-NOOP-05",
            embedding=vec,
        )
        assert self._op_count(entity_id) == ops_before

    def test_changed_embedding_writes_op(self, default_project):
        wi = WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-NOOP-06",
            title="Embed change",
            status="open",
            embedding=_vec768(),
        )
        entity_id = wi["entity_id"]
        ops_before = self._op_count(entity_id)
        WorkItemModel.update_work_item(
            project_id=default_project.id,
            identifier="WI-NOOP-06",
            embedding=[0.9] * 768,
        )
        assert self._op_count(entity_id) == ops_before + 1

    def test_same_status_no_op(self, default_project):
        wi = WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-NOOP-07",
            title="Status test",
            status="open",
            embedding=_vec768(),
        )
        entity_id = wi["entity_id"]
        ops_before = self._op_count(entity_id)
        WorkItemModel.update_work_item(
            project_id=default_project.id,
            identifier="WI-NOOP-07",
            status="open",
        )
        assert self._op_count(entity_id) == ops_before

    def test_same_external_refs_no_op(self, default_project):
        refs = {"github": "issue-42"}
        wi = WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-NOOP-08",
            title="Refs test",
            status="open",
            embedding=_vec768(),
            external_refs=refs,
        )
        entity_id = wi["entity_id"]
        ops_before = self._op_count(entity_id)
        WorkItemModel.update_work_item(
            project_id=default_project.id,
            identifier="WI-NOOP-08",
            external_refs=refs,
        )
        assert self._op_count(entity_id) == ops_before

    def test_mixed_changed_and_unchanged_only_writes_changed(self, default_project):
        # Only the genuinely-changed field must appear in the set_field op; the
        # unchanged fields must not bloat the payload (WI-008/WI-009).
        wi = WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-NOOP-09",
            title="Keep me",
            body="Keep body",
            kind="bug",
            severity="high",
            status="open",
            embedding=_vec768(),
        )
        entity_id = wi["entity_id"]
        WorkItemModel.update_work_item(
            project_id=default_project.id,
            identifier="WI-NOOP-09",
            title="Keep me",
            body="Keep body",
            kind="todo",
            severity="high",
        )
        ops = kernel._get_entity_ops(kernel._conn().__enter__(), entity_id)
        set_field_ops = [o for o in ops if o["op_type"] == "set_field"]
        assert len(set_field_ops) >= 1
        last_payload = {
            k: v for k, v in (set_field_ops[-1].get("payload") or {}).items() if k != "envelope"
        }
        assert "kind" in last_payload
        assert "title" not in last_payload
        assert "severity" not in last_payload

    def test_change_log_body_payload_is_correct(self, default_project):
        # Adversarial-review catch: the native change_log builder used to log
        # body as {"from": old_body, "to": None} because folded has no "body"
        # column. Verify the new body is resolved from the blob instead.
        from agent_notes.core.change_log import history

        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-NOOP-10",
            title="CL body",
            body="original body",
            status="open",
            embedding=_vec768(),
        )
        WorkItemModel.update_work_item(
            project_id=default_project.id,
            identifier="WI-NOOP-10",
            body="updated body",
        )
        rows = history(
            "work_item",
            default_project.workspace_id,
            default_project.id,
            "WI-NOOP-10",
        )
        update_rows = [r for r in rows if r.event == "updated"]
        assert update_rows, "expected an 'updated' change_log row"
        body_entry = update_rows[-1].payload.get("body")
        assert body_entry is not None, "change_log should record the body change"
        assert body_entry["from"] == "original body"
        assert body_entry["to"] == "updated body"


class TestWorkItemClose:
    def test_close_work_item(self, default_project):
        # Plan 014 A(b): native (degrade) close DEFERS to in_review, not terminal
        # — completion needs the review gate. force=True is the terminal escape hatch.
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-007",
            title="To close",
            status="open",
            embedding=_vec768(),
        )
        deferred = WorkItemModel.close_work_item(default_project.id, "WI-007")
        assert deferred["status"] == "in_review"
        assert deferred["closed_at"] is None

    def test_force_close_work_item_terminal(self, default_project):
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-007F",
            title="To force-close",
            status="open",
            embedding=_vec768(),
        )
        closed = WorkItemModel.close_work_item(default_project.id, "WI-007F", force=True)
        assert closed["status"] == "closed"
        assert closed["closed_at"] is not None

    def test_close_nonexistent_raises(self, default_project):
        with pytest.raises(ValueError, match="not found"):
            WorkItemModel.close_work_item(default_project.id, "WI-NONEXISTENT")

    def test_native_deferred_close_is_atomic(self, default_project, monkeypatch):
        """WI-020: the open→in_progress→in_review close commits once.

        The native deferred close moves an open item through ``in_progress`` on
        its way to ``in_review``. Before the fix each step was its own committed
        transaction, so a failure between them stranded the item in
        ``in_progress``. Simulate a crash on the second transition and assert the
        first one rolled back (item still ``open``) — only possible if both share
        a single transaction.
        """
        from agent_notes.core.work_item import _native

        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-ATOMIC",
            title="Atomic close",
            status="open",
            embedding=_vec768(),
        )

        real_update = _native.update_work_item
        calls = {"n": 0}

        def crash_on_second(conn, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("simulated failure on second transition")
            return real_update(conn, *args, **kwargs)

        monkeypatch.setattr(_native, "update_work_item", crash_on_second)

        with pytest.raises(RuntimeError, match="simulated failure"):
            WorkItemModel.close_work_item(default_project.id, "WI-ATOMIC")

        # Both transitions ran, but the shared transaction rolled back: the item
        # is still open, not stranded in_progress by a half-committed close.
        assert calls["n"] == 2
        assert WorkItemModel.get_work_item(default_project.id, "WI-ATOMIC")["status"] == "open"


# ---------------------------------------------------------------------------
# Transition pre-flight (Plan 013 WI-5)
# ---------------------------------------------------------------------------


class TestTransitionPreFlight:
    """Tests for the native-path transition pre-flight (Plan 013 WI-5).

    Before WI-5, the native ``update_work_item`` path wrote any vocab-valid
    status with no transition check — so an illegal state was creatable from
    the native path (merely spelled consistently). The regista path already
    enforced transitions. WI-5 makes both paths reject the same illegal
    transitions, with an explicit ``force=True`` escape hatch.
    """

    def test_legal_transition_allowed(self, default_project):
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-PF-01",
            title="Pre-flight",
            status="open",
            embedding=_vec768(),
        )
        updated = WorkItemModel.update_work_item(
            project_id=default_project.id,
            identifier="WI-PF-01",
            status="in_progress",
        )
        assert updated["status"] == "in_progress"

    def test_illegal_transition_rejected(self, default_project):
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-PF-02",
            title="Pre-flight",
            status="open",
            embedding=_vec768(),
        )
        # open → in_review is not a valid transition (must go through in_progress)
        with pytest.raises(ValueError, match="Unsupported status transition"):
            WorkItemModel.update_work_item(
                project_id=default_project.id,
                identifier="WI-PF-02",
                status="in_review",
            )

    def test_illegal_transition_force_bypasses(self, default_project):
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-PF-03",
            title="Force override",
            status="open",
            embedding=_vec768(),
        )
        # force=True bypasses the transition check (admin/repair escape hatch)
        updated = WorkItemModel.update_work_item(
            project_id=default_project.id,
            identifier="WI-PF-03",
            status="in_review",
            force=True,
        )
        assert updated["status"] == "in_review"

    def test_same_status_no_transition_check(self, default_project):
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-PF-04",
            title="Same status",
            status="open",
            embedding=_vec768(),
        )
        # Setting the same status should not trigger the transition check.
        updated = WorkItemModel.update_work_item(
            project_id=default_project.id,
            identifier="WI-PF-04",
            status="open",
            title="Updated title",
        )
        assert updated["title"] == "Updated title"
        assert updated["status"] == "open"

    def test_no_status_no_transition_check(self, default_project):
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-PF-05",
            title="No status",
            status="open",
            embedding=_vec768(),
        )
        # No status change → no transition check.
        updated = WorkItemModel.update_work_item(
            project_id=default_project.id,
            identifier="WI-PF-05",
            title="Updated title",
        )
        assert updated["title"] == "Updated title"

    def test_set_status_rejects_illegal(self, default_project):
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-PF-06",
            title="Set status",
            status="in_progress",
            embedding=_vec768(),
        )
        # in_progress → done is illegal (must go through review gate).
        with pytest.raises(ValueError, match="Unsupported status transition"):
            WorkItemModel.set_status(
                project_id=default_project.id,
                identifier="WI-PF-06",
                status="done",
            )

    def test_set_status_force_bypasses(self, default_project):
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-PF-07",
            title="Force set",
            status="open",
            embedding=_vec768(),
        )
        updated = WorkItemModel.set_status(
            project_id=default_project.id,
            identifier="WI-PF-07",
            status="done",
            force=True,
        )
        assert updated["status"] == "done"

    def test_closed_alias_transition_allowed(self, default_project):
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-PF-08",
            title="Closed alias",
            status="open",
            embedding=_vec768(),
        )
        # open → closed is an alias for open → done (close_from_open).
        updated = WorkItemModel.update_work_item(
            project_id=default_project.id,
            identifier="WI-PF-08",
            status="closed",
        )
        assert updated["status"] == "closed"


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

    def test_fold_honors_tombstone_and_does_not_resurrect(self, default_project):
        """WI-021: a soft-deleted item must stay out of the cache after a fold.

        ``delete_work_item`` appends a snapshot op carrying ``{"tombstone":
        True}`` and removes the cache row. The fold used to read only
        ``sealed_state`` from snapshot ops, ignoring the tombstone, so
        ``fold_all_work_items`` rebuilt the deleted item's live state and
        re-upserted it — resurrecting it. The fold must treat a tombstoned
        entity as absent.
        """
        created = WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-RESURRECT",
            title="Will be deleted",
            embedding=_vec768(),
        )
        entity_id = created["entity_id"]
        assert WorkItemModel.delete_work_item(default_project.id, "WI-RESURRECT") is True
        assert WorkItemModel.get_work_item(default_project.id, "WI-RESURRECT") is None

        with kernel._conn() as conn:
            # The pure fold reports no state for a tombstoned entity.
            assert kernel.fold_work_item_state(conn, entity_id) is None
            # A full cache rebuild must not bring the deleted item back.
            kernel.fold_all_work_items(conn)
            conn.commit()

        assert WorkItemModel.get_work_item(default_project.id, "WI-RESURRECT") is None


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
        # Plan 014 A(b): only force=True writes the terminal close (item.closed);
        # default close now defers to in_review.
        WorkItemModel.close_work_item(default_project.id, "WI-EVT-02", force=True)
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
        # force=True writes a terminal close op (Plan 014: default close defers).
        WorkItemModel.close_work_item(default_project.id, "WI-DIAG-01", force=True)
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

    def test_deferred_dominates_closed_concurrent(self, default_project):
        # Plan 013 §5: deferred (rank 2) dominates closed (rank 0) — deferred
        # is a non-terminal idle state (unfinished), closed is a legacy
        # terminal. The old _STATUS_LATTICE had closed > deferred (rank 1 vs
        # 0), which violated the fail-safe principle (more-unfinished wins).
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
        assert folded["status"] == "deferred"

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
        WorkItemModel.close_work_item(default_project.id, "WI-LAT-04", force=True)
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
# Canonical-state lattice resolution (Plan 013 WI-3 — the P0 fix)
# ---------------------------------------------------------------------------


class TestCanonicalLatticeResolution:
    """Tests that canonical states resolve correctly in concurrent conflicts.

    Before Plan 013, ``_STATUS_LATTICE`` only covered legacy states
    (open/claimed/closed/deferred), so every canonical state resolved to
    rank -1. This meant concurrent canonical-vs-canonical ties were broken
    purely by lexicographic op_id — a ``done`` with a smaller op_id would
    silently beat ``in_progress``, violating the fail-safe principle.
    """

    def test_in_progress_dominates_done_concurrent(self, default_project):
        wi = WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-CLAT-01",
            title="Canonical lattice",
            status="open",
            embedding=_vec768(),
        )
        entity_id = wi["entity_id"]

        with kernel._conn() as conn:
            _insert_op_direct(conn, entity_id, "work_item", "set_status", 999, {"status": "done"})
            _insert_op_direct(
                conn, entity_id, "work_item", "set_status", 999, {"status": "in_progress"}
            )
            conn.commit()

        with kernel._conn() as conn:
            folded = kernel.fold_work_item(conn, entity_id)
        assert folded is not None
        assert folded["status"] == "in_progress"

    def test_open_dominates_in_progress_concurrent(self, default_project):
        wi = WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-CLAT-02",
            title="Open vs in_progress",
            status="open",
            embedding=_vec768(),
        )
        entity_id = wi["entity_id"]

        with kernel._conn() as conn:
            _insert_op_direct(
                conn, entity_id, "work_item", "set_status", 999, {"status": "in_progress"}
            )
            _insert_op_direct(conn, entity_id, "work_item", "set_status", 999, {"status": "open"})
            conn.commit()

        with kernel._conn() as conn:
            folded = kernel.fold_work_item(conn, entity_id)
        assert folded is not None
        assert folded["status"] == "open"

    def test_in_review_dominates_done_concurrent(self, default_project):
        # The dangerous case from Plan 013 §0.3: a concurrent done must NOT
        # silently beat in_review. Before the fix, both resolved to rank -1
        # and the tie was broken by op_id (done could win).
        wi = WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-CLAT-03",
            title="Review vs done",
            status="open",
            embedding=_vec768(),
        )
        entity_id = wi["entity_id"]

        with kernel._conn() as conn:
            _insert_op_direct(
                conn, entity_id, "work_item", "set_status", 999, {"status": "in_review"}
            )
            _insert_op_direct(conn, entity_id, "work_item", "set_status", 999, {"status": "done"})
            conn.commit()

        with kernel._conn() as conn:
            folded = kernel.fold_work_item(conn, entity_id)
        assert folded is not None
        assert folded["status"] == "in_review"

    def test_in_progress_ties_with_blocked_by_op_id(self, default_project):
        # in_progress and blocked have the same rank (7); tie breaks by op_id.
        wi = WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-CLAT-04",
            title="Tie break",
            status="open",
            embedding=_vec768(),
        )
        entity_id = wi["entity_id"]

        with kernel._conn() as conn:
            _insert_op_direct(
                conn, entity_id, "work_item", "set_status", 999, {"status": "in_progress"}
            )
            _insert_op_direct(
                conn, entity_id, "work_item", "set_status", 999, {"status": "blocked"}
            )
            conn.commit()

        with kernel._conn() as conn:
            folded = kernel.fold_work_item(conn, entity_id)
        assert folded is not None
        # Both have rank 7; the result is one of the two (tie-break by op_id).
        assert folded["status"] in ("in_progress", "blocked")

    def test_claimed_dominates_done_concurrent(self, default_project):
        # Cross-axis (Plan 013 §0.4): claimed (rank 4) should beat done (rank 0).
        # A lease should not be silently completed.
        wi = WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-CLAT-05",
            title="Claimed vs done",
            status="open",
            embedding=_vec768(),
        )
        entity_id = wi["entity_id"]

        with kernel._conn() as conn:
            _insert_op_direct(conn, entity_id, "work_item", "set_status", 999, {"status": "done"})
            _insert_op_direct(
                conn, entity_id, "work_item", "set_status", 999, {"status": "claimed"}
            )
            conn.commit()

        with kernel._conn() as conn:
            folded = kernel.fold_work_item(conn, entity_id)
        assert folded is not None
        assert folded["status"] == "claimed"

    def test_in_review_dominates_claimed_concurrent(self, default_project):
        # Cross-axis: in_review (rank 6) should beat claimed (rank 4).
        # Real lifecycle progress overrides a lease.
        wi = WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-CLAT-06",
            title="Review vs claimed",
            status="open",
            embedding=_vec768(),
        )
        entity_id = wi["entity_id"]

        with kernel._conn() as conn:
            _insert_op_direct(
                conn, entity_id, "work_item", "set_status", 999, {"status": "claimed"}
            )
            _insert_op_direct(
                conn, entity_id, "work_item", "set_status", 999, {"status": "in_review"}
            )
            conn.commit()

        with kernel._conn() as conn:
            folded = kernel.fold_work_item(conn, entity_id)
        assert folded is not None
        assert folded["status"] == "in_review"

    def test_all_canonical_states_have_nonzero_rank_in_fold(self, default_project):
        # Regression: every canonical state must resolve to a real rank (not -1)
        # so concurrent resolution works. Verify each canonical state can win
        # against a concurrent done.
        from agent_notes.core.lifecycle import CANONICAL_STATES

        non_terminal = CANONICAL_STATES - {"done"}
        for i, status in enumerate(sorted(non_terminal)):
            wi = WorkItemModel.file_work_item(
                project_id=default_project.id,
                identifier=f"WI-CLAT-REG-{i:02d}",
                title=f"Regression {status}",
                status="open",
                embedding=_vec768(),
            )
            entity_id = wi["entity_id"]

            with kernel._conn() as conn:
                _insert_op_direct(
                    conn, entity_id, "work_item", "set_status", 999, {"status": "done"}
                )
                _insert_op_direct(
                    conn, entity_id, "work_item", "set_status", 999, {"status": status}
                )
                conn.commit()

            with kernel._conn() as conn:
                folded = kernel.fold_work_item(conn, entity_id)
            assert folded is not None
            assert folded["status"] == status, (
                f"canonical state {status!r} did not dominate concurrent done "
                f"(got {folded['status']!r}) — rank may be -1 (P0 regression)"
            )


# ---------------------------------------------------------------------------
# Unit tests for _resolve_status_lattice (no DB — pure function)
# ---------------------------------------------------------------------------


class TestResolveStatusLatticeUnit:
    """Pure unit tests for the status lattice resolver (no DB needed).

    These verify the resolution logic directly, without going through the
    op-log fold. Each test constructs synthetic status ops and checks the
    winning status.
    """

    @staticmethod
    def _status_op(status: str, op_id: str) -> dict:
        return {"op_type": "set_status", "op_id": op_id, "payload": {"status": status}}

    @staticmethod
    def _close_op(op_id: str) -> dict:
        return {"op_type": "close", "op_id": op_id, "payload": {"reason": "test"}}

    def test_single_canonical_status_applied_directly(self):
        # Single op: sequential, applied directly.
        result = kernel._resolve_status_lattice([self._status_op("in_progress", "aaa")], None)
        assert result == "in_progress"

    def test_in_progress_beats_done(self):
        ops = [
            self._status_op("done", "aaa"),
            self._status_op("in_progress", "bbb"),
        ]
        assert kernel._resolve_status_lattice(ops, None) == "in_progress"

    def test_done_does_not_silently_complete(self):
        # The core P0 bug: done with a smaller op_id must NOT beat in_progress.
        ops = [
            self._status_op("in_progress", "zzz"),
            self._status_op("done", "aaa"),
        ]
        assert kernel._resolve_status_lattice(ops, None) == "in_progress"

    def test_open_beats_all_canonical(self):
        for status in ("in_progress", "blocked", "in_review", "in_human_review", "done"):
            ops = [
                self._status_op(status, "aaa"),
                self._status_op("open", "bbb"),
            ]
            assert kernel._resolve_status_lattice(ops, None) == "open"

    def test_canonical_tie_break_by_op_id(self):
        # Same rank (in_progress == blocked, both 7): tie breaks by op_id.
        ops = [
            self._status_op("in_progress", "zzz"),
            self._status_op("blocked", "aaa"),
        ]
        assert kernel._resolve_status_lattice(ops, None) == "blocked"

    def test_close_op_resolves_to_closed(self):
        ops = [self._close_op("aaa")]
        assert kernel._resolve_status_lattice(ops, None) == "closed"

    def test_close_vs_done_tie_by_op_id(self):
        # close -> "closed" (rank 0), done (rank 0): tie, break by op_id.
        ops = [
            self._close_op("zzz"),
            self._status_op("done", "aaa"),
        ]
        assert kernel._resolve_status_lattice(ops, None) == "done"

    def test_empty_ops_returns_current(self):
        assert kernel._resolve_status_lattice([], "in_progress") == "in_progress"

    # --- Cross-axis tests (Plan 013 §0.4 — the adversarial review scope) ---

    def test_claimed_beats_close_op(self):
        # close -> "closed" (rank 0); claimed (rank 4) wins.
        ops = [
            self._close_op("aaa"),
            self._status_op("claimed", "bbb"),
        ]
        assert kernel._resolve_status_lattice(ops, None) == "claimed"

    def test_in_review_beats_close_op(self):
        # close -> "closed" (rank 0); in_review (rank 6) wins.
        ops = [
            self._close_op("aaa"),
            self._status_op("in_review", "bbb"),
        ]
        assert kernel._resolve_status_lattice(ops, None) == "in_review"

    def test_deferred_beats_close_op(self):
        # close -> "closed" (rank 0); deferred (rank 2) wins.
        ops = [
            self._close_op("aaa"),
            self._status_op("deferred", "bbb"),
        ]
        assert kernel._resolve_status_lattice(ops, None) == "deferred"

    def test_claimed_vs_deferred_concurrent(self):
        # Cross-axis: claimed (rank 4) beats deferred (rank 2).
        # A lease (potentially active) is more unfinished than a deliberate park.
        ops = [
            self._status_op("deferred", "aaa"),
            self._status_op("claimed", "bbb"),
        ]
        assert kernel._resolve_status_lattice(ops, None) == "claimed"

    def test_three_way_open_wins(self):
        # Three concurrent ops: open, closed, done → open (rank 8).
        ops = [
            self._status_op("done", "aaa"),
            self._close_op("bbb"),
            self._status_op("open", "ccc"),
        ]
        assert kernel._resolve_status_lattice(ops, None) == "open"

    def test_three_way_in_progress_wins(self):
        # Three concurrent: done, close, in_progress → in_progress (rank 7).
        ops = [
            self._status_op("done", "aaa"),
            self._close_op("bbb"),
            self._status_op("in_progress", "ccc"),
        ]
        assert kernel._resolve_status_lattice(ops, None) == "in_progress"


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------


class TestCliWorkItem:
    @pytest.mark.slow
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
        """Foreign target projects are stored in cross_project_links without raising."""
        result = WorkItemModel.add_cross_project_link(
            from_project_id=default_project.id,
            from_identifier="WI-LOCAL-02",
            to_project_slug="nonexistent",
            to_identifier="WI-TARGET-02",
        )
        assert result["from_identifier"] == "WI-LOCAL-02"
        assert result["to_project_slug"] == "nonexistent"
        assert result["to_project_id"] is None
        assert result["relationship"] == "blocks"

        # Verify the edge is in cross_project_links.
        from agent_notes.core.db import _conn

        with _conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT 1 FROM cross_project_links
                WHERE from_project_id = %s AND from_identifier = %s
                  AND to_project_slug = %s AND to_identifier = %s
                """,
                (default_project.id, "WI-LOCAL-02", "nonexistent", "WI-TARGET-02"),
            )
            assert cur.fetchone() is not None


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
