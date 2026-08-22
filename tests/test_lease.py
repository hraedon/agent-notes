"""Integration tests for Plan 008 P4 — claim / heartbeat / release lease lifecycle.

These cover the local-lease code path (``claim_work_item``,
``heartbeat_work_item``, ``release_work_item``) which previously had zero
test coverage despite the CHANGELOG citing two bug fixes here:

- the ``set_status`` op written after a claim must be parented on the
  ``claim`` op's ``op_id`` (NOT the ``entity_id``) — same for release.
- the heartbeat path must write both an op and an event, and advance the
  lease row.
"""

from __future__ import annotations

import pytest
from psycopg.rows import dict_row

from agent_notes.core import db as coredb
from agent_notes.core.db import _conn
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
    """Return a dummy 768-dim vector (float list)."""
    return [0.0] * 768


def _ops_for_entity(entity_id: str) -> list[dict]:
    """Return all op_log rows for an entity, ordered by lamport."""
    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT * FROM op_log WHERE entity_id = %s ORDER BY lamport, op_id",
            (entity_id,),
        )
        return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Claim
# ---------------------------------------------------------------------------


class TestClaim:
    def test_claim_writes_claim_op_and_lease(self, default_project):
        wi = WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-LEASE-01",
            title="Claim me",
            status="open",
            embedding=_vec768(),
        )
        entity_id = wi["entity_id"]

        claimed = WorkItemModel.claim_work_item(
            project_id=default_project.id,
            identifier="WI-LEASE-01",
        )
        assert claimed["status"] == "claimed"

        # work_items cache reflects claimed
        fetched = WorkItemModel.get_work_item(default_project.id, "WI-LEASE-01")
        assert fetched is not None
        assert fetched["status"] == "claimed"

        # a lease row exists for the entity
        with _conn() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                "SELECT * FROM work_item_leases WHERE entity_id = %s",
                (entity_id,),
            )
            lease = cur.fetchone()
        assert lease is not None
        assert lease["actor_id"] == "agent:worker"

        # a claim op exists in op_log
        ops = _ops_for_entity(entity_id)
        claim_ops = [op for op in ops if op["op_type"] == "claim"]
        assert len(claim_ops) == 1

    def test_claim_set_status_is_child_of_claim_op(self, default_project):
        """The set_status(claimed) op's parent_op_ids must contain the claim
        op's op_id — NOT the entity_id (the claim parent-chaining bug fix)."""
        wi = WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-LEASE-02",
            title="Parent chain",
            status="open",
            embedding=_vec768(),
        )
        entity_id = wi["entity_id"]

        WorkItemModel.claim_work_item(
            project_id=default_project.id,
            identifier="WI-LEASE-02",
        )

        ops = _ops_for_entity(entity_id)
        claim_ops = [op for op in ops if op["op_type"] == "claim"]
        assert len(claim_ops) == 1
        claim_op_id = claim_ops[0]["op_id"]

        set_status_ops = [
            op
            for op in ops
            if op["op_type"] == "set_status"
            and (op.get("payload") or {}).get("status") == "claimed"
        ]
        assert len(set_status_ops) == 1

        parents = set(set_status_ops[0]["parent_op_ids"])
        assert claim_op_id in parents
        assert entity_id not in parents  # the bug was parenting on entity_id


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


class TestHeartbeat:
    def test_heartbeat_writes_op_and_event(self, default_project):
        wi = WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-LEASE-03",
            title="Heartbeat me",
            status="open",
            embedding=_vec768(),
        )
        entity_id = wi["entity_id"]

        WorkItemModel.claim_work_item(
            project_id=default_project.id,
            identifier="WI-LEASE-03",
            ttl_seconds=60,
        )

        # snapshot lease state before heartbeat
        with _conn() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                "SELECT * FROM work_item_leases WHERE entity_id = %s",
                (entity_id,),
            )
            before = cur.fetchone()
        assert before is not None
        prev_expires = before["expires_at"]
        prev_count = before["heartbeat_count"]

        WorkItemModel.heartbeat_work_item(
            project_id=default_project.id,
            identifier="WI-LEASE-03",
            ttl_seconds=120,
        )

        # a heartbeat op exists in op_log
        ops = _ops_for_entity(entity_id)
        heartbeat_ops = [op for op in ops if op["op_type"] == "heartbeat"]
        assert len(heartbeat_ops) == 1

        # an item.heartbeat event exists in op_log_events
        with _conn() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute("SELECT * FROM op_log_events WHERE event_type = 'item.heartbeat'")
            events = [dict(r) for r in cur.fetchall()]
        assert len(events) >= 1

        # the lease advanced: heartbeat_count incremented, expires_at moved out
        with _conn() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                "SELECT * FROM work_item_leases WHERE entity_id = %s",
                (entity_id,),
            )
            after = cur.fetchone()
        assert after is not None
        assert after["heartbeat_count"] == prev_count + 1
        assert after["expires_at"] > prev_expires


