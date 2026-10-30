"""Integration tests for Plan 008 P4 — claim / heartbeat / release lease lifecycle.

These cover the local-lease code path (``claim_work_item``,
``heartbeat_work_item``, ``release_work_item``):

- a claim records a lease and leaves the lifecycle status alone (WI-086:
  moving it to the legacy ``claimed`` state stranded the item, since the
  canonical workflow has no transitions out of ``claimed``).
- releasing a legacy ``claimed`` row writes a ``set_status(open)`` op parented
  on the ``release`` op's ``op_id`` (NOT the ``entity_id``).
- the heartbeat path must write both an op and an event, and advance the
  lease row.
"""

from __future__ import annotations

import os

import pytest
from psycopg.rows import dict_row

from agent_notes.core import db as coredb
from agent_notes.core import kernel
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
        repo_root=os.path.abspath("/projects/sf2"),
    )
    return proj


def _vec768():
    """Return a dummy 768-dim vector (float list)."""
    return [0.0] * 768


def _file_open(project_id: int, identifier: str) -> dict:
    return WorkItemModel.file_work_item(
        project_id=project_id,
        identifier=identifier,
        title=identifier,
        status="open",
        embedding=_vec768(),
    )


def _legacy_claimed(project_id: int, identifier: str) -> dict:
    """An item in the legacy ``claimed`` state with a live lease, as the old
    claim path left it (a ``set_status(claimed)`` op plus a lease row)."""
    wi = _file_open(project_id, identifier)
    WorkItemModel.update_work_item(project_id, identifier, status="claimed", force=True)
    with _conn() as conn:
        conn.execute(
            "INSERT INTO work_item_leases (entity_id, actor_id, expires_at) "
            "VALUES (%s, 'agent:worker', now() + interval '1 hour')",
            (wi["entity_id"],),
        )
        conn.commit()
    return WorkItemModel.get_work_item(project_id, identifier)


def _lease(entity_id: str) -> dict | None:
    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute("SELECT * FROM work_item_leases WHERE entity_id = %s", (entity_id,))
        return cur.fetchone()


def _claimable_ids(project_id: int) -> set[str]:
    return {r["identifier"] for r in WorkItemModel.claimable_work_items(project_id=project_id)}


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
        # The lease is a separate axis: the lifecycle status is unchanged.
        assert claimed["status"] == "open"

        fetched = WorkItemModel.get_work_item(default_project.id, "WI-LEASE-01")
        assert fetched is not None
        assert fetched["status"] == "open"

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

    def test_claim_writes_no_status_op(self, default_project):
        """WI-086: a claim must not move the lifecycle (no ``set_status`` op)."""
        wi = _file_open(default_project.id, "WI-LEASE-02")
        WorkItemModel.claim_work_item(project_id=default_project.id, identifier="WI-LEASE-02")
        ops = _ops_for_entity(wi["entity_id"])
        assert [op for op in ops if op["op_type"] == "set_status"] == []


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
    def test_release_removes_lease_and_keeps_status(self, default_project):
        wi = _file_open(default_project.id, "WI-LEASE-04")
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
        assert _lease(entity_id) is None

        # a release op exists; there is no status op (the status never moved)
        ops = _ops_for_entity(entity_id)
        assert len([op for op in ops if op["op_type"] == "release"]) == 1
        assert [op for op in ops if op["op_type"] == "set_status"] == []

    def test_release_of_legacy_claimed_row_reopens(self, default_project):
        """A row the old claim path left in ``claimed`` goes back to ``open``,
        via a ``set_status`` op parented on the release op (not the entity)."""
        wi = _legacy_claimed(default_project.id, "WI-LEASE-04L")
        assert wi["status"] == "claimed"
        entity_id = wi["entity_id"]

        released = WorkItemModel.release_work_item(default_project.id, "WI-LEASE-04L")
        assert released["status"] == "open"
        assert _lease(entity_id) is None

        ops = _ops_for_entity(entity_id)
        release_ops = [op for op in ops if op["op_type"] == "release"]
        assert len(release_ops) == 1
        reopen_ops = [
            op
            for op in ops
            if op["op_type"] == "set_status" and (op.get("payload") or {}).get("status") == "open"
        ]
        assert len(reopen_ops) == 1
        parents = set(reopen_ops[0]["parent_op_ids"])
        assert release_ops[0]["op_id"] in parents
        assert entity_id not in parents


# ---------------------------------------------------------------------------
# WI-086: a claim must not strand the item outside the canonical workflow
# ---------------------------------------------------------------------------


