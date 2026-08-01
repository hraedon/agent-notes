"""Regression tests for agent-notes-migrate DSN resolution (WI-051).

The Plan 020 Linux qualification found that ``agent-notes-migrate`` read
``AGENT_NOTES_DSN`` straight from ``os.environ`` and exited when it was unset —
with the DSN correctly present in ``/etc/agent-suite/suite.env``, which the
bootstrap contract (§2) says every suite tool resolves. The runner must go
through the same layered resolver as the rest of agent-notes
(``core.config.resolve_dsn``): process env > per-user suite.env > system
suite.env > tool config file.

No database is required: ``run_all`` is monkeypatched to capture the DSN the
runner actually resolved.
"""

from __future__ import annotations

import pytest

from agent_notes.scripts import migrate


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch, tmp_path):
    """Isolate from the host's env, suite.env files, and config file."""
    monkeypatch.delenv("AGENT_NOTES_DSN", raising=False)
    monkeypatch.setenv("AGENT_NOTES_CONFIG", str(tmp_path / "missing-config.json"))
    monkeypatch.setenv("AGENT_SUITE_CONFIG", str(tmp_path / "user-suite.env"))
    monkeypatch.setenv("AGENT_SUITE_SYSTEM_CONFIG", str(tmp_path / "system-suite.env"))


def _run_main(monkeypatch, *argv: str) -> list[tuple[str, object]]:
    """Run migrate.main() with *argv*, capturing run_all calls."""
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        migrate, "run_all", lambda dsn, schema_dir: calls.append((dsn, schema_dir))
    )
    monkeypatch.setattr("sys.argv", ["agent-notes-migrate", *argv])
    migrate.main()
    return calls


def test_migrate_all_resolves_dsn_from_system_suite_env(monkeypatch, tmp_path):
    """The qualification scenario: DSN only in the system suite.env."""
    (tmp_path / "system-suite.env").write_text(
        "AGENT_NOTES_DSN=postgresql://suite-host/agent_notes\n"
    )
    calls = _run_main(monkeypatch, "--all")
    assert [dsn for dsn, _ in calls] == ["postgresql://suite-host/agent_notes"]


def test_migrate_all_process_env_still_wins(monkeypatch, tmp_path):
    (tmp_path / "system-suite.env").write_text(
        "AGENT_NOTES_DSN=postgresql://suite-host/agent_notes\n"
    )
    monkeypatch.setenv("AGENT_NOTES_DSN", "postgresql://process-env/agent_notes")
    calls = _run_main(monkeypatch, "--all")
    assert [dsn for dsn, _ in calls] == ["postgresql://process-env/agent_notes"]


def test_migrate_all_without_any_dsn_names_the_searched_files(monkeypatch, tmp_path):
    """A missing DSN exits with the files that were searched, per the WI."""
    with pytest.raises(SystemExit) as exc_info:
        _run_main(monkeypatch, "--all")
    message = str(exc_info.value)
    assert "AGENT_NOTES_DSN" in message
    assert str(tmp_path / "user-suite.env") in message
    assert str(tmp_path / "system-suite.env") in message
