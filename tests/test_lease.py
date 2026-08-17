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
            actor_id="agent-a",
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
        assert lease["actor_id"] == "agent-a"

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
            actor_id="agent-a",
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
            actor_id="agent-a",
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
            actor_id="agent-a",
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
            actor_id="agent-a",
        )

        released = WorkItemModel.release_work_item(
            project_id=default_project.id,
            identifier="WI-LEASE-04",
            actor_id="agent-a",
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
            actor_id="agent-a",
        )
        with pytest.raises(ValueError):
            WorkItemModel.claim_work_item(
                project_id=default_project.id,
                identifier="WI-LEASE-05",
                actor_id="agent-b",
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
                actor_id="agent-a",
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
                actor_id="agent-a",
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
            actor_id="agent-a",
            ttl_seconds=60,
        )
        WorkItemModel.heartbeat_work_item(
            project_id=default_project.id,
            identifier="WI-LEASE-CL",
            actor_id="agent-a",
            ttl_seconds=120,
        )
        WorkItemModel.release_work_item(
            project_id=default_project.id,
            identifier="WI-LEASE-CL",
            actor_id="agent-a",
        )

        events = {row["event"]: row for row in _change_log_for("WI-LEASE-CL")}
        assert {"claimed", "heartbeat", "released"} <= set(events)
        for event in ("claimed", "heartbeat", "released"):
            assert events[event]["actor"] == "agent-a"
        assert events["claimed"]["payload"]["ttl_seconds"] == 60
        assert events["heartbeat"]["payload"]["ttl_seconds"] == 120


class TestLineageGateWI068:
    """WI-068: the native lease/delete verbs are lineage-gated like everything
    else (commit 3d2552e gated only the regista lease verbs), the per-invocation
    ``model_lineage`` override takes effect when the env declares nothing, and
    ``delete`` stamps the actor on its tombstone op instead of committing it
    anonymous."""

    def test_lease_verbs_refuse_undeclared_and_accept_explicit_lineage(
        self, default_project, monkeypatch
    ):
        from agent_notes.core.actor import UndeclaredLineageError

        # File while the session lineage is still present.
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-LIN-NAT-01",
            title="Native lease gate",
            status="open",
            embedding=_vec768(),
        )

        # Env declares nothing: every lease verb refuses before writing.
        monkeypatch.delenv("AGENT_NOTES_MODEL_LINEAGE", raising=False)
        with pytest.raises(UndeclaredLineageError):
            WorkItemModel.claim_work_item(
                project_id=default_project.id,
                identifier="WI-LIN-NAT-01",
                actor_id="agent-a",
                ttl_seconds=60,
            )
        # Nothing was written by the refused claim.
        with _conn() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                "SELECT 1 FROM work_item_leases l JOIN work_items w"
                " ON w.entity_id = l.entity_id WHERE w.identifier = %s",
                ("WI-LIN-NAT-01",),
            )
            assert cur.fetchone() is None

        # The explicit per-invocation declaration takes effect over env absence
        # for the whole lease lifecycle.
        claimed = WorkItemModel.claim_work_item(
            project_id=default_project.id,
            identifier="WI-LIN-NAT-01",
            actor_id="agent-a",
            ttl_seconds=60,
            model_lineage="claude-opus",
        )
        assert claimed["status"] == "claimed"
        with pytest.raises(UndeclaredLineageError):
            WorkItemModel.heartbeat_work_item(
                project_id=default_project.id,
                identifier="WI-LIN-NAT-01",
                actor_id="agent-a",
                ttl_seconds=60,
            )
        WorkItemModel.heartbeat_work_item(
            project_id=default_project.id,
            identifier="WI-LIN-NAT-01",
            actor_id="agent-a",
            ttl_seconds=60,
            model_lineage="claude-opus",
        )
        with pytest.raises(UndeclaredLineageError):
            WorkItemModel.release_work_item(
                project_id=default_project.id,
                identifier="WI-LIN-NAT-01",
                actor_id="agent-a",
            )
        released = WorkItemModel.release_work_item(
            project_id=default_project.id,
            identifier="WI-LIN-NAT-01",
            actor_id="agent-a",
            model_lineage="claude-opus",
        )
        assert released["status"] == "open"

    def test_delete_refuses_undeclared_and_stamps_actor_on_tombstone(
        self, default_project, monkeypatch
    ):
        from agent_notes.core.actor import UndeclaredLineageError

        wi = WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-LIN-DEL-01",
            title="Delete gate + actor stamp",
            status="open",
            embedding=_vec768(),
        )
        entity_id = wi["entity_id"]

        monkeypatch.delenv("AGENT_NOTES_MODEL_LINEAGE", raising=False)
        with pytest.raises(UndeclaredLineageError):
            WorkItemModel.delete_work_item(default_project.id, "WI-LIN-DEL-01")
        # The refused delete wrote nothing: the item is still in the cache.
        assert WorkItemModel.get_work_item(default_project.id, "WI-LIN-DEL-01") is not None

        assert WorkItemModel.delete_work_item(
            default_project.id,
            "WI-LIN-DEL-01",
            actor_id="deleter-agent",
            model_lineage="claude-opus",
        )
        # The tombstone snapshot op carries the actor (pre-WI-068 it was NULL).
        snapshots = [op for op in _ops_for_entity(entity_id) if op["op_type"] == "snapshot"]
        assert snapshots, "delete must write the tombstone snapshot op"
        assert snapshots[-1]["actor_id"] == "deleter-agent"
        # ...and so does the change_log row.
        deleted_rows = [r for r in _change_log_for("WI-LIN-DEL-01") if r["event"] == "deleted"]
        assert deleted_rows and deleted_rows[-1]["actor"] == "deleter-agent"