# ---------------------------------------------------------------------------
# Release
# ---------------------------------------------------------------------------


class TestRelease:
    def test_release_removes_lease_and_writes_ops(self, default_project):
        wi = WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-LEASE-04",
            title="Release me",
            status="open",
            embedding=_vec768(),
        )
        entity_id = wi["entity_id"]

        WorkItemModel.claim_work_item(
            project_id=default_project.id,
            identifier="WI-LEASE-04",
        )

        released = WorkItemModel.release_work_item(
            project_id=default_project.id,
            identifier="WI-LEASE-04",
        )
        assert released["status"] == "open"

        # work_items cache reflects open again
        fetched = WorkItemModel.get_work_item(default_project.id, "WI-LEASE-04")
        assert fetched is not None
        assert fetched["status"] == "open"

        # the lease row is deleted
        with _conn() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                "SELECT 1 FROM work_item_leases WHERE entity_id = %s",
                (entity_id,),
            )
            assert cur.fetchone() is None

        # a release op and a set_status op exist in op_log
        ops = _ops_for_entity(entity_id)
        release_ops = [op for op in ops if op["op_type"] == "release"]
        assert len(release_ops) == 1
        release_op_id = release_ops[0]["op_id"]

        set_status_ops = [
            op
            for op in ops
            if op["op_type"] == "set_status" and (op.get("payload") or {}).get("status") == "open"
        ]
        assert len(set_status_ops) == 1

        # the set_status op is parented on the release op's op_id, NOT entity_id
        parents = set(set_status_ops[0]["parent_op_ids"])
        assert release_op_id in parents
        assert entity_id not in parents


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestLeaseErrors:
    def test_claim_already_claimed_raises(self, default_project):
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-LEASE-05",
            title="Double claim",
            status="open",
            embedding=_vec768(),
        )
        WorkItemModel.claim_work_item(
            project_id=default_project.id,
            identifier="WI-LEASE-05",
        )
        with pytest.raises(ValueError):
            WorkItemModel.claim_work_item(
                project_id=default_project.id,
                identifier="WI-LEASE-05",
            )

    def test_heartbeat_unclaimed_raises(self, default_project):
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-LEASE-06",
            title="Heartbeat without claim",
            status="open",
            embedding=_vec768(),
        )
        with pytest.raises(ValueError):
            WorkItemModel.heartbeat_work_item(
                project_id=default_project.id,
                identifier="WI-LEASE-06",
            )

    def test_claim_non_open_raises(self, default_project):
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-LEASE-07",
            title="Closed item",
            status="open",
            embedding=_vec768(),
        )
        WorkItemModel.close_work_item(default_project.id, "WI-LEASE-07")
        with pytest.raises(ValueError):
            WorkItemModel.claim_work_item(
                project_id=default_project.id,
                identifier="WI-LEASE-07",
            )


# ---------------------------------------------------------------------------
# change_log audit trail (BC-027)
# ---------------------------------------------------------------------------


def _change_log_for(identifier: str) -> list[dict]:
    """Return change_log rows for an identifier, oldest first."""
    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT * FROM change_log WHERE kind = 'work_item' AND identifier = %s ORDER BY id",
            (identifier,),
        )
        return [dict(r) for r in cur.fetchall()]


class TestLeaseChangeLog:
    """BC-027: native (degrade) lease ops must write change_log like the regista path."""

    def test_claim_heartbeat_release_write_change_log(self, default_project):
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-LEASE-CL",
            title="Lease audit trail",
            status="open",
            embedding=_vec768(),
        )
        WorkItemModel.claim_work_item(
            project_id=default_project.id,
            identifier="WI-LEASE-CL",
            ttl_seconds=60,
        )
        WorkItemModel.heartbeat_work_item(
            project_id=default_project.id,
            identifier="WI-LEASE-CL",
            ttl_seconds=120,
        )
        WorkItemModel.release_work_item(
            project_id=default_project.id,
            identifier="WI-LEASE-CL",
        )

        events = {row["event"]: row for row in _change_log_for("WI-LEASE-CL")}
        assert {"claimed", "heartbeat", "released"} <= set(events)
        for event in ("claimed", "heartbeat", "released"):
            assert events[event]["actor"] == "agent:worker"
        assert events["claimed"]["payload"]["ttl_seconds"] == 60
        assert events["heartbeat"]["payload"]["ttl_seconds"] == 120
