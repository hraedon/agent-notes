"""Subprocess-level gate contract for ``agent-notes invariants probe`` (WI-071).

The genesis gate does not import agent-notes; it runs
``agent-notes invariants probe --json`` as a child process and judges stdout and
the exit code. So the contract can only be proven by running the CLI the same
way. :func:`gate_contract_violations` below is a **local re-implementation of the
umbrella's own validator** (``agent_suite.genesis_gate._parse_probe_result``,
plus the ``ProbeSpec`` for agent-notes and the preflight regex from
``agent_suite.schedule._help_exposes_invariant_probe``), transcribed rather than
imported: agent-suite is not a dependency of agent-notes, and a test that
imported it would skip on every host that lacks it — the silent-skip class this
repo already burned itself on (WI-026).

Because the transcription can drift from the umbrella, every rule below names the
umbrella behaviour it mirrors. If the gate's contract changes, these are the
lines to reconcile.

Version independence is a hard requirement here. Whether the required check
*passes* depends on the installed regista (the 0.5.x line exports no closed
lineage registry, so the probe reports ``lineage_registry_unavailable``), so the
tests assert the contract unconditionally and assert the *verdict* per branch,
with both branches spelled out.
"""

from __future__ import annotations

import json
import re
import sys

import pytest

from tests.cli_harness import run_cli

# --- transcribed from agent_suite.genesis_gate ------------------------------
PROBE_REPORT_VERSION = 1
COMPONENT = "agent-notes"
REQUIRED_CHECKS = frozenset({"agent_notes.session_identity_resolvable"})
PROBE_CHECK_STATUSES = frozenset({"pass", "measured", "fail"})
# The umbrella derives the namespace prefix as component.replace("-", "_") + "."
COMPONENT_PREFIX = COMPONENT.replace("-", "_") + "."
# ``regista.store_invariant_measurements`` is the only required check the gate
# accepts as "measured"; every other required check must be exactly "pass".
REQUIRED_SUCCESS_STATUS = "pass"

# --- transcribed from agent_suite.schedule ----------------------------------
_USAGE_PATTERN = (
    r"\busage:\s+(?:\S*[/\\])?" + re.escape("agent-notes") + r"\s+invariants\s+probe(?:\s|\[)"
)


def gate_contract_violations(stdout: str, returncode: int) -> list[str]:
    """Return every way ``stdout``/``returncode`` violate the gate contract.

    Mirrors ``_parse_probe_result``: a violation here is MALFORMED or ERROR at
    the gate, which is a *worse* outcome than a failing check because it says
    the probe is broken rather than naming the invariant.
    """
    problems: list[str] = []
    try:
        body = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return [f"stdout is not a single JSON document: {exc}"]
    if not isinstance(body, dict):
        return ["stdout JSON is not an object"]
    if body.get("component") != COMPONENT:
        problems.append(f"component != {COMPONENT!r}: {body.get('component')!r}")
    if not isinstance(body.get("ok"), bool):
        problems.append(f"ok is not a bool: {body.get('ok')!r}")
    # The umbrella checks ``type(...) is not int`` — a bool would pass an
    # isinstance check and be rejected there.
    if type(body.get("probe_version")) is not int or body["probe_version"] != PROBE_REPORT_VERSION:
        problems.append(f"probe_version must be int {PROBE_REPORT_VERSION}")
    raw_checks = body.get("checks")
    if not isinstance(raw_checks, list) or not all(isinstance(c, dict) for c in raw_checks):
        return [*problems, "checks must be a list of objects"]
    if any(c.get("status") not in PROBE_CHECK_STATUSES for c in raw_checks):
        problems.append("every check status must be one of pass/measured/fail")
    check_ids = {c["id"] for c in raw_checks if isinstance(c.get("id"), str) and c["id"].strip()}
    if len(check_ids) != len(raw_checks):
        problems.append("check ids must be unique, non-empty strings")
    missing = sorted(REQUIRED_CHECKS - check_ids)
    if missing:
        problems.append(f"required checks absent: {missing}")
    foreign = sorted(i for i in check_ids if not i.startswith(COMPONENT_PREFIX))
    if foreign:
        problems.append(f"checks owned by another component: {foreign}")
    by_id = {str(c["id"]): c for c in raw_checks}
    if body.get("ok") is True:
        wrong = sorted(
            check_id
            for check_id in REQUIRED_CHECKS
            if by_id.get(check_id, {}).get("status") != REQUIRED_SUCCESS_STATUS
        )
        if wrong:
            problems.append(f"required checks used the wrong success status: {wrong}")
    if returncode not in (0, 1):
        problems.append(f"exit code must be 0 or 1, got {returncode}")
    if (returncode == 0) != bool(body.get("ok")):
        problems.append(f"(exit == 0) must equal ok; exit={returncode} ok={body.get('ok')!r}")
    return problems