# ---------------------------------------------------------------------------
# Lease-op attribution (WI-069)
# ---------------------------------------------------------------------------


class TestLeaseAttributionWI069:
    """WI-069: the native lease-op payloads and their change_log rows record
    the lineage the WI-068 gate checked, so the op-chain can say WHICH lineage
    held a lease after the fact (the regista path already carries it in the
    claim's ``actor_metadata``)."""

    def test_lease_ops_and_change_log_stamp_lineage(self, default_project):
        wi = WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-LEASE-ATTR-01",
            title="Lease attribution",
            status="open",
            embedding=_vec768(),
        )
        entity_id = wi["entity_id"]

        # No per-call declaration: the gate resolves the session env lineage
        # (conftest sets claude-opus) — that resolved value must be stamped.
        WorkItemModel.claim_work_item(
            project_id=default_project.id,
            identifier="WI-LEASE-ATTR-01",
            actor_id="agent-a",
            ttl_seconds=60,
        )
        WorkItemModel.heartbeat_work_item(
            project_id=default_project.id,
            identifier="WI-LEASE-ATTR-01",
            actor_id="agent-a",
            ttl_seconds=120,
        )
        WorkItemModel.release_work_item(
            project_id=default_project.id,
            identifier="WI-LEASE-ATTR-01",
            actor_id="agent-a",
        )

        ops = {op["op_type"]: op for op in _ops_for_entity(entity_id)}
        for op_type in ("claim", "heartbeat", "release"):
            assert ops[op_type]["payload"].get("model_lineage") == "claude-opus", op_type

        events = {row["event"]: row for row in _change_log_for("WI-LEASE-ATTR-01")}
        for event in ("claimed", "heartbeat", "released"):
            assert events[event]["payload"].get("model_lineage") == "claude-opus", event

    def test_lease_ops_stamp_explicit_lineage_param(self, default_project, monkeypatch):
        """WI-069: with no env declaration, the per-invocation ``model_lineage``
        is what lands in the claim op payload and the claimed change_log row —
        the stamp is the gate-resolved declaration, not a raw env echo."""
        wi = WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-LEASE-ATTR-02",
            title="Lease attribution (explicit param)",
            status="open",
            embedding=_vec768(),
        )
        entity_id = wi["entity_id"]

        monkeypatch.delenv("AGENT_NOTES_MODEL_LINEAGE", raising=False)
        WorkItemModel.claim_work_item(
            project_id=default_project.id,
            identifier="WI-LEASE-ATTR-02",
            actor_id="agent-a",
            ttl_seconds=60,
            model_lineage="claude-opus",
        )

        ops = {op["op_type"]: op for op in _ops_for_entity(entity_id)}
        assert ops["claim"]["payload"].get("model_lineage") == "claude-opus"
        events = {row["event"]: row for row in _change_log_for("WI-LEASE-ATTR-02")}
        assert events["claimed"]["payload"].get("model_lineage") == "claude-opus"
