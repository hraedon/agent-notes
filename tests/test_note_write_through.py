"""Tests for the note entity write-through path (Plan 018 WI-1.2).

Memories and reflections write as signed regista entities (entity_kind="note")
behind the AGENT_NOTES_REGISTA_WRITES flag, with the local memories table
becoming a projection. The pgvector index rebuilds from the entities.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from psycopg.rows import dict_row

from agent_notes.core import db as coredb
from agent_notes.core.face_factory import reset_face, set_face_for_test
from agent_notes.core.memory_model import add_memory, delete_memory, update_memory
from agent_notes.core.regista_face import RegistaFace
from tests.conftest import ephemeral_db, provision_v6_regista  # noqa: F401

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
def v6_key_path(tmp_path: Path):
    # provision_v6_regista writes the real v6 keyset here (conftest also
    # exports this fixture; kept local so the module is self-describing).
    return str(tmp_path / "v6_keys.json")


class TestNoteWriteThrough:
    def test_filing_memory_produces_signed_entity(self, default_project, v6_key_path, monkeypatch):
        """AC: filing a memory produces a signed entity in the store readable
        without agent-notes' code."""
        monkeypatch.setenv("AGENT_NOTES_REGISTA_WRITES", "1")
        monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "agent:test-agent")

        reg = provision_v6_regista(v6_key_path)
        face = RegistaFace(reg)
        reset_face()
        set_face_for_test(face)

        try:
            mem = add_memory(
                workspace_id=default_project.workspace_id,
                project_id=default_project.id,
                name="how-deploy-works",
                memory_type="note",
                body="Deploy runs in three stages: build, push, restart.",
                attributes={"source": "session-2026-07-09"},
                embedding=[0.0] * 768,
            )
            assert mem["name"] == "how-deploy-works"
            assert mem["regista_note_id"] is not None

            note_id = mem["regista_note_id"]

            events = face.read_note_events(note_id)
            assert len(events) >= 1
            filed_evt = events[0]
            assert filed_evt.transition == "note_filed"
            assert filed_evt.entity_kind == "note"
            assert filed_evt.payload["note_subtype"] == "memory"
            assert filed_evt.payload["name"] == "how-deploy-works"
            assert "Deploy runs in three stages" in filed_evt.payload["body"]

            assert filed_evt.signature is not None
            assert len(filed_evt.signature) > 0
            assert filed_evt.payload_canonical_hash is not None
            assert len(filed_evt.payload_canonical_hash) > 0
        finally:
            reset_face()
            reg.close()

    def test_local_projection_mirrors_entity(self, default_project, v6_key_path, monkeypatch):
        """The local memories table is a projection of the signed entity."""
        monkeypatch.setenv("AGENT_NOTES_REGISTA_WRITES", "1")
        monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "agent:test-agent")

        reg = provision_v6_regista(v6_key_path)
        face = RegistaFace(reg)
        reset_face()
        set_face_for_test(face)

        try:
            mem = add_memory(
                workspace_id=default_project.workspace_id,
                project_id=default_project.id,
                name="test-mirror",
                memory_type="note",
                body="Mirror body content",
                embedding=[0.1] * 768,
            )

            from agent_notes.core.db import _conn

            with _conn() as conn:
                cur = conn.cursor(row_factory=dict_row)
                cur.execute(
                    "SELECT * FROM memories WHERE project_id = %s AND name = %s AND active = true",
                    (default_project.id, "test-mirror"),
                )
                local = cur.fetchone()

            assert local is not None
            assert local["body"] == "Mirror body content"
            assert str(local["regista_note_id"]) == str(mem["regista_note_id"])
            assert local["memory_type"] == "note"
        finally:
            reset_face()
            reg.close()

    def test_update_memory_appends_note_updated_event(
        self, default_project, v6_key_path, monkeypatch
    ):
        """Updating a memory appends a note_updated event to the entity log."""
        monkeypatch.setenv("AGENT_NOTES_REGISTA_WRITES", "1")
        monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "agent:test-agent")

        reg = provision_v6_regista(v6_key_path)
        face = RegistaFace(reg)
        reset_face()
        set_face_for_test(face)

        try:
            mem = add_memory(
                workspace_id=default_project.workspace_id,
                project_id=default_project.id,
                name="updateable-mem",
                memory_type="note",
                body="Original body",
                embedding=[0.0] * 768,
            )
            note_id = mem["regista_note_id"]

            update_memory(
                workspace_id=default_project.workspace_id,
                project_id=default_project.id,
                name="updateable-mem",
                body="Updated body",
            )

            events = face.read_note_events(note_id)
            transitions = [e.transition for e in events]
            assert "note_filed" in transitions
            assert "note_updated" in transitions

            updated_evt = next(e for e in events if e.transition == "note_updated")
            assert updated_evt.payload["body"] == "Updated body"
        finally:
            reset_face()
            reg.close()

    def test_delete_memory_appends_note_deleted_event(
        self, default_project, v6_key_path, monkeypatch
    ):
        """Deleting a memory appends a note_deleted event and flips active=false."""
        monkeypatch.setenv("AGENT_NOTES_REGISTA_WRITES", "1")
        monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "agent:test-agent")

        reg = provision_v6_regista(v6_key_path)
        face = RegistaFace(reg)
        reset_face()
        set_face_for_test(face)

        try:
            mem = add_memory(
                workspace_id=default_project.workspace_id,
                project_id=default_project.id,
                name="deletable-mem",
                memory_type="note",
                body="To be deleted",
                embedding=[0.0] * 768,
            )
            note_id = mem["regista_note_id"]

            result = delete_memory(
                workspace_id=default_project.workspace_id,
                project_id=default_project.id,
                name="deletable-mem",
            )
            assert result is not None

            events = face.read_note_events(note_id)
            transitions = [e.transition for e in events]
            assert "note_deleted" in transitions

            from agent_notes.core.db import _conn

            with _conn() as conn:
                cur = conn.cursor(row_factory=dict_row)
                cur.execute(
                    "SELECT active FROM memories WHERE project_id = %s AND name = %s",
                    (default_project.id, "deletable-mem"),
                )
                row = cur.fetchone()
            assert row["active"] is False
        finally:
            reset_face()
            reg.close()

    def test_supersede_appends_note_superseded_event(
        self, default_project, v6_key_path, monkeypatch
    ):
        """Filing a memory with the same name supersedes the old one."""
        monkeypatch.setenv("AGENT_NOTES_REGISTA_WRITES", "1")
        monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "agent:test-agent")

        reg = provision_v6_regista(v6_key_path)
        face = RegistaFace(reg)
        reset_face()
        set_face_for_test(face)

        try:
            first = add_memory(
                workspace_id=default_project.workspace_id,
                project_id=default_project.id,
                name="supersede-test",
                memory_type="note",
                body="Version 1",
                embedding=[0.0] * 768,
            )
            old_note_id = first["regista_note_id"]

            second = add_memory(
                workspace_id=default_project.workspace_id,
                project_id=default_project.id,
                name="supersede-test",
                memory_type="note",
                body="Version 2",
                embedding=[0.0] * 768,
            )
            new_note_id = second["regista_note_id"]

            assert old_note_id != new_note_id

            old_events = face.read_note_events(old_note_id)
            old_transitions = [e.transition for e in old_events]
            assert "note_superseded" in old_transitions

            sup_evt = next(e for e in old_events if e.transition == "note_superseded")
            assert sup_evt.payload["superseded_by"] == str(new_note_id)
        finally:
            reset_face()
            reg.close()

    def test_reflection_writes_as_note_entity(self, default_project, v6_key_path, monkeypatch):
        """A reflection (memory_type='reflection') writes as a note entity."""
        monkeypatch.setenv("AGENT_NOTES_REGISTA_WRITES", "1")
        monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "agent:test-agent")

        reg = provision_v6_regista(v6_key_path)
        face = RegistaFace(reg)
        reset_face()
        set_face_for_test(face)

        try:
            mem = add_memory(
                workspace_id=default_project.workspace_id,
                project_id=default_project.id,
                name="2026-07-09-glm",
                memory_type="reflection",
                body="## Session reflection\n\nWorked on Plan 018.",
                embedding=[0.0] * 768,
            )
            note_id = mem["regista_note_id"]

            events = face.read_note_events(note_id)
            filed_evt = events[0]
            assert filed_evt.entity_kind == "note"
            assert filed_evt.payload["note_subtype"] == "reflection"
        finally:
            reset_face()
            reg.close()

    def test_pgvector_rebuilds_from_entities(self, default_project, v6_key_path, monkeypatch):
        """AC: the pgvector index rebuilds from the entities."""
        monkeypatch.setenv("AGENT_NOTES_REGISTA_WRITES", "1")
        monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "agent:test-agent")

        reg = provision_v6_regista(v6_key_path)
        face = RegistaFace(reg)
        reset_face()
        set_face_for_test(face)

        try:
            add_memory(
                workspace_id=default_project.workspace_id,
                project_id=default_project.id,
                name="rebuild-test",
                memory_type="note",
                body="Content for rebuild",
                embedding=[0.5] * 768,
            )

            from agent_notes.core.db import _conn

            with _conn() as conn:
                conn.execute(
                    "DELETE FROM memories WHERE project_id = %s AND name = %s",
                    (default_project.id, "rebuild-test"),
                )
                conn.commit()

            from agent_notes.core.note_model import rebuild_from_regista

            def fake_embed(text):
                return [0.42] * 768

            report = rebuild_from_regista(face, project_id=default_project.id, embed_fn=fake_embed)
            assert report["created"] >= 1

            with _conn() as conn:
                cur = conn.cursor(row_factory=dict_row)
                cur.execute(
                    "SELECT body, embedding, regista_note_id FROM memories "
                    "WHERE project_id = %s AND name = %s AND active = true",
                    (default_project.id, "rebuild-test"),
                )
                row = cur.fetchone()

            assert row is not None
            assert row["body"] == "Content for rebuild"
            assert row["regista_note_id"] is not None
        finally:
            reset_face()
            reg.close()


