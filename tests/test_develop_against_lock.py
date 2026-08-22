"""Develop-against-lock enforcement for the v6 port."""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

import pytest
from packaging.specifiers import SpecifierSet
from packaging.version import Version

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _ROOT / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import suite_lock  # noqa: E402

_CI = _ROOT / ".github" / "workflows" / "ci.yml"


def _lock() -> dict:
    return tomllib.loads((_ROOT / "SUITE.lock").read_text(encoding="utf-8"))


def test_locked_version_is_read_from_suite_lock():
    assert suite_lock.regista_version() == _lock()["spine"]["version"]


def test_default_requirement_pins_the_locked_release(monkeypatch):
    monkeypatch.delenv("DEV_AGAINST", raising=False)
    version = _lock()["spine"]["version"]
    assert suite_lock.regista_requirement() == [f"regista-hraedon=={version}"]


def test_lock_mode_is_explicit_default():
    version = _lock()["spine"]["version"]
    assert suite_lock.regista_requirement(mode="lock") == [f"regista-hraedon=={version}"]


def test_git_ref_hatch_is_explicit():
    assert suite_lock.regista_requirement(mode="main") == [
        "regista-hraedon @ git+https://github.com/hraedon/regista.git@main"
    ]
    assert suite_lock.regista_requirement(mode="feature/x") == [
        "regista-hraedon @ git+https://github.com/hraedon/regista.git@feature/x"
    ]


def test_ci_uses_the_paved_installer_without_hardcoded_pins():
    ci = _CI.read_text(encoding="utf-8")
    assert ci.count("scripts/dev-install.py") >= 2
    assert not re.findall(r"regista-hraedon==\s*\d[\w.]*", ci)
    assert "git+https://github.com/hraedon/regista" not in ci


def test_dev_install_resolves_the_version_from_the_lock():
    assert "regista-hraedon==" not in (_SCRIPTS / "dev-install.py").read_text(encoding="utf-8")


def test_v6_dependency_floor_is_declared_in_project_metadata():
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    spec = next(
        item for item in project["project"]["dependencies"] if item.startswith("regista-hraedon")
    )
    assert spec == "regista-hraedon>=0.7.0,<0.8"


# ---------------------------------------------------------------------------
# SUITE.lock ↔ pyproject version coherence (lock honesty)
#
# The face-local lock and published dependency range must describe one tested
# release. This prevents editable-source development from hiding a stale or
# impossible shipped dependency combination.
# ---------------------------------------------------------------------------


def _pyproject_regista_specifier() -> tuple[str, SpecifierSet]:
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    spec = next(
        item for item in project["project"]["dependencies"] if item.startswith("regista-hraedon")
    )
    return spec, SpecifierSet(spec.split(";", 1)[0].removeprefix("regista-hraedon"))


def test_suite_lock_spine_version_satisfies_the_published_pyproject_range():
    """The locked spine release must sit inside the range pyproject publishes.

    A lock pin outside the published bounds means one of two lies: developing
    against a version the package metadata forbids, or publishing a range the
    lock never tested.
    """

    spec_text, specifier = _pyproject_regista_specifier()
    locked = Version(_lock()["spine"]["version"])

    assert locked in specifier, (
        f"SUITE.lock pins regista {_lock()['spine']['version']!r} but pyproject "
        f"publishes {spec_text!r}: the ported lock pin is not coherent with the "
        "published dependency range."
    )


def test_suite_lock_pyproject_floor_matches_pyproject():
    """The lock's recorded mirror of the pyproject range must not drift."""

    spec_text, _specifier = _pyproject_regista_specifier()
    assert _lock()["spine"]["pyproject_floor"] == spec_text


def test_suite_lock_envelope_is_v6():
    """The ported face writes v6 envelopes; the lock must say so."""

    assert _lock()["envelope"]["envelope_version"] == "v6"


# ---------------------------------------------------------------------------
# v6 identity-override surfaces — structural (AST) tripwire
#
# The old check grepped raw source for substrings, so a history note in a
# docstring counted the same as a reintroduced parameter. This one parses the
# tree and forbids actual surfaces: parameter names, function names, argparse
# flags, environment-variable literals, and on_behalf_of uses — while leaving
# prose (docstrings/comments) free to explain what was removed.
# ---------------------------------------------------------------------------

_FORBIDDEN_FUNCTION_NAMES = frozenset({"actor_with_overrides"})
_FORBIDDEN_PARAMETERS = frozenset(
    {
        "model_lineage",
        "on_behalf_of",
        "principal_id",
        "clear_principal",
    }
)
_FORBIDDEN_ENV_CONSTANTS = frozenset(
    {
        "AGENT_NOTES_PRINCIPAL_ID",
        "AGENT_NOTES_MODEL_LINEAGE",
        "AGENT_NOTES_PRINCIPAL_KIND",
        "AGENT_NOTES_PRINCIPAL_DISPLAY_NAME",
    }
)
_FORBIDDEN_FLAG_PREFIXES = ("--actor-id", "--model-lineage")


def _iter_source_modules():
    for path in sorted((_ROOT / "src" / "agent_notes").rglob("*.py")):
        yield path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _docstring_node_ids(tree: ast.AST) -> set[int]:
    """Node ids of docstrings (first-statement Constant expressions)."""

    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def _add_argument_flag(call: ast.Call) -> str | None:
    """The declared flag string of an add_argument call, if recognizable."""

    func = call.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    if name != "add_argument":
        return None
    if call.args and isinstance(call.args[0], ast.Constant):
        value = call.args[0].value
        if isinstance(value, str):
            return value
    return None


def test_identity_override_parameters_are_absent_from_writer_signatures():
    """No writer surface accepts a per-call identity override parameter."""

    for path, tree in _iter_source_modules():
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert node.name not in _FORBIDDEN_FUNCTION_NAMES, (
                    f"{path.name}: {node.name}() re-introduces an identity-override surface"
                )
                params = [
                    *(a.arg for a in getattr(node.args, "posonlyargs", [])),
                    *(a.arg for a in node.args.args),
                    *(a.arg for a in node.args.kwonlyargs),
                ]
                if node.args.vararg:
                    params.append(node.args.vararg.arg)
                if node.args.kwarg:
                    params.append(node.args.kwarg.arg)
                clashes = _FORBIDDEN_PARAMETERS.intersection(params)
                assert not clashes, (
                    f"{path.name}: {node.name}() declares identity-override "
                    f"parameter(s) {sorted(clashes)}"
                )


def test_identity_override_cli_flags_and_env_names_are_absent():
    """No argparse flag or environment literal re-opens an identity override."""

    for path, tree in _iter_source_modules():
        docstrings = _docstring_node_ids(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                flag = _add_argument_flag(node)
                assert flag is None or not flag.startswith(_FORBIDDEN_FLAG_PREFIXES), (
                    f"{path.name}: argparse flag {flag!r} re-introduces an "
                    "identity-override surface"
                )
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) in docstrings:
                    continue
                assert node.value not in _FORBIDDEN_ENV_CONSTANTS, (
                    f"{path.name}: environment literal {node.value!r} "
                    "re-introduces an identity-override surface"
                )
                assert node.value != "on_behalf_of", (
                    f"{path.name}: the on_behalf_of member is not part of a v6 event"
                )
            if isinstance(node, ast.Attribute):
                assert node.attr != "on_behalf_of", (
                    f"{path.name}: on_behalf_of attribute use re-introduces the "
                    "legacy proxy-principal surface"
                )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
