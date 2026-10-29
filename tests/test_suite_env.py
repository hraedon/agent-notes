"""Tests for suite.env layering and canonical v6 actor resolution."""

from __future__ import annotations

import json

import pytest

from agent_notes.core import config
from agent_notes.core.actor import ActorConfigurationError, resolve_actor
from agent_notes.core.suite_env import _parse_env_file, load_suite_env


def test_parse_env_file_skips_comments_blanks_and_export(tmp_path):
    path = tmp_path / "suite.env"
    path.write_text(
        "# comment\n\nREGISTA_DSN=postgresql://host/db\n"
        'export REGISTA_KEY_PATH=/keys\nREGISTA_REQUIRE_SSL="true"\n',
        encoding="utf-8",
    )

    assert _parse_env_file(path) == {
        "REGISTA_DSN": "postgresql://host/db",
        "REGISTA_KEY_PATH": "/keys",
        "REGISTA_REQUIRE_SSL": "true",
    }


def test_load_suite_env_user_overrides_system(tmp_path):
    system = tmp_path / "system.env"
    user = tmp_path / "user.env"
    system.write_text("REGISTA_DSN=postgresql://system/db\nREGISTA_KEY_PATH=/sys\n")
    user.write_text("REGISTA_DSN=postgresql://user/db\n")

    assert load_suite_env(user_path=user, system_path=system) == {
        "REGISTA_DSN": "postgresql://user/db",
        "REGISTA_KEY_PATH": "/sys",
    }


def test_parse_env_file_missing_returns_empty(tmp_path):
    assert _parse_env_file(tmp_path / "nope.env") == {}


def test_load_suite_env_system_only(tmp_path):
    system = tmp_path / "system.env"
    system.write_text("REGISTA_DSN=postgresql://system/db\n")

    assert load_suite_env(user_path=tmp_path / "nope", system_path=system) == {
        "REGISTA_DSN": "postgresql://system/db"
    }


def test_load_suite_env_neither_file(tmp_path):
    assert load_suite_env(user_path=tmp_path / "nope1", system_path=tmp_path / "nope2") == {}


def _write_suite_env(monkeypatch, tmp_path, content, level="user"):
    f = tmp_path / f"{level}.env"
    f.write_text(content)
    if level == "user":
        monkeypatch.setenv("AGENT_SUITE_CONFIG", str(f))
    else:
        monkeypatch.setenv("AGENT_SUITE_SYSTEM_CONFIG", str(f))
    return f


def _write_config(tmp_path, monkeypatch, dsn):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"dsn": dsn}))
    monkeypatch.setenv("AGENT_NOTES_CONFIG", str(cfg))
    return cfg


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    names = (
        "AGENT_NOTES_DSN",
        "AGENT_NOTES_CONFIG",
        "AGENT_NOTES_ACTOR_ID",
        "REGISTA_PRINCIPAL_ID",
        "AGENT_NOTES_REGISTA_WRITES",
        "AGENT_NOTES_PROJECT",
        "REGISTA_DSN",
        "REGISTA_KEY_PATH",
        "REGISTA_REQUIRE_SSL",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)
    suite = tmp_path / "suite.env"
    monkeypatch.setenv("AGENT_SUITE_CONFIG", str(suite))
    monkeypatch.setenv("AGENT_SUITE_SYSTEM_CONFIG", str(suite))


def test_process_env_beats_suite_env(monkeypatch, tmp_path):
    suite = tmp_path / "suite.env"
    suite.write_text("REGISTA_PRINCIPAL_ID=human:file\n")
    monkeypatch.setenv("AGENT_SUITE_CONFIG", str(suite))
    monkeypatch.setenv("REGISTA_PRINCIPAL_ID", "human:env")

    assert resolve_actor().actor_id == "human:env"


def test_tool_actor_beats_suite_principal(monkeypatch, tmp_path):
    suite = tmp_path / "suite.env"
    suite.write_text("REGISTA_PRINCIPAL_ID=human:operator\n")
    monkeypatch.setenv("AGENT_SUITE_CONFIG", str(suite))
    monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "agent:worker")

    assert resolve_actor().actor_id == "agent:worker"


def test_per_user_overlay_beats_system_for_actor(monkeypatch, tmp_path):
    system = tmp_path / "system.env"
    user = tmp_path / "user.env"
    system.write_text("REGISTA_PRINCIPAL_ID=human:system\n")
    user.write_text("REGISTA_PRINCIPAL_ID=human:user\n")
    monkeypatch.setenv("AGENT_SUITE_SYSTEM_CONFIG", str(system))
    monkeypatch.setenv("AGENT_SUITE_CONFIG", str(user))

    assert resolve_actor().actor_id == "human:user"


def test_actor_resolution_fails_without_a_configured_identity():
    with pytest.raises(ActorConfigurationError):
        resolve_actor()


def test_config_file_remains_a_native_dsn_fallback(monkeypatch, tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"dsn": "postgresql://file/db"}))
    monkeypatch.setenv("AGENT_NOTES_CONFIG", str(path))

    assert config.resolve_dsn() == "postgresql://file/db"


def test_suite_env_does_not_switch_on_the_retired_regista_writes(monkeypatch, tmp_path):
    """The retired writes gate is read from the process env and config file
    only, as before; a suite.env entry never turned it on."""
    _write_suite_env(monkeypatch, tmp_path, "AGENT_NOTES_REGISTA_WRITES=1\n", level="system")

    assert config.regista_writes_requested() is False