class TestLegacyPathUnchanged:
    def test_legacy_memory_path_still_works(self, default_project, monkeypatch):
        """Without the regista face, memories write locally (legacy path)."""
        monkeypatch.delenv("AGENT_NOTES_REGISTA_WRITES", raising=False)
        monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "agent:test-agent")
        reset_face()

        try:
            mem = add_memory(
                workspace_id=default_project.workspace_id,
                project_id=default_project.id,
                name="legacy-mem",
                memory_type="note",
                body="Legacy body",
                embedding=[0.0] * 768,
            )
            assert mem["name"] == "legacy-mem"
            assert mem.get("regista_note_id") is None
        finally:
            reset_face()


class TestMemoryTypeRoundTrip:
    """The fine-grained local memory_type round-trips through the note entity,
    not just the coarse note_subtype (review fix #2)."""

    def test_fine_memory_type_stored_and_restored(self, default_project, v6_key_path, monkeypatch):
        """A fine memory_type ('decision') is stored in the payload alongside the
        coarse subtype and restored verbatim on rebuild (not collapsed)."""
        monkeypatch.setenv("AGENT_NOTES_REGISTA_WRITES", "1")
        monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "agent:test-agent")

        reg = provision_v6_regista(v6_key_path)
        face = RegistaFace(reg)
        reset_face()
        set_face_for_test(face)
        try:
            mem = add_memory(
                workspace_id=default_project.workspace_id,
                project_id=default_project.id,
                name="rt-decision",
                memory_type="decision",
                body="A decision worth remembering",
                embedding=[0.0] * 768,
            )
            note_id = mem["regista_note_id"]

            filed = face.read_note_events(note_id)[0]
            assert filed.payload["note_subtype"] == "memory"
            assert filed.payload["memory_type"] == "decision"

            from agent_notes.core.db import _conn
            from agent_notes.core.note_model import rebuild_from_regista

            with _conn() as conn:
                conn.execute(
                    "DELETE FROM memories WHERE project_id = %s AND name = %s",
                    (default_project.id, "rt-decision"),
                )
                conn.commit()

            report = rebuild_from_regista(
                face, project_id=default_project.id, embed_fn=lambda _: [0.0] * 768
            )
            assert report["created"] >= 1

            with _conn() as conn:
                cur = conn.cursor(row_factory=dict_row)
                cur.execute(
                    "SELECT memory_type FROM memories WHERE regista_note_id = %s AND active = true",
                    (note_id,),
                )
                row = cur.fetchone()
            assert row is not None
            assert row["memory_type"] == "decision"
        finally:
            reset_face()
            reg.close()

    def test_reflection_subtype_round_trips(self, default_project, v6_key_path, monkeypatch):
        """A reflection stays 'reflection' across the round-trip."""
        monkeypatch.setenv("AGENT_NOTES_REGISTA_WRITES", "1")
        monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "agent:test-agent")

        reg = provision_v6_regista(v6_key_path)
        face = RegistaFace(reg)
        reset_face()
        set_face_for_test(face)
        try:
            mem = add_memory(
                workspace_id=default_project.workspace_id,
                project_id=default_project.id,
                name="rt-reflection",
                memory_type="reflection",
                body="## Session reflection\n\nRound-trip test.",
                embedding=[0.0] * 768,
            )
            note_id = mem["regista_note_id"]

            from agent_notes.core.db import _conn
            from agent_notes.core.note_model import rebuild_from_regista

            with _conn() as conn:
                conn.execute(
                    "DELETE FROM memories WHERE project_id = %s AND name = %s",
                    (default_project.id, "rt-reflection"),
                )
                conn.commit()

            rebuild_from_regista(
                face, project_id=default_project.id, embed_fn=lambda _: [0.0] * 768
            )

            with _conn() as conn:
                cur = conn.cursor(row_factory=dict_row)
                cur.execute(
                    "SELECT memory_type FROM memories WHERE regista_note_id = %s AND active = true",
                    (note_id,),
                )
                row = cur.fetchone()
            assert row is not None
            assert row["memory_type"] == "reflection"
        finally:
            reset_face()
            reg.close()


