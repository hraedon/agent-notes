"""Integration tests for core/notify.py and the change_log NOTIFY trigger.

These tests verify that inserting a change_log row fires the Postgres NOTIFY
and the payload is received by `listen()`. Must run against real Postgres
(triggers cannot be mocked).
"""

from __future__ import annotations

import threading
import time

import psycopg
import pytest

from agent_notes.core import db as coredb
from agent_notes.core import notify


@pytest.fixture(scope="module")
def pg(ephemeral_db):
    return ephemeral_db


class TestNotifyTrigger:
    def test_notify_fires_on_change_log_insert(self, pg: str) -> None:
        """Insert a change_log row; the NOTIFY payload should arrive."""
        ws = coredb.get_or_create_workspace("notify-ws", "Notify WS")
        proj = coredb.get_or_create_project(ws.id, "notify-proj", "Notify Proj")

        received: list[dict] = []
        error_holder: list[Exception] = []

        def _listener() -> None:
            try:
                with notify.listen(dsn=pg, poll_timeout=3.0) as events:
                    for payload in events:
                        received.append(payload)
                        break  # stop after first notification
            except Exception as exc:
                error_holder.append(exc)

        listener_thread = threading.Thread(target=_listener, daemon=True)
        listener_thread.start()

        # Give the listener a moment to LISTEN before we INSERT.
        time.sleep(0.3)

        with psycopg.connect(pg) as conn:
            conn.execute(
                "INSERT INTO change_log (kind, workspace_id, project_id, identifier, event) "
                "VALUES (%s, %s, %s, %s, %s)",
                ("breadcrumb", ws.id, proj.id, "BC-NOTIFY-001", "filed"),
            )
            conn.commit()

        listener_thread.join(timeout=6.0)

        if error_holder:
            raise error_holder[0]

        assert len(received) >= 1, "Expected at least one NOTIFY payload"
        payload = received[0]
        assert payload.get("kind") == "breadcrumb"
        assert payload.get("event") == "filed"
        assert payload.get("identifier") == "BC-NOTIFY-001"

    def test_notify_payload_has_expected_fields(self, pg: str) -> None:
        """Payload shape matches the trigger's json_build_object (§4.1)."""
        ws = coredb.get_or_create_workspace("notify-fields-ws", "Notify Fields WS")
        proj = coredb.get_or_create_project(ws.id, "notify-fields-proj", "Notify Fields Proj")

        received: list[dict] = []

        def _listener() -> None:
            with notify.listen(dsn=pg, poll_timeout=3.0) as events:
                for payload in events:
                    received.append(payload)
                    break

        t = threading.Thread(target=_listener, daemon=True)
        t.start()
        time.sleep(0.3)

        with psycopg.connect(pg) as conn:
            conn.execute(
                "INSERT INTO change_log (kind, workspace_id, project_id, identifier, event) "
                "VALUES (%s, %s, %s, %s, %s)",
                ("memory", ws.id, proj.id, "mem-notify-shape", "updated"),
            )
            conn.commit()

        t.join(timeout=6.0)

        assert received
        p = received[0]
        for field in ("kind", "event", "workspace_id", "identifier"):
            assert field in p, f"Expected field {field!r} in payload {p!r}"
