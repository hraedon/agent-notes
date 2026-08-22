"""Regression tests for `agent-notes init` slug/provisioning safety (WI-049).

The Plan 020 Linux qualification ran
``agent-notes init /root/qual-workspace --workspace qual_linux`` and got a
success message — for a project slug silently derived from the directory
basename, whose regista schema did not exist. Every subsequent command in the
workspace then died with a raw ``DB_NOT_FOUND`` traceback naming an internal
API (``Regista.create_project()``), and there was no CLI way back.

Three fixes under test here:

- ``init --slug`` lets the operator name the project instead of accepting the
  basename guess.
- With regista enabled, ``init`` verifies the slug's schema exists *before*
  persisting anything, and refuses (naming ``regista provision --project X``)
  instead of registering a project that can never be used.
- A ``DB_NOT_FOUND`` escaping any subcommand is converted to the common error
  envelope with the real remediation, not a traceback.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
from regista._errors import ErrorCode, RegistaError

import agent_notes.cli as cli
from agent_notes.cli.common import EXIT_NOT_CONFIGURED
from agent_notes.core import face_factory


class _FakeCfg:
    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.project = "agent_notes"


def _db_not_found(schema: str) -> RegistaError:
    return RegistaError(
        ErrorCode.DB_NOT_FOUND,
        f"Project schema {schema!r} does not exist. Use Regista.create_project() to initialize it.",
    )


@pytest.fixture
def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "qual-workspace"
    (repo / ".git").mkdir(parents=True)
    return repo


class TestSchemaMissingCheck:
    def test_disabled_regista_skips_the_check(self, monkeypatch):
        monkeypatch.setattr("agent_notes.core.config.regista_config", lambda: _FakeCfg(False))
        assert cli._regista_schema_missing("anything") is None

    def test_db_not_found_reports_the_schema_name(self, monkeypatch):
        monkeypatch.setattr("agent_notes.core.config.regista_config", lambda: _FakeCfg(True))

        def _raise():
            raise _db_not_found("qual_workspace")

        monkeypatch.setattr(face_factory, "get_face", _raise)
        before = face_factory.current_project()
        assert cli._regista_schema_missing("qual-workspace") == "qual_workspace"
        # The check must not leave the routing context pointed at the probe.
        assert face_factory.current_project() == before
        assert face_factory.current_project() != "qual_workspace" or before == "qual_workspace"

    def test_unreachable_regista_warns_but_does_not_refuse(self, monkeypatch, capsys):
        monkeypatch.setattr("agent_notes.core.config.regista_config", lambda: _FakeCfg(True))

        def _raise():
            raise RegistaError(ErrorCode.VALIDATOR_FAILED, "connection refused")

        monkeypatch.setattr(face_factory, "get_face", _raise)
        assert cli._regista_schema_missing("qual-workspace") is None
        assert "could not verify" in capsys.readouterr().err

    def test_unmappable_slug_is_refused(self, monkeypatch, capsys):
        monkeypatch.setattr("agent_notes.core.config.regista_config", lambda: _FakeCfg(True))
        assert cli._regista_schema_missing("1-starts-with-digit") == "1-starts-with-digit"
        assert "cannot name a regista schema" in capsys.readouterr().err


class TestInitRefusal:
    @pytest.fixture(autouse=True)
    def _missing_schema(self, monkeypatch):
        monkeypatch.setattr("agent_notes.core.config.regista_config", lambda: _FakeCfg(True))

        def _raise():
            raise _db_not_found("qual_workspace")

        monkeypatch.setattr(face_factory, "get_face", _raise)

        # init must not persist anything when it refuses.
        def _forbidden(*args, **kwargs):
            raise AssertionError("init persisted state despite refusing registration")

        monkeypatch.setattr("agent_notes.core.db.get_or_create_workspace", _forbidden)
        monkeypatch.setattr("agent_notes.core.db.get_or_create_project", _forbidden)

    def test_init_refuses_and_names_the_provision_command(self, _repo, capsys):
        rc = cli.cmd_init(str(_repo))
        assert rc == EXIT_NOT_CONFIGURED
        captured = capsys.readouterr()
        assert "registered" not in captured.out.lower()
        assert "refusing to register" in captured.err
        assert "regista provision --project qual_workspace" in captured.err
        # The slug was a basename guess, so the refusal points at --slug.
        assert "--slug" in captured.err

    def test_explicit_slug_refusal_does_not_blame_the_basename(self, _repo, capsys):
        rc = cli.cmd_init(str(_repo), slug="qual-workspace")
        assert rc == EXIT_NOT_CONFIGURED
        assert "--slug" not in capsys.readouterr().err


class TestDispatchDbNotFound:
    def test_json_envelope_with_remediation(self, capsys):
        def _cmd(args):
            raise _db_not_found("qual_workspace")

        face_factory.set_current_project("qual_workspace")
        try:
            rc = cli._dispatch(_cmd, argparse.Namespace(json=True))
        finally:
            face_factory.set_current_project(None)

        assert rc == EXIT_NOT_CONFIGURED
        envelope = json.loads(capsys.readouterr().out)
        assert envelope["error"]["code"] == "DB_NOT_FOUND"
        assert "qual_workspace" in envelope["error"]["message"]
        assert "regista provision --project qual_workspace" in envelope["error"]["detail"]

    def test_human_output_is_legible_not_a_traceback(self, capsys):
        def _cmd(args):
            raise _db_not_found("qual_workspace")

        face_factory.set_current_project("qual_workspace")
        try:
            rc = cli._dispatch(_cmd, argparse.Namespace(json=False))
        finally:
            face_factory.set_current_project(None)

        assert rc == EXIT_NOT_CONFIGURED
        err = capsys.readouterr().err
        assert "regista provision --project qual_workspace" in err
        assert "Traceback" not in err

    def test_other_regista_errors_still_raise(self):
        def _cmd(args):
            raise RegistaError(ErrorCode.VALIDATOR_FAILED, "boom")

        with pytest.raises(RegistaError):
            cli._dispatch(_cmd, argparse.Namespace(json=False))


@pytest.mark.postgres
def test_init_refuses_against_a_real_regista_with_no_schema(
    ephemeral_db, _repo, tmp_path, monkeypatch, capsys
):
    """End-to-end qualification scenario: real Postgres, no provisioned schema.

    regista writes are enabled against the ephemeral database (which has no
    regista schemas at all), exactly the artifact-host state: init must refuse
    and name the provision command, not register a workspace-breaking project.
    """
    # A real v6 keyset (not a placeholder HMAC manifest): the Regista
    # constructor must be able to load keys for the face it will never get to
    # use — init is expected to refuse before any write.
    from regista.testing import make_v6_keyset

    keyset = make_v6_keyset(tmp_path, principals=("agent:worker",), filename="v6_keys.json")
    monkeypatch.setenv("AGENT_NOTES_REGISTA_WRITES", "1")
    monkeypatch.setenv("REGISTA_DSN", ephemeral_db)
    monkeypatch.setenv("REGISTA_KEY_PATH", keyset.path)
    face_factory.reset_face()
    try:
        rc = cli.cmd_init(str(_repo))
    finally:
        face_factory.reset_face()

    assert rc == EXIT_NOT_CONFIGURED
    captured = capsys.readouterr()
    assert "regista provision --project qual_workspace" in captured.err
    assert "registered" not in captured.out.lower()
