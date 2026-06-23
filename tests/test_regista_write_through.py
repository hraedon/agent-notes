"""Tests for the regista write-through path behind AGENT_NOTES_REGISTA_WRITES."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from psycopg.rows import dict_row
from regista.testing import InMemoryRegista

from agent_notes.core import db as coredb
from agent_notes.core.db import _conn
from agent_notes.core.face_factory import reset_face, set_face_for_test
from agent_notes.core.regista_face import RegistaFace
from agent_notes.core.work_item_model import WorkItemModel
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
def hmac_key_path(tmp_path: Path):
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


def _vec768():
    return [0.0] * 768


def _set_regista_env(dsn: str):
    os.environ["AGENT_NOTES_REGISTA_DSN"] = dsn
    os.environ["AGENT_NOTES_REGISTA_WRITES"] = "1"
    os.environ["AGENT_NOTES_REGISTA_PROJECT"] = "test_project"
    os.environ["AGENT_NOTES_REGISTA_HMAC_KEY_PATH"] = "/dev/null"
    os.environ["AGENT_NOTES_ACTOR_ID"] = "test-agent"


def _clear_regista_env():
    for key in (
        "AGENT_NOTES_REGISTA_DSN",
        "AGENT_NOTES_REGISTA_WRITES",
        "AGENT_NOTES_REGISTA_PROJECT",
        "AGENT_NOTES_REGISTA_HMAC_KEY_PATH",
        "AGENT_NOTES_ACTOR_ID",
    ):
        os.environ.pop(key, None)


class TestRegistaWriteThrough:
    def test_file_amend_close_reopen_round_trip(
        self, default_project, hmac_key_path, monkeypatch
    ):
        monkeypatch.setenv("AGENT_NOTES_REGISTA_WRITES", "1")
        monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "test-agent")
        monkeypatch.setenv("AGENT_NOTES_PRINCIPAL_ID", "test@example.com")

        reg = InMemoryRegista(hmac_key_path=hmac_key_path)
        face = RegistaFace(reg)
        reset_face()
        set_face_for_test(face)

        try:
            wi = WorkItemModel.file_work_item(
                project_id=default_project.id,
                identifier="WI-REG-01",
                title="Original title",
                body="Original body",
                kind="bug",
                status="open",
                severity="high",
                embedding=_vec768(),
            )
            assert wi["identifier"] == "WI-REG-01"
            assert wi["status"] == "open"
            assert wi["regista_work_item_id"] is not None

            regista_id = wi["regista_work_item_id"]
            history = face.history(regista_id)
            assert len(history) >= 1
            listed = face.list(current_states=["open"])
            assert any(str(item.work_item_id) == str(regista_id) for item in listed)

            with _conn() as conn:
                cur = conn.cursor(row_factory=dict_row)
                cur.execute(
                    "SELECT * FROM work_items WHERE project_id = %s AND identifier = %s",
                    (default_project.id, "WI-REG-01"),
                )
                local = cur.fetchone()
            assert local is not None
            assert local["status"] == "open"
            assert local["title"] == "Original title"
            assert str(local["regista_work_item_id"]) == str(regista_id)

            updated = WorkItemModel.update_work_item(
                project_id=default_project.id,
                identifier="WI-REG-01",
                title="Amended title",
            )
            assert updated["title"] == "Amended title"
            assert updated["status"] == "open"

            closed = WorkItemModel.close_work_item(default_project.id, "WI-REG-01")
            assert closed["status"] == "closed"

            reopened = WorkItemModel.update_work_item(
                project_id=default_project.id,
                identifier="WI-REG-01",
                status="open",
            )
            assert reopened["status"] == "open"

            listed_closed = face.list(current_states=["closed"])
            assert not any(str(item.work_item_id) == str(regista_id) for item in listed_closed)
        finally:
            reset_face()
            reg.close()

    def test_claim_release_updates_lease_projection(
        self, default_project, hmac_key_path, monkeypatch
    ):
        monkeypatch.setenv("AGENT_NOTES_REGISTA_WRITES", "1")
        monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "test-agent")

        reg = InMemoryRegista(hmac_key_path=hmac_key_path)
        face = RegistaFace(reg)
        reset_face()
        set_face_for_test(face)

        try:
            wi = WorkItemModel.file_work_item(
                project_id=default_project.id,
                identifier="WI-REG-02",
                title="Lease test",
                status="open",
                embedding=_vec768(),
            )
            entity_id = wi["entity_id"]

            claimed = WorkItemModel.claim_work_item(
                project_id=default_project.id,
                identifier="WI-REG-02",
                actor_id="legacy-actor",
                ttl_seconds=300,
            )
            assert claimed["status"] == "claimed"

            with _conn() as conn:
                cur = conn.cursor(row_factory=dict_row)
                cur.execute("SELECT * FROM work_item_leases WHERE entity_id = %s", (entity_id,))
                lease = cur.fetchone()
            assert lease is not None
            assert lease["actor_id"] != "legacy-actor"

            released = WorkItemModel.release_work_item(
                project_id=default_project.id,
                identifier="WI-REG-02",
                actor_id="legacy-actor",
            )
            assert released["status"] == "open"

            with _conn() as conn:
                cur = conn.cursor(row_factory=dict_row)
                cur.execute("SELECT 1 FROM work_item_leases WHERE entity_id = %s", (entity_id,))
                assert cur.fetchone() is None
        finally:
            reset_face()
            reg.close()

    def test_heartbeat_updates_local_lease(self, default_project, hmac_key_path, monkeypatch):
        monkeypatch.setenv("AGENT_NOTES_REGISTA_WRITES", "1")
        monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "test-agent")

        reg = InMemoryRegista(hmac_key_path=hmac_key_path)
        face = RegistaFace(reg)
        reset_face()
        set_face_for_test(face)

        try:
            wi = WorkItemModel.file_work_item(
                project_id=default_project.id,
                identifier="WI-REG-03",
                title="Heartbeat test",
                status="open",
                embedding=_vec768(),
            )
            entity_id = wi["entity_id"]
            WorkItemModel.claim_work_item(default_project.id, "WI-REG-03")

            with _conn() as conn:
                cur = conn.cursor(row_factory=dict_row)
                cur.execute("SELECT * FROM work_item_leases WHERE entity_id = %s", (entity_id,))
                before = cur.fetchone()

            WorkItemModel.heartbeat_work_item(
                project_id=default_project.id,
                identifier="WI-REG-03",
                actor_id="legacy-actor",
                ttl_seconds=600,
            )

            with _conn() as conn:
                cur = conn.cursor(row_factory=dict_row)
                cur.execute("SELECT * FROM work_item_leases WHERE entity_id = %s", (entity_id,))
                after = cur.fetchone()
            assert after["expires_at"] > before["expires_at"]
            assert after["heartbeat_count"] == before["heartbeat_count"] + 1
        finally:
            reset_face()
            reg.close()


class TestMigrateToRegista:
    def test_migration_creates_regista_items_and_records_id(
        self, default_project, hmac_key_path, monkeypatch
    ):
        monkeypatch.delenv("AGENT_NOTES_REGISTA_WRITES", raising=False)
        reset_face()

        legacy = WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-MIG-01",
            title="Migrate me",
            body="Migration body",
            status="closed",
            embedding=_vec768(),
        )
        assert legacy.get("regista_work_item_id") is None

        monkeypatch.setenv("AGENT_NOTES_REGISTA_DSN", "postgresql://unused")
        monkeypatch.setenv("AGENT_NOTES_REGISTA_PROJECT", "test_project")
        monkeypatch.setenv("AGENT_NOTES_REGISTA_HMAC_KEY_PATH", hmac_key_path)
        monkeypatch.setenv("AGENT_NOTES_REGISTA_WRITES", "1")
        monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "test-agent")

        import regista as regista_module

        from agent_notes.scripts import migrate_to_regista

        reg = InMemoryRegista(project="test_project", hmac_key_path=hmac_key_path)

        class _PatchedRegista:
            def __init__(self, dsn, project, key_path, *, require_ssl=False):
                self._reg = reg

            def __getattr__(self, name):
                return getattr(self._reg, name)

        monkeypatch.setattr(regista_module, "Regista", _PatchedRegista)

        try:
            code = migrate_to_regista._run_migration("sf2", apply=True)
            assert code == 0

            with _conn() as conn:
                cur = conn.cursor(row_factory=dict_row)
                cur.execute(
                    "SELECT regista_work_item_id, status FROM work_items WHERE identifier = %s",
                    ("WI-MIG-01",),
                )
                row = cur.fetchone()
            assert row is not None
            assert row["regista_work_item_id"] is not None
            assert row["status"] == "closed"

            face = RegistaFace(reg)
            item = face.get(row["regista_work_item_id"])
            assert item is not None
            assert item.current_state == "closed"
            assert item.custom_fields["title"] == "Migrate me"
        finally:
            reg.close()
            reset_face()
            _clear_regista_env()

    def test_dry_run_does_not_create_regista_items(
        self, default_project, hmac_key_path, monkeypatch
    ):
        monkeypatch.delenv("AGENT_NOTES_REGISTA_WRITES", raising=False)
        reset_face()

        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-DRY-01",
            title="Dry run",
            status="open",
            embedding=_vec768(),
        )

        monkeypatch.setenv("AGENT_NOTES_REGISTA_DSN", "postgresql://unused")
        monkeypatch.setenv("AGENT_NOTES_REGISTA_HMAC_KEY_PATH", hmac_key_path)
        monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "test-agent")

        from agent_notes.scripts import migrate_to_regista

        code = migrate_to_regista._run_migration("sf2", apply=False)
        assert code == 0

        with _conn() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                "SELECT regista_work_item_id FROM work_items WHERE identifier = %s",
                ("WI-DRY-01",),
            )
            assert cur.fetchone()["regista_work_item_id"] is None

        _clear_regista_env()
        reset_face()


class TestLegacyPathUnchanged:
    def test_legacy_path_still_writes_op_log(self, default_project, monkeypatch):
        _clear_regista_env()
        reset_face()
        monkeypatch.delenv("AGENT_NOTES_REGISTA_WRITES", raising=False)
        monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "test-agent")

        wi = WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="WI-LEG-01",
            title="Legacy item",
            embedding=_vec768(),
        )
        entity_id = wi["entity_id"]

        with _conn() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute("SELECT * FROM op_log WHERE entity_id = %s", (entity_id,))
            ops = cur.fetchall()
        assert len(ops) >= 1
        assert any(op["op_type"] == "create" for op in ops)

        _clear_regista_env()
        reset_face()
