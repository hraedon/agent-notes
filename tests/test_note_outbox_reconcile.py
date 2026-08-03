"""Tests for the note outbox + reconcile path (Plan 018 review fix #1).

Notes mirror the breadcrumb outbox contract: ``append_note`` captures to the
outbox when regista is unreachable, and ``reconcile`` replays the captured op
into regista. Covers face-level capture/replay (no Postgres) and the DB-backed
end-to-end path (``pending_sync`` set on the local ``memories`` row on capture,
cleared after a successful reconcile).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from psycopg.rows import dict_row
from regista.testing import InMemoryRegista

from agent_notes.core import db as coredb
from agent_notes.core.actor import Actor
from agent_notes.core.envelope import LocalKeySigner, verify_envelope
from agent_notes.core.face_factory import reset_face, set_face_for_test
from agent_notes.core.memory_model import add_memory
from agent_notes.core.outbox import OutboxAwareFace, count_ops, read_all
from agent_notes.core.reconcile import reconcile
from agent_notes.core.regista_face import RegistaFace
from tests.conftest import ephemeral_db  # noqa: F401

_PROJECT = "note_ac"


@pytest.fixture
def outbox_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "outbox"
    monkeypatch.setenv("AGENT_NOTES_OUTBOX_DIR", str(d))
    monkeypatch.setenv("AGENT_NOTES_SESSION", uuid.uuid4().hex)
    return d


@pytest.fixture
def signer(tmp_path: Path) -> LocalKeySigner:
    return LocalKeySigner(str(tmp_path / "signing.key"))


@pytest.fixture
def actor() -> Actor:
    return Actor(actor_id="note-ac-agent", display_name="Note AC")


@pytest.fixture
def hmac_key_path(tmp_path: Path) -> str:
    path = tmp_path / "keys.json"
    path.write_text(
        json.dumps(
            {
                "keys": [
                    {
                        "key_id": "test-key-001",
                        "secret": "dGhpcyBpcyBhIHRlc3Qgc2VjcmV0IGtleSBmb3Igc3Vic3RyYXRl",
                        "status": "active",
                    }
                ]
            }
        )
    )
    return str(path)


class TestNoteOutboxCapture:
    """Face-level (no Postgres): an unreachable append_note captures to the
    outbox and never surfaces failure to the caller."""

    def test_offline_append_enqueues(self, outbox_env, signer, actor, hmac_key_path) -> None:
        face = RegistaFace(InMemoryRegista(hmac_key_path=hmac_key_path))
        outface = OutboxAwareFace(
            face, project=_PROJECT, signer=signer, unreachable_probe=lambda: True
        )
        eid = uuid.uuid4()

        result = outface.append_note(
            actor, eid, transition="note_filed", payload={"name": "x", "body": "b"}
        )

        assert result is None
        assert outface.last_op_outboxed is True
        assert outface.pending_count() == 1

        entries = read_all(_PROJECT)
        assert len(entries) == 1
        payload = verify_envelope(entries[0].envelope, signer.public_key())
        assert payload["op"] == "note_append"
        assert payload["work_item_id"] == str(eid)
        assert payload["args"]["transition"] == "note_filed"
        assert payload["args"]["payload"] == {"name": "x", "body": "b"}

    def test_live_append_sets_outboxed_false(
        self, outbox_env, signer, actor, hmac_key_path
    ) -> None:
        face = RegistaFace(InMemoryRegista(hmac_key_path=hmac_key_path))
        outface = OutboxAwareFace(
            face, project=_PROJECT, signer=signer, unreachable_probe=lambda: False
        )
        outface.append_note(actor, uuid.uuid4(), transition="note_filed", payload={})

        assert outface.last_op_outboxed is False
        assert outface.pending_count() == 0


class TestNoteReconcileReplay:
    """An offline append captured to the outbox is replayed into regista on
    reconcile against a reachable face."""

    def test_replay_creates_note_event(self, outbox_env, signer, actor, hmac_key_path) -> None:
        face = RegistaFace(InMemoryRegista(hmac_key_path=hmac_key_path))
        outface = OutboxAwareFace(
            face, project=_PROJECT, signer=signer, unreachable_probe=lambda: True
        )
        eid = uuid.uuid4()
        outface.append_note(
            actor, eid, transition="note_filed", payload={"name": "replay-me", "body": "b"}
        )

        assert face.read_note_events(eid) == []

        report = reconcile(_PROJECT, face=face, signer=signer)

        assert report.replayed == 1
        assert report.rejected == 0
        assert report.conflicts == 0
        assert count_ops(_PROJECT) == 0

        events = face.read_note_events(eid)
        assert len(events) == 1
        assert events[0].transition == "note_filed"
        assert events[0].payload["name"] == "replay-me"

    def test_replay_then_update_in_order(self, outbox_env, signer, actor, hmac_key_path) -> None:
        face = RegistaFace(InMemoryRegista(hmac_key_path=hmac_key_path))
        outface = OutboxAwareFace(
            face, project=_PROJECT, signer=signer, unreachable_probe=lambda: True
        )
        eid = uuid.uuid4()
        outface.append_note(actor, eid, transition="note_filed", payload={"body": "v1"})
        outface.append_note(actor, eid, transition="note_updated", payload={"body": "v2"})

        assert count_ops(_PROJECT) == 2

        report = reconcile(_PROJECT, face=face, signer=signer)
        assert report.replayed == 2

        transitions = [e.transition for e in face.read_note_events(eid)]
        assert transitions == ["note_filed", "note_updated"]


@pytest.mark.usefixtures("ephemeral_db")
class TestNoteOutboxEndToEnd:
    """DB-backed: note_model.add_memory while offline -> outbox op + the local
    memories row flagged pending_sync; reconcile -> event lands in regista and
    pending_sync clears."""

    @pytest.fixture
    def default_project(self):
        ws = coredb.get_or_create_workspace("default", "Default Workspace")
        return coredb.get_or_create_project(
            ws.id, slug="sf2", name="sf2", repo_root="/projects/sf2"
        )

    def test_offline_add_then_reconcile(
        self, default_project, hmac_key_path, signer, outbox_env, monkeypatch
    ) -> None:
        monkeypatch.setenv("AGENT_NOTES_REGISTA_WRITES", "1")
        monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "test-agent")

        reg = InMemoryRegista(hmac_key_path=hmac_key_path)
        face = RegistaFace(reg)
        outface = OutboxAwareFace(
            face, project=_PROJECT, signer=signer, unreachable_probe=lambda: True
        )
        reset_face()
        set_face_for_test(outface)
        try:
            mem = add_memory(
                workspace_id=default_project.workspace_id,
                project_id=default_project.id,
                name="offline-note",
                memory_type="decision",
                body="captured while offline",
                embedding=[0.0] * 768,
            )
            note_id = mem["regista_note_id"]
            assert note_id is not None

            assert count_ops(_PROJECT) == 1
            assert face.read_note_events(note_id) == []

            from agent_notes.core.db import _conn

            with _conn() as conn:
                cur = conn.cursor(row_factory=dict_row)
                cur.execute(
                    "SELECT pending_sync FROM memories WHERE regista_note_id = %s",
                    (note_id,),
                )
                row = cur.fetchone()
            assert row is not None
            assert row["pending_sync"] is True

            report = reconcile(_PROJECT, face=face, signer=signer)
            assert report.replayed == 1
            assert count_ops(_PROJECT) == 0

            events = face.read_note_events(note_id)
            assert len(events) == 1
            assert events[0].transition == "note_filed"

            with _conn() as conn:
                cur = conn.cursor(row_factory=dict_row)
                cur.execute(
                    "SELECT pending_sync FROM memories WHERE regista_note_id = %s",
                    (note_id,),
                )
                row = cur.fetchone()
            assert row["pending_sync"] is False
        finally:
            reset_face()
            reg.close()
