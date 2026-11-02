"""Op identity: a repeated op is a new op, never a collision.

``op_log.op_id`` is UNIQUE and was a pure content hash of
``(entity_type, op_type, payload, parent_op_ids)``. Every native writer
parents the entity itself, so an op whose payload repeated collided with the
earlier one and the write failed. Some writers added a timestamp to dodge it;
heartbeat, force-close, a repeated review note and a field set back to an
earlier value did not. ``commit_op`` now hashes the op's lamport into every
op but ``create`` (``kernel.OP_SEQ_KEY``).
"""

from __future__ import annotations

import os

import pytest
from psycopg.rows import dict_row

from agent_notes.core import db as coredb
from agent_notes.core import kernel, verifier
from agent_notes.core.db import _conn
from agent_notes.core.work_item_model import WorkItemModel

# Import the fixture so pytest discovers it
from tests.conftest import ephemeral_db  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")


@pytest.fixture
def project():
    ws = coredb.get_or_create_workspace("default", "Default Workspace")
    return coredb.get_or_create_project(
        ws.id, slug="opid", name="opid", repo_root=os.path.abspath("/projects/opid")
    )


def _file(project_id: int, identifier: str) -> dict:
    return WorkItemModel.file_work_item(
        project_id=project_id,
        identifier=identifier,
        title=identifier,
        status="open",
        embedding=[0.0] * 768,
    )


def _ops(entity_id: str, op_type: str) -> list[dict]:
    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT op_id, lamport, payload FROM op_log "
            "WHERE entity_id = %s AND op_type = %s ORDER BY lamport",
            (entity_id, op_type),
        )
        return [dict(r) for r in cur.fetchall()]


def _verified(entity_id: str) -> None:
    result = verifier.verify_entity(entity_id)
    assert result.ok(), [v.message for v in result.violations]


def test_a_second_identical_heartbeat_extends_the_lease(project):
    wi = _file(project.id, "OP-1")
    WorkItemModel.claim_work_item(project.id, "OP-1", 600)
    first = WorkItemModel.heartbeat_work_item(project.id, "OP-1", 600)
    second = WorkItemModel.heartbeat_work_item(project.id, "OP-1", 600)
    assert second["heartbeat_count"] == first["heartbeat_count"] + 1
    assert second["expires_at"] >= first["expires_at"]
    beats = _ops(wi["entity_id"], "heartbeat")
    assert len(beats) == 2 and beats[0]["op_id"] != beats[1]["op_id"]
    _verified(wi["entity_id"])


def test_a_field_set_back_to_an_earlier_value(project):
    wi = _file(project.id, "OP-2")
    WorkItemModel.update_work_item(project.id, "OP-2", title="renamed")
    WorkItemModel.update_work_item(project.id, "OP-2", title="OP-2-final")
    WorkItemModel.update_work_item(project.id, "OP-2", title="renamed")
    assert WorkItemModel.get_work_item(project.id, "OP-2")["title"] == "renamed"
    assert len(_ops(wi["entity_id"], "set_field")) == 3
    _verified(wi["entity_id"])


def test_force_close_after_a_reopen(project):
    wi = _file(project.id, "OP-3")
    WorkItemModel.close_work_item(project.id, "OP-3", force=True)
    WorkItemModel.update_work_item(project.id, "OP-3", status="open", force=True)
    closed = WorkItemModel.close_work_item(project.id, "OP-3", force=True)
    assert closed["status"] == "closed"
    assert len(_ops(wi["entity_id"], "close")) == 2
    _verified(wi["entity_id"])


def test_the_same_review_note_twice(project):
    wi = _file(project.id, "OP-4")
    for _ in range(2):
        WorkItemModel.update_work_item(project.id, "OP-4", status="in_progress")
        WorkItemModel.update_work_item(project.id, "OP-4", status="in_review")
        WorkItemModel.review_transition(project.id, "OP-4", "reject", "needs tests")
    _verified(wi["entity_id"])


def test_every_op_but_create_hashes_its_lamport(project):
    wi = _file(project.id, "OP-5")
    WorkItemModel.update_work_item(project.id, "OP-5", title="x")
    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT op_type, lamport, payload FROM op_log WHERE entity_id = %s",
            (wi["entity_id"],),
        )
        ops = cur.fetchall()
    for op in ops:
        if op["op_type"] == "create":
            assert kernel.OP_SEQ_KEY not in op["payload"]
        else:
            assert op["payload"][kernel.OP_SEQ_KEY] == op["lamport"]


def test_verifier_flags_an_op_moved_in_the_fold_order(project):
    wi = _file(project.id, "OP-6")
    WorkItemModel.update_work_item(project.id, "OP-6", title="moved")
    op = _ops(wi["entity_id"], "set_field")[0]
    with _conn() as conn:
        conn.execute(
            "UPDATE op_log SET lamport = lamport + 1000000 WHERE op_id = %s", (op["op_id"],)
        )
        conn.commit()
    result = verifier.verify_entity(wi["entity_id"])
    assert not result.ok()
    assert any("does not match lamport" in v.message for v in result.violations)


def test_the_same_cross_project_link_twice(project):
    _file(project.id, "OP-7")
    for _ in range(2):
        WorkItemModel.add_cross_project_link(
            from_project_id=project.id,
            from_identifier="OP-7",
            to_project_slug="foreign-project",
            to_identifier="WI-9",
        )
    with _conn() as conn:
        rows = conn.execute(
            "SELECT op_id, entity_id FROM op_log WHERE op_type = 'add_link' "
            "AND payload->>'from_identifier' = 'OP-7'"
        ).fetchall()
    # Two events, each the root of its own entity: entity_id is its op_id.
    assert len(rows) == 2
    assert all(r["entity_id"] == r["op_id"] for r in rows)
    assert rows[0]["entity_id"] != rows[1]["entity_id"]


def test_request_and_wait_entities_are_named_by_their_op(project):
    request = WorkItemModel.request_work_item(
        project_id=project.id, target_project_slug="foreign-project", title="please", body="b"
    )
    wait = WorkItemModel.wait_on_work_item(
        project_id=project.id, target_project_slug="foreign-project", target_identifier="WI-9"
    )
    for created, op_type in ((request, "request"), (wait, "wait")):
        (op,) = _ops(created["entity_id"], op_type)
        assert op["op_id"] == created["entity_id"]
        assert op["payload"][kernel.OP_SEQ_KEY] == op["lamport"]
    _verified(request["entity_id"])
    _verified(wait["entity_id"])


def test_gate_waiver_stays_review_bookkeeping(project):
    """The new key must not make a waiver look like implementation work."""
    from agent_notes.core.work_item import _independence

    op = {
        "op_type": "set_field",
        "payload": {
            "diagnostic_keys": {"gate_attestation": {"status": "waived"}},
            "producer": {"model": None},
            kernel.OP_SEQ_KEY: 42,
        },
    }
    assert _independence._is_bookkeeping(op, None)
