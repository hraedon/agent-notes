"""Unit tests for core/config.py DSN resolution (no database required)."""

from __future__ import annotations

import json

import pytest

from agent_notes.core import config


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_NOTES_DSN", raising=False)
    monkeypatch.delenv("AGENT_NOTES_CONFIG", raising=False)
    for v in (
        "REGISTA_DSN",
        "REGISTA_KEY_PATH",
        "REGISTA_REQUIRE_SSL",
        config._REGISTA_WRITES_ENV,
        "AGENT_NOTES_PROJECT",
    ):
        monkeypatch.delenv(v, raising=False)
    # Isolate from host suite.env files (Plan 017 WI-4.2): point both paths
    # at a non-existent file so tests control their own suite.env content.
    suite_env = tmp_path / "suite.env"
    monkeypatch.setenv("AGENT_SUITE_CONFIG", str(suite_env))
    monkeypatch.setenv("AGENT_SUITE_SYSTEM_CONFIG", str(suite_env))


def _write_config(tmp_path, monkeypatch, dsn):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"dsn": dsn}))
    monkeypatch.setenv("AGENT_NOTES_CONFIG", str(cfg))
    return cfg


def test_explicit_arg_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NOTES_DSN", "postgresql://env/x")
    _write_config(tmp_path, monkeypatch, "postgresql://file/x")
    assert config.resolve_dsn("postgresql://explicit/x") == "postgresql://explicit/x"


