"""Tests for the cross-project trigger loop (Plan 008 P3).

Uses testcontainers for a real Postgres so NOTIFY/LISTEN works.
"""

from __future__ import annotations

import json

import psycopg

from agent_notes.core.db import _conn
from agent_notes.trigger_loop import (
    _build_wake_event,
    _process_event,
    _resolve_project_wake_channel,
    _route_item_closed,
    _route_link_added,
    _route_request_created,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _notify_payload(event_type: str, payload: dict) -> dict:
    return {"event_type": event_type, "payload": payload}


# ---------------------------------------------------------------------------
# Unit tests for routing functions
# ---------------------------------------------------------------------------


def test_resolve_project_wake_channel_found(ephemeral_db):
    with _conn() as conn:
        # Project "default" is created by setup.
        channel = _resolve_project_wake_channel(conn, project_slug="default")
        # Default project has no wake_channel set.
        assert channel is None


def test_resolve_project_wake_channel_missing(ephemeral_db):
    with _conn() as conn:
        channel = _resolve_project_wake_channel(conn, project_slug="nonexistent")
        assert channel is None


def test_build_wake_event():
    event = _build_wake_event("request.created", {"target_project": "foo"}, "agent-notes")
    assert event["source"] == "agent-notes"
    assert event["kind"] == "trigger-loop"
    assert event["wake"] is True
    assert "request.created" in event["content"]
    assert event["meta"]["agent_notes_event_type"] == "request.created"


def test_route_request_created_no_target_project(ephemeral_db):
    buffer: list[dict] = []
    with _conn() as conn:
        _route_request_created(conn, {}, None, "secret", "src", buffer)
    assert len(buffer) == 0


def test_route_request_created_unknown_target(ephemeral_db):
    buffer: list[dict] = []
    with _conn() as conn:
        _route_request_created(
            conn, {"target_project": "unknown-project"}, None, "secret", "src", buffer
        )
    # No default target and no wake_channel on project → nothing buffered.
    assert len(buffer) == 0


def test_route_request_created_with_default_target(ephemeral_db):
    buffer: list[dict] = []
    with _conn() as conn:
        _route_request_created(
            conn,
            {"target_project": "unknown-project"},
            "http://example.com/wake",
            "secret",
            "src",
            buffer,
        )
    assert len(buffer) == 1
    assert buffer[0]["target"] == "http://example.com/wake"
    assert buffer[0]["event"]["meta"]["target_project"] == "unknown-project"


def test_route_link_added_not_cross_project(ephemeral_db):
    buffer: list[dict] = []
    with _conn() as conn:
        _route_link_added(
            conn, {"cross_project": False, "to_project_id": 1}, None, "secret", "src", buffer
        )
    assert len(buffer) == 0


def test_route_link_added_cross_project_no_target(ephemeral_db):
    buffer: list[dict] = []
    with _conn() as conn:
        _route_link_added(
            conn, {"cross_project": True, "to_project_id": 99999}, None, "secret", "src", buffer
        )
    assert len(buffer) == 0


def test_route_item_closed_no_entity(ephemeral_db):
    buffer: list[dict] = []
    with _conn() as conn:
        _route_item_closed(conn, {}, None, "secret", "src", buffer)
    assert len(buffer) == 0


def test_route_item_closed_with_dependents(ephemeral_db):
    """Close a work item that blocks another local item via links."""
    from agent_notes.core.db import add_vocabulary, get_or_create_project, get_or_create_workspace
    from agent_notes.core.links import add_link
    from agent_notes.core.work_item_model import WorkItemModel

    ws = get_or_create_workspace("test-ws", "Test Workspace")
    add_vocabulary(ws.id, "wi_kind", "todo")
    add_vocabulary(ws.id, "wi_status", "open")
    add_vocabulary(ws.id, "wi_severity", "medium")
    proj_a = get_or_create_project(ws.id, slug="proj-a", name="Project A")
    proj_b = get_or_create_project(ws.id, slug="proj-b", name="Project B")

    # Set wake_channel on proj_b so routing has a target.
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE projects SET wake_channel = %s WHERE id = %s",
            ("http://proj-b/wake", proj_b.id),
        )
        conn.commit()

    # Create two work items.
    blocker = WorkItemModel.file_work_item(
        project_id=proj_a.id,
        identifier="BLK-001",
        title="Blocker",
        kind="todo",
        status="open",
    )
    WorkItemModel.file_work_item(
        project_id=proj_b.id,
        identifier="BLD-001",
        title="Blocked",
        kind="todo",
        status="open",
    )

    # Add a same-project link: blocked is blocked by blocker.
    # Wait, links uses integer IDs. blocked is in proj_b, blocker in proj_a.
    add_link(
        from_kind="work_item",
        from_workspace=ws.id,
        from_project=proj_b.id,
        from_identifier="BLD-001",
        to_kind="work_item",
        to_workspace=ws.id,
        to_project=proj_a.id,
        to_identifier="BLK-001",
        relationship="blocks",
    )

    buffer: list[dict] = []
    with _conn() as conn:
        _route_item_closed(
            conn,
            {"entity_id": blocker["entity_id"], "identifier": "BLK-001"},
            None,
            "secret",
            "src",
            buffer,
        )

    assert len(buffer) == 1
    event = buffer[0]["event"]
    assert event["meta"]["unblocked_project"] == "proj-b"
    assert event["meta"]["unblocked_identifier"] == "BLD-001"
    assert buffer[0]["target"] == "http://proj-b/wake"


