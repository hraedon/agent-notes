"""Plan 025 Phase 1 exit: native Postgres is the work-item authority.

A host that still carries regista connection settings (DSN, key path, a
``regista`` block in config.json) but has no explicit write gate must never
construct a regista face, and ``file → claim → close`` must reach ``done``.
"""

from __future__ import annotations

import json

import pytest

from agent_notes import codex_lifecycle
from agent_notes.core import db as coredb
from agent_notes.core import face_factory
from agent_notes.core.config import regista_config
from agent_notes.core.work_item_model import WorkItemModel
from tests.conftest import ephemeral_db  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")


@pytest.fixture
def regista_configured_but_gated(monkeypatch, tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps({"regista": {"dsn": "postgresql://regista/x", "key_path": "/k.json"}})
    )
    monkeypatch.setenv("AGENT_NOTES_CONFIG", str(cfg))
    monkeypatch.setenv("REGISTA_DSN", "postgresql://regista/x")
    monkeypatch.setenv("REGISTA_KEY_PATH", "/k.json")
    monkeypatch.delenv("AGENT_NOTES_REGISTA_WRITES", raising=False)
    face_factory.reset_face()

    def never(*_args, **_kwargs):
        raise AssertionError("normal operation must not construct a regista face")

    monkeypatch.setattr(face_factory, "_build_face", never)
    yield
    face_factory.reset_face()


def test_writes_gate_defaults_off_with_regista_configured(regista_configured_but_gated):
    assert regista_config().enabled is False
    assert face_factory.get_face() is None


def test_file_claim_close_reaches_done_without_regista(regista_configured_but_gated, tmp_path):
    ws = coredb.get_or_create_workspace("default", "Default Workspace")
    proj = coredb.get_or_create_project(
        ws.id, slug="p25-native", name="p25-native", repo_root="/projects/p25-native"
    )
    WorkItemModel.file_work_item(
        project_id=proj.id, identifier="NA-001", title="native", embedding=[0.0] * 768
    )
    WorkItemModel.claim_work_item(proj.id, "NA-001")
    assert WorkItemModel.close_work_item(proj.id, "NA-001")["status"] == "done"

    # The Codex Stop hook is a no-op and never reaches for regista.
    payload = {"hook_event_name": "Stop", "cwd": str(tmp_path)}
    assert codex_lifecycle.run_stop(payload) == {"continue": True}
