"""Codex-specific doctor states (Plan 019 WI-3.1)."""

from __future__ import annotations

import json
from pathlib import Path

from agent_notes.cli.harness import _install_harness_one
from agent_notes.scripts import doctor
from agent_notes.scripts.doctor import _check_codex_harness


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
