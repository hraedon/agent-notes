"""Codex-specific doctor states (Plan 019 WI-3.1)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agent_notes.cli.harness import _install_harness_one
from agent_notes.scripts import doctor
from agent_notes.scripts.doctor import _check_codex_harness


@pytest.mark.parametrize(
    ("installed_version", "expected_state"), [("9.8.7", "enabled"), ("9.8.6", "stale")]
)
def test_plugin_probe_limits_query_to_configured_sources(
    monkeypatch, tmp_path, installed_version, expected_state
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        '[plugins."agent-notes@local-notes"]\nenabled = true\n'
        '[plugins."other@remote"]\nenabled = true\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("shutil.which", lambda name: "codex.exe")
    monkeypatch.setattr("importlib.metadata.version", lambda name: "9.8.7")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {"installed": [
                    {"name": "agent-notes", "version": installed_version, "enabled": True}
                ]}
            ),
        )

    monkeypatch.setattr(subprocess, "run", run)
    assert doctor._probe_codex_agent_notes_plugin()[0] == expected_state
    assert calls == [["codex", "plugin", "list", "--json", "--marketplace", "local-notes"]]


def _use_home(monkeypatch, home: Path, plugin_state: str = "absent") -> None:
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setattr(
        doctor,
        "_probe_codex_agent_notes_plugin",
        lambda: (plugin_state, f"fixture {plugin_state}"),
    )


def test_codex_doctor_reports_absent_as_informational(monkeypatch, tmp_path) -> None:
    _use_home(monkeypatch, tmp_path)

    ok, detail = _check_codex_harness()

    assert ok is None
    assert detail.startswith("codex: absent")


def test_codex_doctor_reports_current_direct_install(monkeypatch, tmp_path) -> None:
    _use_home(monkeypatch, tmp_path)
    result, _warnings = _install_harness_one(
        "codex", dry_run=False, user=None, source=None, dest=None, home=tmp_path
    )
    assert result["status"] == "installed"

    ok, detail = _check_codex_harness()

    assert ok is True
    assert detail.startswith("codex: direct wired")
    assert "trust-unverified" in detail


def test_codex_doctor_names_modified_skill(monkeypatch, tmp_path) -> None:
    _use_home(monkeypatch, tmp_path)
    _install_harness_one("codex", dry_run=False, user=None, source=None, dest=None, home=tmp_path)
    changed = tmp_path / ".agents" / "skills" / "start" / "SKILL.md"
    changed.write_text(changed.read_text(encoding="utf-8") + "\nlocal edit\n", encoding="utf-8")

    ok, detail = _check_codex_harness()

    assert ok is False
    assert detail.startswith("codex: stale")
    assert "start: installed file modified" in detail


def test_codex_doctor_names_missing_skill(monkeypatch, tmp_path) -> None:
    _use_home(monkeypatch, tmp_path)
    _install_harness_one("codex", dry_run=False, user=None, source=None, dest=None, home=tmp_path)
    missing = tmp_path / ".agents" / "skills" / "reflect" / "SKILL.md"
    missing.unlink()

    ok, detail = _check_codex_harness()

    assert ok is False
    assert "reflect: installed file missing" in detail


def test_codex_doctor_reports_plugin_and_trust_state(monkeypatch, tmp_path) -> None:
    _use_home(monkeypatch, tmp_path, plugin_state="enabled")

    ok, detail = _check_codex_harness()

    assert ok is True
    assert detail.startswith("codex: plugin wired")
    assert "trust-unverified" in detail


@pytest.mark.parametrize("plugin_state", ["enabled", "disabled", "stale", "absent"])
def test_plugin_only_install_satisfies_generic_wiring_checks(
    monkeypatch, tmp_path, plugin_state
) -> None:
    """A healthy plugin must not prompt installation of duplicate direct hooks."""
    _use_home(monkeypatch, tmp_path, plugin_state=plugin_state)
    # Exercise run_json's real wiring aggregation, without contacting a store.
    monkeypatch.setattr(doctor, "_check_dsn", lambda: (False, "fixture store offline"))
    monkeypatch.setattr(doctor, "_check_regista_reachable", lambda cfg: (None, "not configured"))
    monkeypatch.setattr(doctor, "_check_secrets_backend", lambda cfg: (None, "not configured"))
    monkeypatch.setattr(doctor, "_check_memory_provider", lambda: (None, "native"))
    payload, _code = doctor.run_json()
    checks = {check["name"]: check for check in payload["checks"]}
    expected = "ok" if plugin_state == "enabled" else "fail"
    assert checks["skills_installed"]["status"] == expected
    assert checks["harness_wired"]["status"] == expected
    assert checks["codex_harness"]["status"] == ("skip" if plugin_state == "absent" else expected)
    assert checks["dsn_reachable"]["status"] == "fail"
    assert not (tmp_path / ".agents").exists()


def test_codex_doctor_reports_plugin_direct_duplicate(monkeypatch, tmp_path) -> None:
    _use_home(monkeypatch, tmp_path, plugin_state="enabled")
    _install_harness_one("codex", dry_run=False, user=None, source=None, dest=None, home=tmp_path)

    ok, detail = _check_codex_harness()

    assert ok is False
    assert detail.startswith("codex: duplicate")


def test_codex_doctor_names_modified_hook(monkeypatch, tmp_path) -> None:
    _use_home(monkeypatch, tmp_path)
    _install_harness_one("codex", dry_run=False, user=None, source=None, dest=None, home=tmp_path)
    hook_path = tmp_path / ".codex" / "hooks.json"
    hooks = json.loads(hook_path.read_text())
    hooks["hooks"]["Stop"][0]["hooks"][0]["timeout"] = 99
    hook_path.write_text(json.dumps(hooks), encoding="utf-8")

    ok, detail = _check_codex_harness()

    assert ok is False
    assert detail.startswith("codex: stale")
    assert "Stop hook modified" in detail
