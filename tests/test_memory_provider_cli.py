"""Tests for the memory-provider CLI subcommands (Plan 020 WI-3.1)."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from agent_notes.core import db as coredb
from tests.conftest import ephemeral_db  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")

_CLI = [sys.executable, "-m", "agent_notes.cli"]


def _run(*args: str, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    merged_env = {**os.environ, **(env or {})}
    return subprocess.run(
        _CLI + list(args),
        capture_output=True,
        text=True,
        env=merged_env,
        check=check,
    )


@pytest.fixture
def default_project():
    ws = coredb.get_or_create_workspace("default", "Default Workspace")
    proj = coredb.get_or_create_project(ws.id, slug="sf2", name="sf2", repo_root="/projects/sf2")
    return proj


# ---------------------------------------------------------------------------
# describe
# ---------------------------------------------------------------------------


def test_describe_native_returns_engine_and_health():
    result = _run(
        "memory-provider", "describe", "--json",
        env={"AGENT_NOTES_MEMORY_ENGINE": "native"},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["engine"] == "native"
    assert "health" in data
    assert data["health"]["state"] == "healthy"


def test_describe_native_capabilities():
    result = _run(
        "memory-provider", "describe", "--json",
        env={"AGENT_NOTES_MEMORY_ENGINE": "native"},
        check=False,
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    caps = data["health"]["capabilities"]
    assert "ingest" in caps
    assert "recall" in caps
    assert "forget" in caps
    assert "exact_source" in caps


def test_describe_native_version():
    result = _run(
        "memory-provider", "describe", "--json",
        env={"AGENT_NOTES_MEMORY_ENGINE": "native"},
        check=False,
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data["health"]["version"] is not None


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def test_doctor_native_ok():
    result = _run(
        "memory-provider", "doctor", "--json",
        env={"AGENT_NOTES_MEMORY_ENGINE": "native"},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["ok"] is True
    assert data["engine"] == "native"
    assert data["state"] == "healthy"
    assert data["degraded"] is False


def test_doctor_native_capabilities():
    result = _run(
        "memory-provider", "doctor", "--json",
        env={"AGENT_NOTES_MEMORY_ENGINE": "native"},
        check=False,
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    caps = data["capabilities"]
    assert "ingest" in caps
    assert "recall" in caps


def test_doctor_hindsight_unreachable():
    result = _run(
        "memory-provider", "doctor", "--json",
        env={
            "AGENT_NOTES_MEMORY_ENGINE": "hindsight",
            "HINDSIGHT_URL": "http://127.0.0.1:1",
            "HINDSIGHT_TIMEOUT": "2",
        },
        check=False,
    )
    assert result.returncode != 0, result.stderr
    data = json.loads(result.stdout)
    assert data["ok"] is False
    assert data["engine"] == "hindsight"
    assert data["state"] == "unreachable"


def test_doctor_hindsight_not_configured():
    result = _run(
        "memory-provider", "doctor", "--json",
        env={"AGENT_NOTES_MEMORY_ENGINE": "hindsight"},
        check=False,
    )
    assert result.returncode != 0, result.stderr
    data = json.loads(result.stdout)
    assert data["ok"] is False
    assert data["state"] == "not_configured"


# ---------------------------------------------------------------------------
# configure
# ---------------------------------------------------------------------------


def test_configure_native_via_env():
    result = _run(
        "memory-provider", "configure", "--json",
        env={"AGENT_NOTES_MEMORY_ENGINE": "native"},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["engine"] == "native"
    assert data["configured_via"] == "env"
    assert "hindsight" not in data


def test_configure_default():
    env = dict(os.environ)
    env.pop("AGENT_NOTES_MEMORY_ENGINE", None)
    result = _run(
        "memory-provider", "configure", "--json",
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["engine"] == "native"
    assert data["configured_via"] == "default"


def test_configure_hindsight_redacts_api_key():
    result = _run(
        "memory-provider", "configure", "--json",
        env={
            "AGENT_NOTES_MEMORY_ENGINE": "hindsight",
            "HINDSIGHT_URL": "http://example.com:8080",
            "HINDSIGHT_API_KEY": "sk-secret-key-12345",
            "HINDSIGHT_TENANT": "prod",
            "HINDSIGHT_TIMEOUT": "45",
        },
        check=False,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["engine"] == "hindsight"
    assert data["configured_via"] == "env"
    hs = data["hindsight"]
    assert hs["url"] == "http://example.com:8080"
    assert hs["tenant"] == "prod"
    assert hs["timeout"] == 45
    assert hs["api_key"] == "sk-s***"
    raw = json.dumps(data)
    assert "secret-key-12345" not in raw


# ---------------------------------------------------------------------------
# suite doctor includes memory_provider check
# ---------------------------------------------------------------------------


def test_suite_doctor_includes_memory_provider_native():
    """The suite doctor JSON includes a memory_provider check that is skip
    for the default native engine (native health is covered by dsn_reachable)."""
    result = _run(
        "doctor", "--json",
        env={"AGENT_NOTES_MEMORY_ENGINE": "native"},
        check=False,
    )
    data = json.loads(result.stdout)
    check_names = [c["name"] for c in data["checks"]]
    assert "memory_provider" in check_names
    mp_check = next(c for c in data["checks"] if c["name"] == "memory_provider")
    assert mp_check["status"] == "skip"


def test_suite_doctor_memory_provider_fails_on_unreachable_hindsight():
    """When an external engine is configured but unreachable, the suite
    doctor reports it as a failure (not skip)."""
    result = _run(
        "doctor", "--json",
        env={
            "AGENT_NOTES_MEMORY_ENGINE": "hindsight",
            "HINDSIGHT_URL": "http://127.0.0.1:1",
            "HINDSIGHT_TIMEOUT": "2",
        },
        check=False,
    )
    data = json.loads(result.stdout)
    mp_check = next(c for c in data["checks"] if c["name"] == "memory_provider")
    assert mp_check["status"] == "fail"
