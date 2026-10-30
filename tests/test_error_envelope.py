"""The CLI error envelope is a wire contract (Plan 026 D3, Phase 3 exit).

``tests/fixtures/cli_error_envelopes.json`` was captured from ``bfff823``, the
last commit that depended on regista 0.7.2, with one sample per error code:
generic codes straight through ``emit_error``, and the identity and retired-
setting codes end to end through the CLI's ``_dispatch`` (the actor path that
used ``regista.validate_principal_id``). The vendored ``ErrorCode`` and
principal grammar must reproduce those stdout bytes exactly, because skills
and hooks match on them.
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

import pytest

from agent_notes.cli import _dispatch
from agent_notes.cli.common import EXIT_NOT_CONFIGURED, emit_error
from agent_notes.core import actor
from agent_notes.core.errors import AgentNotesError, ErrorCode

_ROOT = Path(__file__).resolve().parents[1]
_GOLDENS: dict[str, str] = json.loads(
    (_ROOT / "tests" / "fixtures" / "cli_error_envelopes.json").read_text(encoding="utf-8")
)

# Codes whose sample was captured end to end through ``_dispatch``; the value
# is the environment that produced it.
_DISPATCHED: dict[str, dict[str, str]] = {
    "ACTOR_ID_NOT_CONFIGURED": {},
    "PRINCIPAL_ID_UNGRAMMATICAL": {"AGENT_NOTES_ACTOR_ID": "robot:x"},
    "PRINCIPAL_ID_NOT_CANONICAL": {"AGENT_NOTES_ACTOR_ID": "mvmcc03-agent"},
    "RETIRED_SETTING": {"AGENT_NOTES_REGISTA_WRITES": "1", "AGENT_NOTES_ACTOR_ID": "agent:x"},
}
_CAPTURE_ENV = {
    "AGENT_SUITE_CONFIG": "/nonexistent/agent-suite/suite.env",
    "AGENT_SUITE_SYSTEM_CONFIG": "/nonexistent/agent-suite/system-suite.env",
    "AGENT_NOTES_CONFIG": "/nonexistent/agent-notes/config.json",
}
_GENERIC = sorted(set(_GOLDENS) - set(_DISPATCHED))


def test_goldens_cover_exactly_the_error_codes():
    assert set(_GOLDENS) == {code.value for code in ErrorCode}


def test_codes_are_their_own_names():
    for code in ErrorCode:
        assert code.value == code.name


@pytest.mark.parametrize("code", _GENERIC)
def test_generic_envelope_is_byte_identical(code, capsys):
    sample = {"sample": code.lower(), "n": 1}
    for spelled in (ErrorCode(code), code):
        rc = emit_error(spelled, f"sample {code} failure", use_json=True, detail=sample)
        assert rc == 1
        assert capsys.readouterr().out == _GOLDENS[code]


@pytest.mark.parametrize("code", sorted(_DISPATCHED))
def test_dispatched_envelope_is_byte_identical(code, monkeypatch, capsys):
    for name in (
        "AGENT_NOTES_ACTOR_ID",
        "REGISTA_PRINCIPAL_ID",
        "AGENT_NOTES_REGISTA_WRITES",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in {**_CAPTURE_ENV, **_DISPATCHED[code]}.items():
        monkeypatch.setenv(name, value)

    rc = _dispatch(lambda args: (actor.write_actor(), 0)[1], argparse.Namespace(json=True))

    assert rc == EXIT_NOT_CONFIGURED
    # The archived capture uses a POSIX path; only that environment-dependent
    # value changes on Windows. Keep every other stdout byte pinned.
    captured_path = _CAPTURE_ENV["AGENT_NOTES_CONFIG"]
    expected = _GOLDENS[code].replace(captured_path, json.dumps(str(Path(captured_path)))[1:-1])
    assert capsys.readouterr().out == expected


def test_agent_notes_error_renders_like_registas():
    exc = AgentNotesError(ErrorCode.NOT_FOUND, "no such thing", {"k": "v"})
    assert str(exc) == "[NOT_FOUND] no such thing"
    assert exc.code == "NOT_FOUND"
    assert exc.detail == {"k": "v"}


# ---------------------------------------------------------------------------
# Every string code in src/ is an ErrorCode member
# ---------------------------------------------------------------------------

# Callables whose first positional argument is an envelope code.
_CODE_TAKING_CALLS = frozenset({"emit_error", "ScopeError", "_CheckoutError"})


def _string_codes_in_src() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    src = _ROOT / "src" / "agent_notes"
    for path in src.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        where = str(path.relative_to(src))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                if name in _CODE_TAKING_CALLS and node.args:
                    first = node.args[0]
                    if isinstance(first, ast.Constant) and isinstance(first.value, str):
                        found.setdefault(first.value, set()).add(where)
            if isinstance(node, ast.ClassDef) and node.name.endswith("Error"):
                for stmt in node.body:
                    if (
                        isinstance(stmt, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == "code" for t in stmt.targets)
                        and isinstance(stmt.value, ast.Constant)
                        and isinstance(stmt.value.value, str)
                    ):
                        found.setdefault(stmt.value.value, set()).add(where)
    return found


def test_every_string_code_in_src_is_an_error_code():
    found = _string_codes_in_src()
    # The scan must actually see the call sites, or it proves nothing.
    assert {"NOT_FOUND", "VALIDATION_FAILED", "RETIRED_SETTING"} <= set(found)
    unknown = {code: sorted(where) for code, where in found.items() if code not in ErrorCode}
    assert not unknown, f"add these to core.errors.ErrorCode: {unknown}"
