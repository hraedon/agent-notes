"""CLI configuration-failure envelopes (WI-049, Plan 026 D1).

WI-049 made the most likely first-run failures legible: a subcommand that
hits a configuration gap renders the common error envelope instead of a
traceback. The regista half of WI-049 (refusing ``init`` for an unprovisioned
regista schema, and converting regista's ``DB_NOT_FOUND``) went with the
write-through in Plan 026 D1. What remains under test here: the retired
regista writes setting is rendered the same way, and unrelated errors still
raise.
"""

from __future__ import annotations

import argparse
import json

import pytest

import agent_notes.cli as cli
from agent_notes.cli.common import EXIT_NOT_CONFIGURED
from agent_notes.core.config import reject_regista_writes


def _cmd(_args):
    reject_regista_writes()
    raise AssertionError("the retired setting must be refused before any work")


class TestDispatchRetiredSetting:
    @pytest.fixture(autouse=True)
    def _writes_on(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_NOTES_CONFIG", str(tmp_path / "missing.json"))
        monkeypatch.setenv("AGENT_NOTES_REGISTA_WRITES", "1")

    def test_json_envelope_names_the_setting_and_the_runbook(self, capsys):
        rc = cli._dispatch(_cmd, argparse.Namespace(json=True))

        assert rc == EXIT_NOT_CONFIGURED
        envelope = json.loads(capsys.readouterr().out)
        assert envelope["ok"] is False
        assert envelope["error"]["code"] == "RETIRED_SETTING"
        assert "AGENT_NOTES_REGISTA_WRITES" in envelope["error"]["message"]
        assert "docs/spikes/025-regista-cutover.md" in envelope["error"]["message"]

    def test_human_output_is_legible_not_a_traceback(self, capsys):
        rc = cli._dispatch(_cmd, argparse.Namespace(json=False))

        assert rc == EXIT_NOT_CONFIGURED
        err = capsys.readouterr().err
        assert "RETIRED_SETTING" in err
        assert "Traceback" not in err


def test_other_errors_still_raise():
    def _boom(_args):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        cli._dispatch(_boom, argparse.Namespace(json=False))
