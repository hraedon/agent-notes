"""Unit tests for core/config.py DSN resolution (no database required)."""

from __future__ import annotations

import json

import pytest

from agent_notes.core import config


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("AGENT_NOTES_DSN", raising=False)
    monkeypatch.delenv("AGENT_NOTES_CONFIG", raising=False)


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


def test_malformed_config_raises(monkeypatch, tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text("{not valid json")
    monkeypatch.setenv("AGENT_NOTES_CONFIG", str(cfg))
    with pytest.raises(RuntimeError, match="could not be read"):
        config.resolve_dsn()


def test_config_path_honors_override(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NOTES_CONFIG", str(tmp_path / "custom.json"))
    assert config.config_path() == tmp_path / "custom.json"