class TestWikilinksOnRegistaPath:
    """The regista write path auto-creates [[wikilink]] relates_to links,
    matching the legacy local-only path (review fix #3)."""

    def test_wikilinks_created_on_regista_path(self, default_project, v6_key_path, monkeypatch):
        monkeypatch.setenv("AGENT_NOTES_REGISTA_WRITES", "1")
        monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "agent:test-agent")

        reg = provision_v6_regista(v6_key_path)
        face = RegistaFace(reg)
        reset_face()
        set_face_for_test(face)
        try:
            add_memory(
                workspace_id=default_project.workspace_id,
                project_id=default_project.id,
                name="links-source",
                memory_type="note",
                body="See [[other-note]] and [[another]] for context.",
                embedding=[0.0] * 768,
            )

            from agent_notes.core.db import _conn

            with _conn() as conn:
                cur = conn.cursor(row_factory=dict_row)
                cur.execute(
                    "SELECT to_identifier, relationship FROM links "
                    "WHERE from_kind = 'memory' AND from_project = %s "
                    "AND from_identifier = %s",
                    (default_project.id, "links-source"),
                )
                rows = cur.fetchall()

            targets = {r["to_identifier"] for r in rows}
            assert {"other-note", "another"} <= targets
            assert all(r["relationship"] == "relates_to" for r in rows)
        finally:
            reset_face()
            reg.close()