def _probe(**kwargs):
    return run_cli("invariants", "probe", "--json", check=False, **kwargs)


def _required_check(body: dict) -> dict:
    return next(c for c in body["checks"] if c["id"] == "agent_notes.session_identity_resolvable")


@pytest.fixture(scope="module")
def registry_available() -> bool:
    """Whether the *installed* regista exposes the closed lineage registry.

    Read from a child process rather than this one, so it reflects the same
    interpreter and import environment the CLI subprocess will use.
    """
    import subprocess

    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import regista; print(bool(getattr(regista, 'MODEL_LINEAGE_FAMILIES', None)))",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() == "True"


def test_probe_satisfies_the_full_gate_contract():
    """The contract holds whatever this host's verdict is."""
    proc = _probe()
    assert gate_contract_violations(proc.stdout, proc.returncode) == [], proc.stdout[-800:]


def test_stdout_is_the_report_and_nothing_else():
    """Stream purity: diagnostics belong on stderr, stdout stays parseable."""
    proc = _probe()
    assert proc.stdout.count("{") >= 1
    # json.loads over the *whole* stream — any banner, warning or second
    # document would break this, which is exactly what the gate would see.
    body = json.loads(proc.stdout)
    assert body["component"] == "agent-notes"
    assert "Traceback" not in proc.stderr


def test_verdict_matches_the_installed_registry(registry_available):
    """A wired environment passes iff regista's closed registry is available.

    Both branches are asserted so the test is meaningful on the ``SUITE.lock``
    environment (regista 0.5.5, no registry) and on a sibling 0.6 checkout.
    """
    proc = _probe(env={"AGENT_NOTES_MODEL_LINEAGE": "claude-opus"})
    assert gate_contract_violations(proc.stdout, proc.returncode) == []
    body = json.loads(proc.stdout)
    required = _required_check(body)
    if registry_available:
        assert (body["ok"], proc.returncode) == (True, 0), proc.stdout[-800:]
        assert required["reason"] == "resolved"
        assert required["evidence"]["lineage_in_registry"] is True
    else:
        assert (body["ok"], proc.returncode) == (False, 1), proc.stdout[-800:]
        assert required["reason"] == "lineage_registry_unavailable"


def test_undeclared_lineage_is_a_failing_report_not_an_error_envelope():
    """The ``_dispatch`` hazard, pinned.

    Every other command renders ``UndeclaredLineageError`` through
    ``cli/__init__.py::_dispatch`` as an ``UNDECLARED_LINEAGE`` envelope with
    exit 3 (``EXIT_NOT_CONFIGURED``). For the probe that is a double contract
    violation — exit 3 is not in ``(0, 1)`` and an error envelope is not a probe
    report — so the failure must be *reported*, not raised.
    """
    proc = _probe(strip_keys=("AGENT_NOTES_MODEL_LINEAGE",))
    assert gate_contract_violations(proc.stdout, proc.returncode) == [], proc.stdout[-800:]
    body = json.loads(proc.stdout)
    assert (body["ok"], proc.returncode) == (False, 1)
    assert "error" not in body
    required = _required_check(body)
    assert required["reason"] == "lineage_undeclared"
    assert required["evidence"]["write_path_error_code"] == "UNDECLARED_LINEAGE"


