"""Bridge integration tests (Plan 004 Phase 9c).

Spins up a fake HTTP receiver, runs the bridge in a thread against the
ephemeral Postgres, files a breadcrumb via the model layer, and asserts a
signed POST arrives with the expected wake-event shape.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agent_notes import bridge
from agent_notes.core import db as coredb
from agent_notes.core.breadcrumbs_model import BreadcrumbModel

from tests.conftest import ephemeral_db  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")

SECRET = "test-bridge-secret"


class _Receiver(BaseHTTPRequestHandler):
    received: list[dict] = []

    def log_message(self, *_args, **_kwargs):
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        sig = self.headers.get("X-AgentWake-Signature", "")
        source = self.headers.get("X-AgentWake-Source", "")
        expected = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        valid = hmac.compare_digest(sig, expected)
        try:
            payload = json.loads(body)
        except Exception:
            payload = None
        type(self).received.append(
            {"body": payload, "source": source, "signature_valid": valid}
        )
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"queued"}')


def _start_receiver() -> tuple[HTTPServer, str]:
    _Receiver.received = []
    server = HTTPServer(("127.0.0.1", 0), _Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    return server, url


def test_sign_format():
    sig = bridge._sign(b"abc", "secret")
    assert sig.startswith("sha256=")
    expected = hmac.new(b"secret", b"abc", hashlib.sha256).hexdigest()
    assert sig == f"sha256={expected}"


def test_payload_to_wake_event_shape():
    ev = bridge._payload_to_wake_event(
        {
            "kind": "breadcrumb",
            "event": "filed",
            "identifier": "BC-1",
            "project_id": 7,
            "workspace_id": 1,
        },
        source="agent-notes",
    )
    assert ev["v"] == 0
    assert ev["source"] == "agent-notes"
    assert ev["kind"] == "note-change"
    assert "BC-1" in ev["content"]
    assert ev["meta"]["agent_notes_kind"] == "breadcrumb"
    assert ev["meta"]["agent_notes_event"] == "filed"
    assert ev["meta"]["agent_notes_project_id"] == "7"
    assert "event_id" in ev


def test_post_with_retry_succeeds():
    server, url = _start_receiver()
    try:
        ok = bridge._post_with_retry(
            url,
            SECRET,
            "agent-notes",
            {"v": 0, "event_id": "x", "source": "agent-notes", "kind": "note-change",
             "content": "x", "meta": {}, "wake": False},
            delays=(),
        )
        assert ok is True
        assert len(_Receiver.received) == 1
        assert _Receiver.received[0]["signature_valid"] is True
        assert _Receiver.received[0]["source"] == "agent-notes"
    finally:
        server.shutdown()


def test_post_with_retry_eventually_drops():
    # Point at a closed port; expect False after retries (use tiny delays).
    ok = bridge._post_with_retry(
        "http://127.0.0.1:1/",
        SECRET,
        "agent-notes",
        {"v": 0, "event_id": "x", "source": "agent-notes", "kind": "note-change",
         "content": "x", "meta": {}, "wake": False},
        delays=(0.0, 0.0),
        timeout=0.5,
    )
    assert ok is False


def test_bridge_end_to_end_delivers_post():
    """File a breadcrumb → bridge LISTEN sees NOTIFY → POST hits fake server."""
    server, url = _start_receiver()

    ws = coredb.get_or_create_workspace("default", "Default Workspace")
    proj = coredb.get_or_create_project(
        ws.id, slug="bridge-test", name="bridge-test", repo_root="/tmp/bridge-test"
    )

    errors: list[BaseException] = []

    def _run_bridge():
        try:
            bridge.run(
                target=url,
                secret=SECRET,
                source="agent-notes",
                batch_ms=50,
                batch_n=1,
                max_events=1,
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    bridge_thread = threading.Thread(target=_run_bridge, daemon=True)
    bridge_thread.start()

    # Give LISTEN time to subscribe before we fire the NOTIFY.
    time.sleep(0.5)

    BreadcrumbModel.file_breadcrumb(
        project_id=proj.id,
        identifier="BC-BRIDGE-1",
        title="Bridge test",
        body="trigger NOTIFY",
        kind="observation",
        status="open",
        embedding=[0.0] * 768,
    )

    bridge_thread.join(timeout=10.0)
    server.shutdown()

    assert not errors, f"bridge errored: {errors[0]!r}"
    assert not bridge_thread.is_alive(), "bridge did not exit after max_events"
    assert _Receiver.received, "no POST received from bridge"
    rx = _Receiver.received[0]
    assert rx["signature_valid"] is True
    assert rx["body"]["v"] == 0
    assert rx["body"]["kind"] == "note-change"
    # The change_log NOTIFY payload carries the kind/identifier of the row.
    meta = rx["body"]["meta"]
    assert meta.get("agent_notes_identifier") == "BC-BRIDGE-1" or "BC-BRIDGE-1" in rx["body"]["content"]


def test_doctor_bridge_target_unset_passes():
    from agent_notes.scripts.doctor import _check_bridge_target

    old = os.environ.pop("AGENT_NOTES_BRIDGE_TARGET", None)
    try:
        ok, msg = _check_bridge_target()
        assert ok is True
        assert "not set" in msg.lower() or "disabled" in msg.lower()
    finally:
        if old is not None:
            os.environ["AGENT_NOTES_BRIDGE_TARGET"] = old


def test_doctor_bridge_target_reachable():
    from agent_notes.scripts.doctor import _check_bridge_target

    server, url = _start_receiver()
    old = os.environ.get("AGENT_NOTES_BRIDGE_TARGET")
    os.environ["AGENT_NOTES_BRIDGE_TARGET"] = url
    try:
        ok, msg = _check_bridge_target()
        assert ok is True, msg
    finally:
        server.shutdown()
        if old is None:
            del os.environ["AGENT_NOTES_BRIDGE_TARGET"]
        else:
            os.environ["AGENT_NOTES_BRIDGE_TARGET"] = old


def test_doctor_bridge_target_unreachable():
    from agent_notes.scripts.doctor import _check_bridge_target

    old = os.environ.get("AGENT_NOTES_BRIDGE_TARGET")
    os.environ["AGENT_NOTES_BRIDGE_TARGET"] = "http://127.0.0.1:1/"
    try:
        ok, msg = _check_bridge_target()
        assert ok is False
        assert "unreachable" in msg.lower() or "refused" in msg.lower() or "timeout" in msg.lower()
    finally:
        if old is None:
            del os.environ["AGENT_NOTES_BRIDGE_TARGET"]
        else:
            os.environ["AGENT_NOTES_BRIDGE_TARGET"] = old
