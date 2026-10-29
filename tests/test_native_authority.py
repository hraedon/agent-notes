"""Native Postgres is the only work-item authority (Plan 025 D7, Plan 026 D1).

Regista write-through is deleted. A host that still carries regista
connection settings (DSN, key path, a ``regista`` block in config.json) but no
write gate works natively, and ``file → claim → close`` reaches ``done``.
A host that still asks for regista writes (``AGENT_NOTES_REGISTA_WRITES`` or
``regista.writes_enabled``) is refused with a named error on every write
rather than silently written natively.
"""

from __future__ import annotations

import json

import pytest

from agent_notes import codex_lifecycle
from agent_notes.core import db as coredb
from agent_notes.core.actor import default_actor, write_actor
from agent_notes.core.config import RetiredSettingError, regista_writes_requested
from agent_notes.core.memory_model import add_memory
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


@pytest.fixture
def project():
    ws = coredb.get_or_create_workspace("default", "Default Workspace")
    return coredb.get_or_create_project(
        ws.id, slug="p25-native", name="p25-native", repo_root="/projects/p25-native"
    )


def test_writes_gate_defaults_off_with_regista_configured(regista_configured_but_gated):
    assert regista_writes_requested() is False
    assert write_actor().actor_id


def test_file_claim_close_reaches_done_without_regista(
    regista_configured_but_gated, project, tmp_path
):
    WorkItemModel.file_work_item(
        project_id=project.id, identifier="NA-001", title="native", embedding=[0.0] * 768
    )
    WorkItemModel.claim_work_item(project.id, "NA-001")
    assert WorkItemModel.close_work_item(project.id, "NA-001")["status"] == "done"

    # The Codex Stop hook is a no-op and never reaches for regista.
    payload = {"hook_event_name": "Stop", "cwd": str(tmp_path)}
    assert codex_lifecycle.run_stop(payload) == {"continue": True}


@pytest.mark.parametrize("value", ["1", "true", "yes"])
def test_regista_writes_env_is_a_named_error(monkeypatch, project, value):
    monkeypatch.setenv("AGENT_NOTES_REGISTA_WRITES", value)
    with pytest.raises(RetiredSettingError, match="025-regista-cutover.md") as info:
        WorkItemModel.file_work_item(
            project_id=project.id, identifier="NA-002", title="refused", embedding=[0.0] * 768
        )
    assert info.value.code == "RETIRED_SETTING"
    assert WorkItemModel.get_work_item(project.id, "NA-002") is None
    with pytest.raises(RetiredSettingError):
        default_actor()


def test_regista_writes_enabled_in_config_is_a_named_error(monkeypatch, tmp_path, project):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"regista": {"writes_enabled": True}}))
    monkeypatch.setenv("AGENT_NOTES_CONFIG", str(cfg))
    monkeypatch.delenv("AGENT_NOTES_REGISTA_WRITES", raising=False)
    with pytest.raises(RetiredSettingError, match="writes_enabled"):
        add_memory(
            workspace_id=project.workspace_id,
            project_id=project.id,
            name="refused",
            memory_type="note",
            body="never written",
        )


def test_regista_writes_off_values_are_not_an_error(monkeypatch, project):
    monkeypatch.setenv("AGENT_NOTES_REGISTA_WRITES", "0")
    assert regista_writes_requested() is False
    row = WorkItemModel.file_work_item(
        project_id=project.id, identifier="NA-003", title="ok", embedding=[0.0] * 768
    )
    assert row["identifier"] == "NA-003"