class TestClaimThenWorkflow:
    def test_claim_then_start_then_submit_for_review(self, default_project):
        wi = _file_open(default_project.id, "WI-086-A")
        entity_id = wi["entity_id"]

        claimed = WorkItemModel.claim_work_item(default_project.id, "WI-086-A", 14400)
        assert claimed["status"] == "open"

        started = WorkItemModel.update_work_item(
            default_project.id, "WI-086-A", status="in_progress"
        )
        assert started["status"] == "in_progress"
        # The lease survives the lifecycle move and can still be heartbeated.
        assert _lease(entity_id)["actor_id"] == "agent:worker"
        lease = WorkItemModel.heartbeat_work_item(default_project.id, "WI-086-A", 600)
        assert lease["heartbeat_count"] >= 1

        submitted = WorkItemModel.update_work_item(
            default_project.id, "WI-086-A", status="in_review"
        )
        assert submitted["status"] == "in_review"

        # Releasing drops the lease and leaves the item in review.
        released = WorkItemModel.release_work_item(default_project.id, "WI-086-A")
        assert released["status"] == "in_review"
        assert _lease(entity_id) is None

    def test_live_lease_excludes_item_from_claimable(self, default_project):
        wi = _file_open(default_project.id, "WI-086-B")
        assert "WI-086-B" in _claimable_ids(default_project.id)

        WorkItemModel.claim_work_item(default_project.id, "WI-086-B")
        assert "WI-086-B" not in _claimable_ids(default_project.id)
        # ``ready`` is "open and unblocked"; the lease is the claimable filter.
        ready = WorkItemModel.ready_work_items(project_id=default_project.id)
        assert "WI-086-B" in {r["identifier"] for r in ready}

        # An expired lease no longer excludes it, and the sweep removes it.
        with _conn() as conn:
            conn.execute(
                "UPDATE work_item_leases SET expires_at = now() - interval '1 second' "
                "WHERE entity_id = %s",
                (wi["entity_id"],),
            )
            conn.commit()
        assert "WI-086-B" in _claimable_ids(default_project.id)
        with _conn() as conn:
            conn.execute("SELECT sweep_expired_leases()")
            conn.commit()
        assert _lease(wi["entity_id"]) is None

        # A claim then a release leaves it claimable again.
        WorkItemModel.claim_work_item(default_project.id, "WI-086-B")
        WorkItemModel.release_work_item(default_project.id, "WI-086-B")
        assert "WI-086-B" in _claimable_ids(default_project.id)

    def test_in_progress_item_can_be_claimed(self, default_project):
        """Work started without a claim (or whose lease lapsed) can be leased."""
        wi = _file_open(default_project.id, "WI-086-C")
        WorkItemModel.update_work_item(default_project.id, "WI-086-C", status="in_progress")
        claimed = WorkItemModel.claim_work_item(default_project.id, "WI-086-C")
        assert claimed["status"] == "in_progress"
        assert _lease(wi["entity_id"]) is not None

    def test_legacy_claimed_row_can_start(self, default_project):
        """A row stranded in ``claimed`` by the old claim path is an open item
        holding a lease: it starts as ``open`` would, and keeps its lease."""
        wi = _legacy_claimed(default_project.id, "WI-086-D")
        started = WorkItemModel.update_work_item(
            default_project.id, "WI-086-D", status="in_progress"
        )
        assert started["status"] == "in_progress"
        assert _lease(wi["entity_id"]) is not None
        submitted = WorkItemModel.update_work_item(
            default_project.id, "WI-086-D", status="in_review"
        )
        assert submitted["status"] == "in_review"

    def test_fold_agrees_with_cache_after_claim(self, default_project):
        wi = _file_open(default_project.id, "WI-086-E")
        WorkItemModel.claim_work_item(default_project.id, "WI-086-E")
        with _conn() as conn:
            state = kernel.fold_work_item_state(conn, wi["entity_id"])
        assert state["status"] == "open"


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

    def test_claim_in_review_raises(self, default_project):
        _file_open(default_project.id, "WI-LEASE-07R")
        WorkItemModel.update_work_item(default_project.id, "WI-LEASE-07R", status="in_progress")
        WorkItemModel.update_work_item(default_project.id, "WI-LEASE-07R", status="in_review")
        with pytest.raises(ValueError, match="Cannot claim"):
            WorkItemModel.claim_work_item(default_project.id, "WI-LEASE-07R")

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
