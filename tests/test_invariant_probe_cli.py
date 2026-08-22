"""Subprocess-level contract tests for the v6 identity probe."""

from __future__ import annotations

import json
import re

from tests.cli_harness import run_cli

_CHECK_ID = "agent_notes.session_identity_resolvable"
_USAGE = re.compile(r"\busage:\s+(?:\S*[/\\])?agent-notes\s+invariants\s+probe(?:\s|\[)")


def _probe(**kwargs):
    return run_cli("invariants", "probe", "--json", check=False, **kwargs)


def _body(proc):
    return json.loads(proc.stdout)


def test_probe_stdout_is_one_contract_json_document():
    proc = _probe(env={"AGENT_NOTES_ACTOR_ID": "agent:worker"})

    body = _body(proc)
    assert proc.returncode == 0
    assert body["component"] == "agent-notes"
    assert body["ok"] is True
    assert body["checks"][0]["id"] == _CHECK_ID
    assert "Traceback" not in proc.stderr


def test_probe_reports_missing_actor_as_a_failed_check():
    proc = _probe(strip_keys=("AGENT_NOTES_ACTOR_ID", "REGISTA_PRINCIPAL_ID"))

    body = _body(proc)
    assert proc.returncode == 1
    assert body["ok"] is False
    assert body["checks"][0]["reason"] == "identity_not_configured"
    assert "error" not in body


def test_probe_reports_invalid_actor_as_a_failed_check():
    proc = _probe(env={"AGENT_NOTES_ACTOR_ID": "legacy-bare-name"})

    body = _body(proc)
    assert proc.returncode == 1
    assert body["checks"][0]["reason"] == "identity_invalid"


def test_probe_does_not_reach_a_configured_store():
    dead = "postgresql://nobody@127.0.0.1:1/nothing"
    proc = _probe(
        env={
            "AGENT_NOTES_ACTOR_ID": "agent:worker",
            "REGISTA_DSN": dead,
            "AGENT_NOTES_DSN": dead,
            "AGENT_NOTES_REGISTA_WRITES": "1",
        }
    )

    assert proc.returncode == 0
    assert _body(proc)["ok"] is True


def test_help_exposes_the_command_to_schedule_preflight():
    proc = run_cli("invariants", "probe", "--help", check=False)
    normalized = " ".join(f"{proc.stdout}\n{proc.stderr}".split()).lower()

    assert proc.returncode == 0
    assert _USAGE.search(normalized)
