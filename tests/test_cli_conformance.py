"""agent-notes' CLI run through the CLI contract v1 conformance kit (Plan 018 WI-2).

The kit is the centrally versioned package ``agent_suite.conformance``, consumed
pinned (never copied) from the public agent-suite repo. These are agent-notes'
component-side fixtures against its own CLI.

Every case is hermetic: it strips the store configuration
(``AGENT_NOTES_DSN`` / ``AGENT_NOTES_REGISTA_DSN`` / ``REGISTA_DSN``) so results
depend on the contract, not on whether the test Postgres is up. agent-notes
keeps its decision-52 exit-code taxonomy (2/3/4); the kit checks the contract
invariants (stream purity, envelope shape, no tracebacks, usage=exit 2), which
those codes satisfy.
"""

from __future__ import annotations

import sys

import pytest

# The conformance kit is installed by CI as a dedicated pinned step (it is a
# git+SHA direct reference, which PyPI forbids in package metadata, so it is not
# a pyproject dependency). Skip cleanly if it is absent — but note CI installs it
# in a step that fails loudly, so a missing kit in CI never silently skips here.
conformance = pytest.importorskip("agent_suite.conformance")

BrokenPipeCase = conformance.BrokenPipeCase
ErrorCase = conformance.ErrorCase
SuccessCase = conformance.SuccessCase
UsageCase = conformance.UsageCase
run_broken_pipe_case = conformance.run_broken_pipe_case
run_error_case = conformance.run_error_case
run_success_case = conformance.run_success_case
run_usage_case = conformance.run_usage_case

_CLI = (sys.executable, "-m", "agent_notes.cli")

# Strip any store configuration inherited from the environment (including the
# CI testcontainer) so the cases below never depend on a reachable database.
_HERMETIC_UNSET = ("AGENT_NOTES_DSN", "AGENT_NOTES_REGISTA_DSN", "REGISTA_DSN", "REGISTA_PROJECT")


SUCCESS_CASES = [
    # A dry-run skill install is a pure filesystem plan: no store, JSON out.
    SuccessCase(
        name="install-skills-dry-run-json",
        argv=(*_CLI, "install-skills", "--dry-run", "--json"),
        unset_env=_HERMETIC_UNSET,
    ),
]

ERROR_CASES = [
    # With no project/workspace resolvable and no --path, the CLI reports a
    # documented PROJECT_NOT_RESOLVED envelope before touching a store.
    ErrorCase(
        name="memory-list-no-project",
        argv=(*_CLI, "memory", "list", "--json"),
        expect_code="PROJECT_NOT_RESOLVED",
        unset_env=_HERMETIC_UNSET,
    ),
]

USAGE_CASES = [
    UsageCase(name="unknown-verb", argv=(*_CLI, "bogusverb")),
]

BROKEN_PIPE_CASES = [
    BrokenPipeCase(name="memory-list-broken-pipe", argv=(*_CLI, "memory", "list", "--json")),
]


@pytest.mark.parametrize("case", SUCCESS_CASES, ids=lambda c: c.name)
def test_success_conformance(case: SuccessCase) -> None:
    assert run_success_case(case) == []


@pytest.mark.parametrize("case", ERROR_CASES, ids=lambda c: c.name)
def test_error_conformance(case: ErrorCase) -> None:
    assert run_error_case(case) == []


@pytest.mark.parametrize("case", USAGE_CASES, ids=lambda c: c.name)
def test_usage_conformance(case: UsageCase) -> None:
    assert run_usage_case(case) == []


@pytest.mark.parametrize("case", BROKEN_PIPE_CASES, ids=lambda c: c.name)
def test_broken_pipe_conformance(case: BrokenPipeCase) -> None:
    assert run_broken_pipe_case(case) == []
