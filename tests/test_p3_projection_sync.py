"""Tests for Plan 009 P3: projection + enforcement-hooks Python side."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import pytest

from agent_notes.cli.breadcrumbs import cmd_bc_export_index
from agent_notes.cli.orient import cmd_orient
from agent_notes.core import db as coredb
from agent_notes.core import outbox, projection
from agent_notes.core.actor import Actor
from agent_notes.core.db import _conn
from agent_notes.core.envelope import LocalKeySigner
from agent_notes.core.face_factory import reset_face, set_face_for_test
from agent_notes.core.regista_face import RegistaFace
from agent_notes.scripts.doctor import _check_regista_face
from tests.conftest import ephemeral_db, provision_v6_regista  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")

_REGPROJECT = "p3_test_project"


@pytest.fixture
def default_project():
    ws = coredb.get_or_create_workspace("p3ws", "P3 Workspace")
    return coredb.get_or_create_project(
        ws.id,
        slug="p3proj",
        name="p3proj",
        repo_root="/projects/p3proj",
    )


@pytest.fixture
def regista_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("REGISTA_DSN", "postgresql://unused")
    monkeypatch.setenv("AGENT_NOTES_PROJECT", _REGPROJECT)
    monkeypatch.setenv("AGENT_NOTES_REGISTA_WRITES", "1")
    monkeypatch.setenv("REGISTA_KEY_PATH", os.devnull)
    monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "agent:p3-test-agent")
    outbox_dir = tmp_path / "outbox"
    monkeypatch.setenv("AGENT_NOTES_OUTBOX_DIR", str(outbox_dir))
    monkeypatch.setenv("AGENT_NOTES_SESSION", "p3-session")
    return outbox_dir


@pytest.fixture
def signer(tmp_path: Path) -> LocalKeySigner:
    return LocalKeySigner(str(tmp_path / "sign.key"))


@pytest.fixture
def actor() -> Actor:
    return Actor(actor_id="agent:p3-test-agent", actor_kind="agent", display_name="P3 Test")


@pytest.fixture
def face(tmp_path: Path) -> RegistaFace:
    return RegistaFace(provision_v6_regista(tmp_path / "v6_keys.json", project=_REGPROJECT))


def _make_args(**kwargs: Any) -> argparse.Namespace:
    defaults = {
        "json": False,
        "workspace": None,
        "project": None,
        "path": "/projects/p3proj",
        "days": 7,
        "limit": 15,
        "reconcile": False,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


class TestOrientRegistaSync:
    def test_orient_surface_regista_sync_counts(
        self,
        default_project,
        regista_env: Path,
        signer: LocalKeySigner,
        actor: Actor,
        face: RegistaFace,
        capsys,
    ):
        reset_face()
        set_face_for_test(face)
        try:
            wid, _ = face.create_breadcrumb(
                actor,
                title="Orient pending item",
                source_identifier="WI-ORIENT-01",
            )

            with _conn() as conn:
                projection.mirror_from_regista(
                    conn,
                    project_id=default_project.id,
                    identifier="WI-ORIENT-01",
                    entity_id="ent-orient-01",
                    regista_work_item_id=wid,
                    state="open",
                    custom_fields={"title": "Orient pending item", "description": "b"},
                    pending_sync=True,
                    actor_id="a",
                )
                conn.commit()

            outbox.enqueue(
                _REGPROJECT,
                {"op": "create", "work_item_id": None, "args": {}},
                signer,
            )

            code = cmd_orient(_make_args(json=True))
            captured = capsys.readouterr()
            assert code == 0

            payload = json.loads(captured.out)
            sync = payload["regista_sync"]
            assert sync["enabled"] is True
            assert sync["project"] == _REGPROJECT
            assert sync["outbox_pending"] == 1
            assert sync["outbox_conflicts"] == 0
            assert sync["outbox_rejected"] == 0
            assert sync["pending_sync_rows"] == 1
        finally:
            reset_face()

    def test_orient_no_stale_line_when_zero(
        self,
        default_project,
        regista_env: Path,
        face: RegistaFace,
        capsys,
    ):
        reset_face()
        set_face_for_test(face)
        try:
            with _conn() as conn:
                cur = conn.cursor()
                cur.execute(
                    "UPDATE work_items SET pending_sync = FALSE WHERE project_id = %s",
                    (default_project.id,),
                )
                conn.commit()

            code = cmd_orient(_make_args(json=False))
            captured = capsys.readouterr()
            assert code == 0
            assert "STALE" not in captured.out
            assert "pending sync" not in captured.out
        finally:
            reset_face()


class TestDoctorRegistaFace:
    def test_doctor_reports_enabled_counts(
        self,
        monkeypatch,
        tmp_path,
        regista_env: Path,
    ):
        ok, msg = _check_regista_face()
        assert ok is True
        assert _REGPROJECT in msg

    def test_doctor_disabled_is_safe(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.delenv("REGISTA_DSN", raising=False)
        monkeypatch.delenv("AGENT_NOTES_REGISTA_WRITES", raising=False)
        ok, msg = _check_regista_face()
        assert ok is True
        assert "disabled" in msg


class TestRebuildFromRegista:
    def test_rebuild_creates_and_updates_local_row(
        self,
        default_project,
        face: RegistaFace,
        actor: Actor,
    ):
        wid, _ = face.create_breadcrumb(
            actor,
            title="Rebuild test",
            source_identifier="WI-REB-01",
        )

        with _conn() as conn:
            report = projection.rebuild_from_regista(conn, face, project_id=default_project.id)
        assert report.mirrored == 0
        assert report.created == 1
        assert report.skipped == 0
        assert report.failed == 0

        with _conn() as conn:
            local = projection.find_local_for_regista(conn, wid)
        assert local is not None
        assert local["identifier"] == "WI-REB-01"
        assert local["status"] == "open"

        face.transition_breadcrumb(actor, wid, "start")
        face.transition_breadcrumb(actor, wid, "submit_for_review")

        with _conn() as conn:
            report2 = projection.rebuild_from_regista(conn, face, project_id=default_project.id)
        assert report2.mirrored == 1
        assert report2.created == 0

        with _conn() as conn:
            local2 = projection.find_local_for_regista(conn, wid)
        assert local2["status"] == "in_review"


class TestExportIndexBanner:
    def test_export_index_with_non_empty_outbox_has_banner(
        self,
        default_project,
        regista_env: Path,
        signer: LocalKeySigner,
        tmp_path: Path,
    ):
        outbox.enqueue(
            _REGPROJECT,
            {"op": "create", "work_item_id": None, "args": {}},
            signer,
        )
        out_path = tmp_path / "OPEN_WORK_ITEMS.txt"
        code = cmd_bc_export_index(_make_args(json=False, output=str(out_path)))
        assert code == 0
        content = out_path.read_text(encoding="utf-8")
        assert content.startswith(
            "> ⚠ STALE — 1 ops pending sync; run `agent-notes outbox reconcile`"
        )

    def test_export_index_without_outbox_has_no_banner(
        self,
        default_project,
        regista_env: Path,
        tmp_path: Path,
    ):
        out_path = tmp_path / "OPEN_WORK_ITEMS.txt"
        code = cmd_bc_export_index(_make_args(json=False, output=str(out_path)))
        assert code == 0
        content = out_path.read_text(encoding="utf-8")
        assert not content.startswith("> ⚠ STALE")
        assert "# Open Work Items" in content


class TestProjectionCLI:
    def test_cli_rebuild_from_regista(
        self,
        default_project,
        regista_env: Path,
        face: RegistaFace,
        actor: Actor,
        capsys,
    ):
        reset_face()
        set_face_for_test(face)
        try:
            face.create_breadcrumb(
                actor,
                title="CLI rebuild test",
                source_identifier="WI-CLI-01",
            )

            parser = argparse.ArgumentParser()
            sub = parser.add_subparsers()
            from agent_notes.cli.projection import register_projection_parsers

            register_projection_parsers(sub)
            args = parser.parse_args(
                ["projection", "rebuild-from-regista", "--path", "/projects/p3proj"]
            )
            code = args.func(args)
            captured = capsys.readouterr()
            assert code == 0
            assert "created" in captured.out
        finally:
            reset_face()