def test_lineage_outside_the_registry_is_a_failing_report(registry_available):
    if not registry_available:
        pytest.skip("installed regista exports no closed lineage registry to violate")
    proc = _probe(env={"AGENT_NOTES_MODEL_LINEAGE": "opencode-glm-5.3"})
    assert gate_contract_violations(proc.stdout, proc.returncode) == []
    body = json.loads(proc.stdout)
    assert (body["ok"], proc.returncode) == (False, 1)
    required = _required_check(body)
    assert required["reason"] == "lineage_not_in_registry"


def test_probe_does_not_reach_the_store():
    """Read-only, proven end to end.

    A DSN pointing at a closed port with regista writes enabled: a probe that
    connected would hang or fail. It must produce the same verdict as a probe
    with no store configured at all.
    """
    dead = "postgresql://nobody@127.0.0.1:1/nothing"
    proc = _probe(
        env={
            "AGENT_NOTES_MODEL_LINEAGE": "claude-opus",
            "REGISTA_DSN": dead,
            "AGENT_NOTES_DSN": dead,
            "AGENT_NOTES_REGISTA_WRITES": "1",
        }
    )
    assert gate_contract_violations(proc.stdout, proc.returncode) == [], proc.stderr[-800:]
    baseline = _probe(env={"AGENT_NOTES_MODEL_LINEAGE": "claude-opus"})
    assert json.loads(proc.stdout)["ok"] == json.loads(baseline.stdout)["ok"]
    assert "Traceback" not in proc.stderr


def test_help_exposes_the_command_to_the_schedule_preflight():
    """``schedule install``'s parser-only capability check must recognize us.

    The umbrella normalizes whitespace, lowercases, joins stdout+stderr, and
    requires a usage line naming the executable immediately followed by
    ``invariants probe``. Prose mentioning the phrase is deliberately not
    accepted there, so only the real argparse usage line satisfies it.
    """
    proc = run_cli("invariants", "probe", "--help", check=False)
    assert proc.returncode == 0
    normalized = " ".join(f"{proc.stdout}\n{proc.stderr}".split()).lower()
    assert re.search(_USAGE_PATTERN, normalized), normalized[:400]


def test_usage_regex_rejects_prose_mentioning_the_command():
    """Calibration: the transcribed regex is not a substring match."""
    assert not re.search(_USAGE_PATTERN, "run agent-notes invariants probe to measure identity")
    assert re.search(_USAGE_PATTERN, "usage: agent-notes invariants probe [-h] [--json]")
    assert re.search(_USAGE_PATTERN, "usage: /usr/bin/agent-notes invariants probe [-h]")


def test_group_help_is_exit_zero():
    """``invariants`` with no verb prints help and exits 0, like every other noun.

    ``tests/test_cli_manifest.py`` walks the parser tree with ``--help`` and
    treats a non-zero exit as "this command does not exist".
    """
    proc = run_cli("invariants", check=False)
    assert proc.returncode == 0
    assert "probe" in proc.stdout


def test_human_output_names_every_check_and_keeps_the_exit_code():
    proc = run_cli("invariants", "probe", check=False, env={"AGENT_NOTES_MODEL_LINEAGE": "kimi"})
    assert proc.returncode in (0, 1)
    assert "agent_notes.session_identity_resolvable" in proc.stdout
    assert "agent_notes.lineage_registry_available" in proc.stdout
    assert "agent_notes.write_path_refuses_unresolvable_lineage" in proc.stdout
    # Human output is not JSON, so it must never be mistaken for the report.
    with pytest.raises(json.JSONDecodeError):
        json.loads(proc.stdout)