# ---------------------------------------------------------------------------
# Integration test: LISTEN/NOTIFY
# ---------------------------------------------------------------------------


def test_trigger_loop_listen_notify(ephemeral_db):
    """Verify the trigger loop actually receives NOTIFY events."""

    from agent_notes.core.config import resolve_dsn

    dsn = resolve_dsn()
    conn = psycopg.connect(dsn, autocommit=True)
    conn.execute("LISTEN agent_notes_op_log_events")

    # Emit a NOTIFY from another connection.
    conn2 = psycopg.connect(dsn, autocommit=True)
    payload = json.dumps({"event_type": "request.created", "payload": {"target_project": "foo"}})
    conn2.execute(
        "SELECT pg_notify('agent_notes_op_log_events', %s)",
        (payload,),
    )
    conn2.close()

    # Wait for the NOTIFY.
    received = []
    for n in conn.notifies(timeout=2.0):
        received.append(n)
        break
    conn.close()

    assert len(received) == 1
    data = json.loads(received[0].payload)
    assert data["event_type"] == "request.created"


# ---------------------------------------------------------------------------
# Integration test: _process_event end-to-end
# ---------------------------------------------------------------------------


def test_process_event_request_created(ephemeral_db):
    from agent_notes.core.db import get_or_create_project, get_or_create_workspace

    ws = get_or_create_workspace("test-ws-2", "Test Workspace 2")
    proj = get_or_create_project(ws.id, slug="target-proj", name="Target Project")

    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE projects SET wake_channel = %s WHERE id = %s",
            ("http://target/wake", proj.id),
        )
        conn.commit()

    buffer: list[dict] = []
    with _conn() as conn:
        _process_event(
            conn,
            _notify_payload("request.created", {"target_project": "target-proj"}),
            None,
            "secret",
            "src",
            buffer,
        )

    assert len(buffer) == 1
    assert buffer[0]["target"] == "http://target/wake"


def test_process_event_item_closed_no_links(ephemeral_db):
    from agent_notes.core.db import add_vocabulary, get_or_create_project, get_or_create_workspace
    from agent_notes.core.work_item_model import WorkItemModel

    ws = get_or_create_workspace("test-ws-3", "Test Workspace 3")
    add_vocabulary(ws.id, "wi_kind", "todo")
    add_vocabulary(ws.id, "wi_status", "open")
    add_vocabulary(ws.id, "wi_severity", "medium")
    proj = get_or_create_project(ws.id, slug="proj-c", name="Project C")

    wi = WorkItemModel.file_work_item(
        project_id=proj.id,
        identifier="WI-001",
        title="Standalone",
        kind="todo",
        status="open",
    )

    buffer: list[dict] = []
    with _conn() as conn:
        _process_event(
            conn,
            _notify_payload("item.closed", {"entity_id": wi["entity_id"], "identifier": "WI-001"}),
            None,
            "secret",
            "src",
            buffer,
        )

    assert len(buffer) == 0
