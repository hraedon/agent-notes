"""Failure-injection tests for the note/memory split-brain contract.

The signed regista event commits *before* the local projection write. If the
local write then fails, the note exists in regista but not in the local
``memories`` table. ``NoteProjectionError`` makes this state explicit and
recoverable via ``rebuild_from_regista``.

These tests prove:
1. A local projection failure after a committed regista event raises
   ``NoteProjectionError`` (not a generic exception, not a clean success).
2. The error carries the ``regista_note_id`` so the caller can report the
   partial commit.
3. ``rebuild_from_regista`` recovers the state — the note reappears in the
   local projection with the correct body and metadata.
4. The CLI never reports a clean success (exit 0) when the projection fails.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from psycopg.rows import dict_row
from regista.testing import InMemoryRegista

from agent_notes.core import db as coredb
from agent_notes.core.face_factory import reset_face, set_face_for_test
from agent_notes.core.memory_model import add_memory, delete_memory, update_memory
from agent_notes.core.note_model import NoteProjectionError, rebuild_from_regista
from agent_notes.core.regista_face import RegistaFace
from tests.conftest import ephemeral_db  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")


@pytest.fixture
def default_project():
    ws = coredb.get_or_create_workspace("default", "Default Workspace")
    return coredb.get_or_create_project(
        ws.id,
        slug="sf2",
        name="sf2",
        repo_root="/projects/sf2",
    )


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


def _setup_face(hmac_key_path: str, monkeypatch: pytest.MonkeyPatch) -> RegistaFace:
    monkeypatch.setenv("AGENT_NOTES_REGISTA_WRITES", "1")
    monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "test-agent")
    monkeypatch.setenv("AGENT_NOTES_MODEL_LINEAGE", "glm")
    reg = InMemoryRegista(hmac_key_path=hmac_key_path)
    face = RegistaFace(reg)
    reset_face()
    set_face_for_test(face)
    return face


# ---------------------------------------------------------------------------
# add_memory: projection failure after regista commit
# ---------------------------------------------------------------------------


class TestAddMemorySplitBrain:
    def test_projection_failure_raises_note_projection_error(
        self, default_project, hmac_key_path, monkeypatch
    ):
        """When the local projection write fails after the regista event
        commits, NoteProjectionError is raised (not a generic exception)."""
        face = _setup_face(hmac_key_path, monkeypatch)
        try:
            with patch(
                "agent_notes.core.note_model._mirror_note_to_projection",
                side_effect=RuntimeError("injected DB failure"),
            ):
                with pytest.raises(NoteProjectionError) as exc_info:
                    add_memory(
                        workspace_id=default_project.workspace_id,
                        project_id=default_project.id,
                        name="split-brain-add",
                        memory_type="note",
                        body="This note is in regista but not in the local table.",
                        embedding=[0.0] * 768,
                    )

            err = exc_info.value
            assert err.operation == "add"
            assert isinstance(err.regista_note_id, uuid.UUID)
            assert "injected DB failure" in str(err)
            assert "rebuild-from-regista" in str(err)

            # The regista event IS committed — the note exists in the
            # authoritative store even though the local projection failed.
            events = face.read_note_events(err.regista_note_id)
            assert len(events) == 1
            assert events[0].transition == "note_filed"
            assert events[0].payload["name"] == "split-brain-add"
        finally:
            reset_face()
            face.close()

    def test_recovery_via_rebuild_from_regista(
        self, default_project, hmac_key_path, monkeypatch
    ):
        """After a split-brain add, rebuild_from_regista recovers the local
        projection — the note reappears with the correct body."""
        face = _setup_face(hmac_key_path, monkeypatch)
        try:
            with patch(
                "agent_notes.core.note_model._mirror_note_to_projection",
                side_effect=RuntimeError("injected DB failure"),
            ):
                with pytest.raises(NoteProjectionError) as exc_info:
                    add_memory(
                        workspace_id=default_project.workspace_id,
                        project_id=default_project.id,
                        name="recoverable-note",
                        memory_type="decision",
                        body="A decision that must survive the split-brain.",
                        embedding=[0.0] * 768,
                    )

            note_id = exc_info.value.regista_note_id

            # Verify the note is NOT in the local table yet.
            from agent_notes.core.db import _conn

            with _conn() as conn:
                cur = conn.cursor(row_factory=dict_row)
                cur.execute(
                    "SELECT id FROM memories WHERE regista_note_id = %s",
                    (note_id,),
                )
                assert cur.fetchone() is None

            # Recovery: rebuild the projection from regista.
            report = rebuild_from_regista(
                face,
                project_id=default_project.id,
                embed_fn=lambda _: [0.42] * 768,
            )
            assert report["created"] >= 1
            assert report["failed"] == 0

            # The note is now in the local table with the correct body.
            with _conn() as conn:
                cur = conn.cursor(row_factory=dict_row)
                cur.execute(
                    "SELECT name, body, memory_type, active FROM memories "
                    "WHERE regista_note_id = %s",
                    (note_id,),
                )
                row = cur.fetchone()

            assert row is not None
            assert row["name"] == "recoverable-note"
            assert row["body"] == "A decision that must survive the split-brain."
            assert row["memory_type"] == "decision"
            assert row["active"] is True
        finally:
            reset_face()
            face.close()

    def test_outboxed_op_does_not_raise_note_projection_error(
        self, default_project, hmac_key_path, monkeypatch, tmp_path
    ):
        """When the op is outboxed (regista unreachable), a local projection
        failure is a plain exception — there is no committed regista event to
        report a split-brain for."""
        monkeypatch.setenv("AGENT_NOTES_REGISTA_WRITES", "1")
        monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "test-agent")
        monkeypatch.setenv("AGENT_NOTES_OUTBOX_DIR", str(tmp_path / "outbox"))
        monkeypatch.setenv("AGENT_NOTES_SESSION", uuid.uuid4().hex)

        from agent_notes.core.envelope import LocalKeySigner
        from agent_notes.core.outbox import OutboxAwareFace

        reg = InMemoryRegista(hmac_key_path=hmac_key_path)
        base_face = RegistaFace(reg)
        outface = OutboxAwareFace(
            base_face,
            project="split_brain_test",
            signer=LocalKeySigner(str(tmp_path / "signing.key")),
            unreachable_probe=lambda: True,  # force outbox path
        )
        reset_face()
        set_face_for_test(outface)
        try:
            with patch(
                "agent_notes.core.note_model._mirror_note_to_projection",
                side_effect=RuntimeError("injected DB failure"),
            ):
                # The op went to the outbox (last_op_outboxed=True), so a
                # local failure is a plain RuntimeError, not NoteProjectionError.
                with pytest.raises(RuntimeError, match="injected DB failure"):
                    add_memory(
                        workspace_id=default_project.workspace_id,
                        project_id=default_project.id,
                        name="outboxed-note",
                        memory_type="note",
                        body="outboxed body",
                        embedding=[0.0] * 768,
                    )
        finally:
            reset_face()
            reg.close()


# ---------------------------------------------------------------------------
# update_memory: projection failure after regista commit
# ---------------------------------------------------------------------------


class TestUpdateMemorySplitBrain:
    def test_update_projection_failure_raises_note_projection_error(
        self, default_project, hmac_key_path, monkeypatch
    ):
        """A local projection failure during update (after the regista
        note_updated event commits) raises NoteProjectionError."""
        face = _setup_face(hmac_key_path, monkeypatch)
        try:
            mem = add_memory(
                workspace_id=default_project.workspace_id,
                project_id=default_project.id,
                name="update-split-brain",
                memory_type="note",
                body="Original body",
                embedding=[0.0] * 768,
            )
            note_id = mem["regista_note_id"]

            # Inject a failure in the UPDATE statement execution.
            with patch(
                "agent_notes.core.note_model.write_change",
                side_effect=RuntimeError("injected write_change failure"),
            ):
                with pytest.raises(NoteProjectionError) as exc_info:
                    update_memory(
                        workspace_id=default_project.workspace_id,
                        project_id=default_project.id,
                        name="update-split-brain",
                        body="Updated body",
                    )

            err = exc_info.value
            assert err.operation == "update"
            assert err.regista_note_id == uuid.UUID(str(note_id))

            # The note_updated event IS in regista.
            events = face.read_note_events(note_id)
            transitions = [e.transition for e in events]
            assert "note_updated" in transitions
        finally:
            reset_face()
            face.close()

    def test_update_recovery_via_rebuild(
        self, default_project, hmac_key_path, monkeypatch
    ):
        """After a split-brain update, rebuild_from_regista applies the
        note_updated event and the local projection reflects the new body."""
        face = _setup_face(hmac_key_path, monkeypatch)
        try:
            mem = add_memory(
                workspace_id=default_project.workspace_id,
                project_id=default_project.id,
                name="update-recover",
                memory_type="note",
                body="Version 1",
                embedding=[0.0] * 768,
            )
            note_id = mem["regista_note_id"]

            with patch(
                "agent_notes.core.note_model.write_change",
                side_effect=RuntimeError("injected failure"),
            ):
                with pytest.raises(NoteProjectionError):
                    update_memory(
                        workspace_id=default_project.workspace_id,
                        project_id=default_project.id,
                        name="update-recover",
                        body="Version 2",
                    )

            # Local table still has Version 1 (the update didn't commit).
            from agent_notes.core.db import _conn

            with _conn() as conn:
                cur = conn.cursor(row_factory=dict_row)
                cur.execute(
                    "SELECT body FROM memories WHERE regista_note_id = %s AND active = true",
                    (note_id,),
                )
                row = cur.fetchone()
            assert row["body"] == "Version 1"

            # Recovery: rebuild applies the note_updated event.
            rebuild_from_regista(
                face,
                project_id=default_project.id,
                embed_fn=lambda _: [0.0] * 768,
            )

            with _conn() as conn:
                cur = conn.cursor(row_factory=dict_row)
                cur.execute(
                    "SELECT body FROM memories WHERE regista_note_id = %s AND active = true",
                    (note_id,),
                )
                row = cur.fetchone()
            assert row["body"] == "Version 2"
        finally:
            reset_face()
            face.close()


# ---------------------------------------------------------------------------
# delete_memory: projection failure after regista commit
# ---------------------------------------------------------------------------


class TestDeleteMemorySplitBrain:
    def test_delete_projection_failure_raises_note_projection_error(
        self, default_project, hmac_key_path, monkeypatch
    ):
        """A local projection failure during delete (after the regista
        note_deleted event commits) raises NoteProjectionError."""
        face = _setup_face(hmac_key_path, monkeypatch)
        try:
            mem = add_memory(
                workspace_id=default_project.workspace_id,
                project_id=default_project.id,
                name="delete-split-brain",
                memory_type="note",
                body="To be deleted",
                embedding=[0.0] * 768,
            )
            note_id = mem["regista_note_id"]

            with patch(
                "agent_notes.core.note_model.write_change",
                side_effect=RuntimeError("injected delete failure"),
            ):
                with pytest.raises(NoteProjectionError) as exc_info:
                    delete_memory(
                        workspace_id=default_project.workspace_id,
                        project_id=default_project.id,
                        name="delete-split-brain",
                    )

            err = exc_info.value
            assert err.operation == "delete"
            assert err.regista_note_id == uuid.UUID(str(note_id))

            # The note_deleted event IS in regista.
            events = face.read_note_events(note_id)
            transitions = [e.transition for e in events]
            assert "note_deleted" in transitions
        finally:
            reset_face()
            face.close()

    def test_delete_recovery_via_rebuild(
        self, default_project, hmac_key_path, monkeypatch
    ):
        """After a split-brain delete, rebuild_from_regista applies the
        note_deleted event and the local row is marked inactive."""
        face = _setup_face(hmac_key_path, monkeypatch)
        try:
            mem = add_memory(
                workspace_id=default_project.workspace_id,
                project_id=default_project.id,
                name="delete-recover",
                memory_type="note",
                body="Will be deleted",
                embedding=[0.0] * 768,
            )
            note_id = mem["regista_note_id"]

            with patch(
                "agent_notes.core.note_model.write_change",
                side_effect=RuntimeError("injected failure"),
            ):
                with pytest.raises(NoteProjectionError):
                    delete_memory(
                        workspace_id=default_project.workspace_id,
                        project_id=default_project.id,
                        name="delete-recover",
                    )

            # Local table still has the row active (the delete didn't commit).
            from agent_notes.core.db import _conn

            with _conn() as conn:
                cur = conn.cursor(row_factory=dict_row)
                cur.execute(
                    "SELECT active FROM memories WHERE regista_note_id = %s",
                    (note_id,),
                )
                row = cur.fetchone()
            assert row["active"] is True

            # Recovery: rebuild applies the note_deleted event.
            rebuild_from_regista(
                face,
                project_id=default_project.id,
                embed_fn=lambda _: [0.0] * 768,
            )

            with _conn() as conn:
                cur = conn.cursor(row_factory=dict_row)
                cur.execute(
                    "SELECT active FROM memories WHERE regista_note_id = %s",
                    (note_id,),
                )
                row = cur.fetchone()
            assert row["active"] is False
        finally:
            reset_face()
            face.close()


# ---------------------------------------------------------------------------
# CLI: never reports clean success on projection failure
# ---------------------------------------------------------------------------


class TestCliNeverReportsCleanSuccess:
    def test_mem_add_projection_failure_returns_nonzero(
        self, default_project, hmac_key_path, monkeypatch, capsys
    ):
        """cmd_mem_add returns a nonzero exit code and a PROJECTION_FAILED
        envelope when the local projection write fails after the regista
        event commits."""
        import argparse

        from agent_notes.cli.memory import cmd_mem_add

        _setup_face(hmac_key_path, monkeypatch)
        try:
            with (
                patch(
                    "agent_notes.core.note_model._mirror_note_to_projection",
                    side_effect=RuntimeError("injected DB failure"),
                ),
                patch(
                    "agent_notes.core.embed.embed",
                    return_value=__import__("numpy").zeros(768),
                ),
            ):
                ns = argparse.Namespace(
                    workspace=None,
                    project=None,
                    path="/projects/sf2",
                    json=True,
                    name="cli-split-brain",
                    type="note",
                    body="CLI split-brain body",
                    attributes=None,
                )
                rc = cmd_mem_add(ns)

            assert rc != 0, "CLI must not return exit 0 on projection failure"
            captured = capsys.readouterr()
            # structlog may write key-loading info to stdout in the full suite.
            # The CLI error envelope is a pretty-printed JSON document (indent=2)
            # so it starts with '{\n  "ok":'. Find it by locating the last
            # '"ok":' marker and backing up to the preceding '{'.
            ok_pos = captured.out.rindex('"ok":')
            brace_pos = captured.out.rindex("{", 0, ok_pos)
            out = json.loads(captured.out[brace_pos:])
            assert out["ok"] is False
            assert out["error"]["code"] == "PROJECTION_FAILED"
            assert "rebuild-from-regista" in out["error"]["detail"]
        finally:
            reset_face()


# ---------------------------------------------------------------------------
# CLI end-to-end: split-brain → rebuild-from-regista restores the row
# ---------------------------------------------------------------------------


class TestCliEndToEndRecovery:
    def test_split_brain_then_cli_rebuild_restores_row(
        self, default_project, hmac_key_path, monkeypatch, capsys
    ):
        """After an injected split-brain on add, running the CLI
        ``memory rebuild-from-regista`` command restores the memory row.
        The rebuild command must not exit 0 while only rebuilding work_items
        (it rebuilds memories via note_model.rebuild_from_regista)."""
        import argparse

        import numpy as np

        from agent_notes.cli.memory import cmd_mem_add, cmd_mem_rebuild

        face = _setup_face(hmac_key_path, monkeypatch)
        try:
            # 1. Inject a split-brain: regista commits, local projection fails.
            with (
                patch(
                    "agent_notes.core.note_model._mirror_note_to_projection",
                    side_effect=RuntimeError("injected DB failure"),
                ),
                patch(
                    "agent_notes.core.embed.embed",
                    return_value=np.zeros(768),
                ),
            ):
                ns_add = argparse.Namespace(
                    workspace=None, project=None, path="/projects/sf2",
                    json=True, name="e2e-recovery", type="note",
                    body="This note must survive the split-brain.",
                    attributes=None,
                )
                rc_add = cmd_mem_add(ns_add)
            assert rc_add != 0

            # 2. Verify the row is NOT in the local table.
            from agent_notes.core.db import _conn

            with _conn() as conn:
                cur = conn.cursor(row_factory=dict_row)
                cur.execute(
                    "SELECT id FROM memories WHERE project_id = %s AND name = %s",
                    (default_project.id, "e2e-recovery"),
                )
                assert cur.fetchone() is None

            # 3. Run the CLI rebuild command (with embed mocked to avoid
            #    loading the 270MB model in tests).
            with patch(
                "agent_notes.core.embed.embed",
                return_value=np.zeros(768),
            ):
                ns_rebuild = argparse.Namespace(
                    workspace=None, project=None, path="/projects/sf2",
                    json=True,
                )
                rc_rebuild = cmd_mem_rebuild(ns_rebuild)

            # The rebuild must succeed (exit 0) and report created >= 1.
            assert rc_rebuild == 0, f"rebuild exited {rc_rebuild}"
            captured = capsys.readouterr()
            ok_pos = captured.out.rindex('"created":')
            brace_pos = captured.out.rindex("{", 0, ok_pos)
            report = json.loads(captured.out[brace_pos:])
            assert report["created"] >= 1
            assert report["failed"] == 0

            # 4. The row is now in the local table with the correct body.
            with _conn() as conn:
                cur = conn.cursor(row_factory=dict_row)
                cur.execute(
                    "SELECT name, body, active FROM memories "
                    "WHERE project_id = %s AND name = %s AND active = true",
                    (default_project.id, "e2e-recovery"),
                )
                row = cur.fetchone()
            assert row is not None
            assert row["body"] == "This note must survive the split-brain."
        finally:
            reset_face()
            face.close()


# ---------------------------------------------------------------------------
# Supersede failure-injection: both entity_ids reported, rebuild recovers
# ---------------------------------------------------------------------------


class TestSupersedeSplitBrain:
    def test_supersede_failure_reports_both_entity_ids(
        self, default_project, hmac_key_path, monkeypatch
    ):
        """When a note supersedes an existing one and the local projection
        fails after both regista appends commit, NoteProjectionError carries
        the new entity_id and mentions the superseded entity_id."""
        face = _setup_face(hmac_key_path, monkeypatch)
        try:
            # File the first note successfully.
            first = add_memory(
                workspace_id=default_project.workspace_id,
                project_id=default_project.id,
                name="supersede-target",
                memory_type="note",
                body="Version 1",
                embedding=[0.0] * 768,
            )
            old_note_id = first["regista_note_id"]

            # Inject a failure in the local projection write for the second
            # (superseding) note. Both the note_filed and note_superseded
            # events will have committed to regista before the failure.
            with patch(
                "agent_notes.core.note_model._mirror_note_to_projection",
                side_effect=RuntimeError("injected supersede failure"),
            ):
                with pytest.raises(NoteProjectionError) as exc_info:
                    add_memory(
                        workspace_id=default_project.workspace_id,
                        project_id=default_project.id,
                        name="supersede-target",
                        memory_type="note",
                        body="Version 2",
                        embedding=[0.0] * 768,
                    )

            err = exc_info.value
            assert err.operation == "add"
            # The error message must mention both entity_ids.
            assert str(old_note_id) in str(err), (
                f"superseded entity_id {old_note_id} not in error: {err}"
            )
            assert "superseded_entity_id=" in str(err)

            # Both events are in regista.
            old_events = face.read_note_events(old_note_id)
            assert any(e.transition == "note_superseded" for e in old_events)
        finally:
            reset_face()
            face.close()

    def test_supersede_split_brain_rebuild_recovers(
        self, default_project, hmac_key_path, monkeypatch
    ):
        """After a supersede split-brain, rebuild_from_regista restores the
        correct state: the new note is active, the old note is inactive."""
        face = _setup_face(hmac_key_path, monkeypatch)
        try:
            first = add_memory(
                workspace_id=default_project.workspace_id,
                project_id=default_project.id,
                name="supersede-recover",
                memory_type="note",
                body="Version 1",
                embedding=[0.0] * 768,
            )
            old_note_id = first["regista_note_id"]

            with patch(
                "agent_notes.core.note_model._mirror_note_to_projection",
                side_effect=RuntimeError("injected failure"),
            ):
                with pytest.raises(NoteProjectionError) as exc_info:
                    add_memory(
                        workspace_id=default_project.workspace_id,
                        project_id=default_project.id,
                        name="supersede-recover",
                        memory_type="note",
                        body="Version 2",
                        embedding=[0.0] * 768,
                    )
            new_note_id = exc_info.value.regista_note_id

            # Recovery.
            rebuild_from_regista(
                face, project_id=default_project.id, embed_fn=lambda _: [0.0] * 768
            )

            from agent_notes.core.db import _conn

            with _conn() as conn:
                cur = conn.cursor(row_factory=dict_row)
                # New note is active.
                cur.execute(
                    "SELECT body, active FROM memories WHERE regista_note_id = %s",
                    (new_note_id,),
                )
                new_row = cur.fetchone()
                # Old note is inactive.
                cur.execute(
                    "SELECT active FROM memories WHERE regista_note_id = %s",
                    (old_note_id,),
                )
                old_row = cur.fetchone()

            assert new_row is not None
            assert new_row["body"] == "Version 2"
            assert new_row["active"] is True
            assert old_row is not None
            assert old_row["active"] is False
        finally:
            reset_face()
            face.close()


# ---------------------------------------------------------------------------
# Drift check: read-only detection of hard-crash split-brain
# ---------------------------------------------------------------------------


class TestProjectionDriftCheck:
    def test_no_drift_when_projection_is_consistent(
        self, default_project, hmac_key_path, monkeypatch
    ):
        """When the local projection is consistent with regista, the drift
        check reports zero drifted entities."""
        from agent_notes.core.note_model import check_projection_drift

        face = _setup_face(hmac_key_path, monkeypatch)
        try:
            add_memory(
                workspace_id=default_project.workspace_id,
                project_id=default_project.id,
                name="drift-clean",
                memory_type="note",
                body="Consistent note",
                embedding=[0.0] * 768,
            )
            drift = check_projection_drift(face, project_id=default_project.id)
            assert drift["drifted"] == 0
            assert drift["missing_entity_ids"] == []
            assert drift["stale"] == []
        finally:
            reset_face()
            face.close()

    def test_drift_detected_after_hard_crash_missing_row(
        self, default_project, hmac_key_path, monkeypatch
    ):
        """Simulates a hard crash on add: the regista event commits but the
        local row is deleted (as if the process died before the local commit).
        The drift check detects the missing entity."""
        from agent_notes.core.note_model import check_projection_drift

        face = _setup_face(hmac_key_path, monkeypatch)
        try:
            mem = add_memory(
                workspace_id=default_project.workspace_id,
                project_id=default_project.id,
                name="drift-missing",
                memory_type="note",
                body="Will be lost in a hard crash",
                embedding=[0.0] * 768,
            )
            note_id = mem["regista_note_id"]

            # Simulate hard crash: delete the local row (regista still has it).
            from agent_notes.core.db import _conn

            with _conn() as conn:
                conn.execute(
                    "DELETE FROM memories WHERE regista_note_id = %s",
                    (note_id,),
                )
                conn.commit()

            drift = check_projection_drift(face, project_id=default_project.id)
            assert drift["drifted"] >= 1
            assert str(note_id) in drift["missing_entity_ids"]
        finally:
            reset_face()
            face.close()

    def test_drift_detects_stale_update(
        self, default_project, hmac_key_path, monkeypatch
    ):
        """Simulates a hard crash on update: the regista note_updated event
        commits but the local row still has the old body. The drift check
        reports a stale entity with a body mismatch reason."""
        from agent_notes.core.note_model import check_projection_drift

        face = _setup_face(hmac_key_path, monkeypatch)
        try:
            mem = add_memory(
                workspace_id=default_project.workspace_id,
                project_id=default_project.id,
                name="drift-stale-update",
                memory_type="note",
                body="Original body",
                embedding=[0.0] * 768,
            )
            note_id = mem["regista_note_id"]

            # Commit the update to regista but revert the local row to the
            # old body (simulating a crash between regista commit and local
            # UPDATE).
            update_memory(
                workspace_id=default_project.workspace_id,
                project_id=default_project.id,
                name="drift-stale-update",
                body="Updated body",
            )
            from agent_notes.core.db import _conn

            with _conn() as conn:
                conn.execute(
                    "UPDATE memories SET body = 'Original body' "
                    "WHERE regista_note_id = %s",
                    (note_id,),
                )
                conn.commit()

            drift = check_projection_drift(face, project_id=default_project.id)
            assert drift["drifted"] >= 1
            stale_ids = {s["entity_id"] for s in drift["stale"]}
            assert str(note_id) in stale_ids
            entry = next(s for s in drift["stale"] if s["entity_id"] == str(note_id))
            assert any("body" in r for r in entry["reasons"])
        finally:
            reset_face()
            face.close()

    def test_drift_detects_stale_delete(
        self, default_project, hmac_key_path, monkeypatch
    ):
        """Simulates a hard crash on delete: the regista note_deleted event
        commits but the local row is still active. The drift check reports a
        stale entity with an active mismatch reason."""
        from agent_notes.core.note_model import check_projection_drift

        face = _setup_face(hmac_key_path, monkeypatch)
        try:
            mem = add_memory(
                workspace_id=default_project.workspace_id,
                project_id=default_project.id,
                name="drift-stale-delete",
                memory_type="note",
                body="Will be deleted",
                embedding=[0.0] * 768,
            )
            note_id = mem["regista_note_id"]

            # Commit the delete to regista but revert the local row to active
            # (simulating a crash between regista commit and local UPDATE).
            delete_memory(
                workspace_id=default_project.workspace_id,
                project_id=default_project.id,
                name="drift-stale-delete",
            )
            from agent_notes.core.db import _conn

            with _conn() as conn:
                conn.execute(
                    "UPDATE memories SET active = true WHERE regista_note_id = %s",
                    (note_id,),
                )
                conn.commit()

            drift = check_projection_drift(face, project_id=default_project.id)
            assert drift["drifted"] >= 1
            stale_ids = {s["entity_id"] for s in drift["stale"]}
            assert str(note_id) in stale_ids
            entry = next(s for s in drift["stale"] if s["entity_id"] == str(note_id))
            assert any("active" in r for r in entry["reasons"])
        finally:
            reset_face()
            face.close()

    def test_cli_check_drift_exits_nonzero_on_drift(
        self, default_project, hmac_key_path, monkeypatch, capsys
    ):
        """The CLI 'memory check-drift' command exits nonzero (EXIT_CONFLICT)
        when drift exists, while still emitting structured JSON."""
        import argparse

        from agent_notes.cli.memory import cmd_mem_check_drift

        face = _setup_face(hmac_key_path, monkeypatch)
        try:
            mem = add_memory(
                workspace_id=default_project.workspace_id,
                project_id=default_project.id,
                name="drift-cli-exit",
                memory_type="note",
                body="Will be lost",
                embedding=[0.0] * 768,
            )
            note_id = mem["regista_note_id"]

            # Simulate hard crash: delete the local row.
            from agent_notes.core.db import _conn

            with _conn() as conn:
                conn.execute(
                    "DELETE FROM memories WHERE regista_note_id = %s",
                    (note_id,),
                )
                conn.commit()

            ns = argparse.Namespace(
                workspace=None, project=None, path="/projects/sf2", json=True,
            )
            rc = cmd_mem_check_drift(ns)
            assert rc != 0, "check-drift must exit nonzero when drift exists"

            captured = capsys.readouterr()
            ok_pos = captured.out.rindex('"drifted":')
            brace_pos = captured.out.rindex("{", 0, ok_pos)
            out = json.loads(captured.out[brace_pos:])
            assert out["drifted"] >= 1
        finally:
            reset_face()
            face.close()

    def test_cli_check_drift_exits_zero_when_clean(
        self, default_project, hmac_key_path, monkeypatch, capsys
    ):
        """The CLI 'memory check-drift' command exits 0 when no drift exists."""
        import argparse

        from agent_notes.cli.memory import cmd_mem_check_drift

        face = _setup_face(hmac_key_path, monkeypatch)
        try:
            add_memory(
                workspace_id=default_project.workspace_id,
                project_id=default_project.id,
                name="drift-cli-clean",
                memory_type="note",
                body="Consistent",
                embedding=[0.0] * 768,
            )
            ns = argparse.Namespace(
                workspace=None, project=None, path="/projects/sf2", json=True,
            )
            rc = cmd_mem_check_drift(ns)
            assert rc == 0, "check-drift must exit 0 when no drift"
        finally:
            reset_face()
            face.close()