def test_env_overrides_file(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NOTES_DSN", "postgresql://env/x")
    _write_config(tmp_path, monkeypatch, "postgresql://file/x")
    assert config.resolve_dsn() == "postgresql://env/x"


def test_file_fallback_when_env_absent(monkeypatch, tmp_path):
    _write_config(tmp_path, monkeypatch, "postgresql://file/x")
    assert config.resolve_dsn() == "postgresql://file/x"


def test_raises_when_nothing_set(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NOTES_CONFIG", str(tmp_path / "missing.json"))
    with pytest.raises(RuntimeError, match="No Postgres DSN found"):
        config.resolve_dsn()


# ---------------------------------------------------------------------------
# Native DSN suite.env layer (WI-051): process env > per-user suite.env >
# system suite.env > config file, per bootstrap-contract §2.
# ---------------------------------------------------------------------------


def _write_native_suite_env(monkeypatch, tmp_path, dsn, level="system"):
    f = tmp_path / f"{level}-suite.env"
    f.write_text(f"AGENT_NOTES_DSN={dsn}\n")
    var = "AGENT_SUITE_CONFIG" if level == "user" else "AGENT_SUITE_SYSTEM_CONFIG"
    monkeypatch.setenv(var, str(f))
    return f


def test_native_dsn_from_system_suite_env(monkeypatch, tmp_path):
    """AGENT_NOTES_DSN in /etc-level suite.env satisfies resolve_dsn (WI-051).

    This is the Lane C qualification scenario: the bootstrap writes the DSN
    into the system suite.env and nothing exports it into the process env.
    """
    _write_native_suite_env(monkeypatch, tmp_path, "postgresql://system-suite/db")
    assert config.resolve_dsn() == "postgresql://system-suite/db"


def test_native_dsn_user_suite_env_beats_system(monkeypatch, tmp_path):
    _write_native_suite_env(monkeypatch, tmp_path, "postgresql://system-suite/db")
    _write_native_suite_env(monkeypatch, tmp_path, "postgresql://user-suite/db", level="user")
    assert config.resolve_dsn() == "postgresql://user-suite/db"


def test_native_dsn_process_env_beats_suite_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NOTES_DSN", "postgresql://env/x")
    _write_native_suite_env(monkeypatch, tmp_path, "postgresql://system-suite/db")
    assert config.resolve_dsn() == "postgresql://env/x"


def test_native_dsn_suite_env_beats_config_file(monkeypatch, tmp_path):
    _write_native_suite_env(monkeypatch, tmp_path, "postgresql://system-suite/db")
    _write_config(tmp_path, monkeypatch, "postgresql://file/x")
    assert config.resolve_dsn() == "postgresql://system-suite/db"


def test_missing_dsn_error_names_the_searched_files(monkeypatch, tmp_path):
    """The failure message names every file that was searched (WI-051)."""
    monkeypatch.setenv("AGENT_NOTES_CONFIG", str(tmp_path / "missing.json"))
    user_env = tmp_path / "user-suite.env"
    system_env = tmp_path / "system-suite.env"
    monkeypatch.setenv("AGENT_SUITE_CONFIG", str(user_env))
    monkeypatch.setenv("AGENT_SUITE_SYSTEM_CONFIG", str(system_env))
    with pytest.raises(RuntimeError) as exc_info:
        config.resolve_dsn()
    message = exc_info.value.args[0]
    assert str(user_env) in message
    assert str(system_env) in message
    assert str(tmp_path / "missing.json") in message


def test_regista_dsn_does_not_fallback_to_native(monkeypatch, tmp_path):
    """F-5: REGISTA_DSN must NOT satisfy the native agent-notes DSN.

    The deployment model uses separate regista and agent-notes databases.
    A missing AGENT_NOTES_DSN must be an actionable failure, not a silent
    connection to the regista database.
    """
    monkeypatch.setenv("REGISTA_DSN", "postgresql://regista/db")
    monkeypatch.setenv("AGENT_NOTES_REGISTA_DSN", "postgresql://legacy-regista/db")
    monkeypatch.setenv("AGENT_NOTES_CONFIG", str(tmp_path / "missing.json"))
    with pytest.raises(RuntimeError, match="No Postgres DSN found") as exc_info:
        config.resolve_dsn()
    assert "REGISTA_DSN" not in exc_info.value.args[0]
    assert "AGENT_NOTES_REGISTA_DSN" not in exc_info.value.args[0]


def test_removed_regista_dsn_alone_does_not_satisfy_native(monkeypatch, tmp_path):
    """The removed regista DSN name cannot satisfy the native DSN."""
    monkeypatch.setenv("AGENT_NOTES_REGISTA_DSN", "postgresql://legacy-regista/db")
    monkeypatch.setenv("AGENT_NOTES_CONFIG", str(tmp_path / "missing.json"))
    with pytest.raises(RuntimeError, match="No Postgres DSN found") as exc_info:
        config.resolve_dsn()
    assert "AGENT_NOTES_REGISTA_DSN" not in exc_info.value.args[0]
    assert "REGISTA_DSN" not in exc_info.value.args[0]


def test_malformed_config_raises(monkeypatch, tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text("{not valid json")
    monkeypatch.setenv("AGENT_NOTES_CONFIG", str(cfg))
    with pytest.raises(RuntimeError, match="could not be read"):
        config.resolve_dsn()


def test_config_path_honors_override(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NOTES_CONFIG", str(tmp_path / "custom.json"))
    assert config.config_path() == tmp_path / "custom.json"


# ---------------------------------------------------------------------------
# Retired regista writes gate (Plan 026 D1)
# ---------------------------------------------------------------------------


def _write_regista_config(tmp_path, monkeypatch, regista, native="postgresql://native/x"):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"dsn": native, "regista": regista}))
    monkeypatch.setenv("AGENT_NOTES_CONFIG", str(cfg))
    return cfg


def test_writes_not_requested_when_nothing_set(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NOTES_CONFIG", str(tmp_path / "missing.json"))
    assert config.regista_writes_requested() is False
    config.reject_regista_writes()  # must not raise


def test_regista_connection_settings_alone_are_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setenv("REGISTA_DSN", "postgresql://env/x")
    monkeypatch.setenv("REGISTA_KEY_PATH", "/env/key")
    _write_regista_config(tmp_path, monkeypatch, {"dsn": "postgresql://file/x"})
    assert config.regista_writes_requested() is False
    config.reject_regista_writes()


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes"])
def test_env_gate_on_is_refused(monkeypatch, tmp_path, value):
    monkeypatch.setenv("AGENT_NOTES_CONFIG", str(tmp_path / "missing.json"))
    monkeypatch.setenv(config._REGISTA_WRITES_ENV, value)
    with pytest.raises(config.RetiredSettingError) as info:
        config.reject_regista_writes()
    message = str(info.value)
    assert info.value.code == "RETIRED_SETTING"
    assert message.startswith("RETIRED_SETTING: ")
    assert "AGENT_NOTES_REGISTA_WRITES" in message
    assert "docs/spikes/025-regista-cutover.md" in message


def test_file_gate_on_is_refused(monkeypatch, tmp_path):
    _write_regista_config(tmp_path, monkeypatch, {"writes_enabled": True})
    with pytest.raises(config.RetiredSettingError, match="writes_enabled"):
        config.reject_regista_writes()


def test_env_gate_off_overrides_file_gate_on(monkeypatch, tmp_path):
    monkeypatch.setenv(config._REGISTA_WRITES_ENV, "0")
    _write_regista_config(tmp_path, monkeypatch, {"writes_enabled": True})
    assert config.regista_writes_requested() is False


def test_empty_env_gate_falls_through_to_file(monkeypatch, tmp_path):
    monkeypatch.setenv(config._REGISTA_WRITES_ENV, "")
    _write_regista_config(tmp_path, monkeypatch, {"writes_enabled": True})
    assert config.regista_writes_requested() is True


def test_file_string_false_is_not_truthy(monkeypatch, tmp_path):
    _write_regista_config(tmp_path, monkeypatch, {"writes_enabled": "false"})
    assert config.regista_writes_requested() is False


@pytest.mark.parametrize("content", ["{not valid json", "[1, 2]", '{"regista": [1]}'])
def test_malformed_config_does_not_crash_the_gate(monkeypatch, tmp_path, content):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(content)
    monkeypatch.setenv("AGENT_NOTES_CONFIG", str(cfg_file))
    assert config.regista_writes_requested() is False
