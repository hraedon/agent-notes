"""Tests for cwd-based project discovery.

Discovery is a fallback layered on ``db.resolve_project``; these tests stub
that function so they stay hermetic (no registry, no container). The point
being pinned is the *precedence and failure behaviour*, which is where a
convenience feature can do real damage: discovery must never override an
explicit selector, and an unregistered cwd must stay unresolved rather than
being guessed at.
"""

from __future__ import annotations

import pytest

from agent_notes.core import project_discovery
from agent_notes.core.project_discovery import DiscoveredProject, discover_project

RESOLUTION = {
    "workspace": "hraedon",
    "project": "vitrine",
    "repo_root": "/projects/vitrine",
    "log_location": None,
    "wake_channel": None,
    "resolved_via": "exact",
}


@pytest.fixture
def stub_resolve(monkeypatch):
    """Replace db.resolve_project with a recording stub."""

    def install(result):
        calls: list[str] = []

        def fake(path: str):
            calls.append(path)
            if isinstance(result, Exception):
                raise result
            return result

        import agent_notes.core.db as coredb

        monkeypatch.setattr(coredb, "resolve_project", fake)
        return calls

    return install


# ---------------------------------------------------------------------------
# discover_project
# ---------------------------------------------------------------------------


def test_discovers_the_project_containing_the_cwd(stub_resolve, monkeypatch, tmp_path):
    calls = stub_resolve(RESOLUTION)
    monkeypatch.chdir(tmp_path)

    found = discover_project()

    assert found == DiscoveredProject(
        workspace="hraedon",
        project="vitrine",
        repo_root="/projects/vitrine",
        resolved_via="exact",
    )
    assert calls == [str(tmp_path)], "discovery did not consult the cwd"


def test_explicit_start_path_wins_over_cwd(stub_resolve, monkeypatch, tmp_path):
    calls = stub_resolve(RESOLUTION)
    monkeypatch.chdir(tmp_path)

    discover_project("/somewhere/else")

    assert calls == ["/somewhere/else"]


def test_unregistered_directory_stays_unresolved(stub_resolve):
    """Guessing here is how a breadcrumb lands on the wrong project."""
    stub_resolve(ValueError("PROJECT_NOT_REGISTERED: no project found"))

    assert discover_project() is None


def test_unreachable_registry_does_not_raise(stub_resolve):
    """Discovery is a convenience; it must not turn a working command into a crash."""
    stub_resolve(RuntimeError("connection refused"))

    assert discover_project() is None


def test_ancestor_resolution_is_reported_as_such(stub_resolve):
    stub_resolve({**RESOLUTION, "resolved_via": "ancestor"})

    found = discover_project()

    assert found is not None
    assert found.resolved_via == "ancestor"


def test_missing_resolved_via_defaults_to_ancestor(stub_resolve):
    """The weaker claim is the safe default when the registry omits the field."""
    payload = {k: v for k, v in RESOLUTION.items() if k != "resolved_via"}
    stub_resolve(payload)

    found = discover_project()

    assert found is not None
    assert found.resolved_via == "ancestor"


def test_to_dict_round_trips(stub_resolve):
    stub_resolve(RESOLUTION)

    found = discover_project()

    assert found is not None
    assert found.to_dict() == {
        "workspace": "hraedon",
        "project": "vitrine",
        "repo_root": "/projects/vitrine",
        "resolved_via": "exact",
    }


# ---------------------------------------------------------------------------
# CLI resolution precedence
# ---------------------------------------------------------------------------


def _stub_registry(monkeypatch, *, workspaces, projects):
    import agent_notes.core.db as coredb

    monkeypatch.setattr(coredb, "list_workspaces", lambda: workspaces)
    monkeypatch.setattr(coredb, "list_projects", lambda workspace_id=None: projects)


class _Row:
    def __init__(self, id: int, slug: str) -> None:
        self.id = id
        self.slug = slug


def test_cli_falls_back_to_discovery_when_nothing_is_specified(monkeypatch):
    from agent_notes.cli.common import _resolve_impl

    _stub_registry(
        monkeypatch,
        workspaces=[_Row(1, "hraedon")],
        projects=[_Row(7, "vitrine")],
    )
    monkeypatch.setattr(
        project_discovery,
        "discover_project",
        lambda start=None: DiscoveredProject("hraedon", "vitrine", "/projects/vitrine", "exact"),
    )

    assert _resolve_impl(None, None, None) == (1, 7, "hraedon", "vitrine")


def test_cli_still_fails_when_the_cwd_is_unregistered(monkeypatch):
    """The additive fallback must not invent a project."""
    from agent_notes.cli.common import _resolve_impl

    _stub_registry(monkeypatch, workspaces=[], projects=[])
    monkeypatch.setattr(project_discovery, "discover_project", lambda start=None: None)

    with pytest.raises(SystemExit):
        _resolve_impl(None, None, None)


def test_discovery_is_not_consulted_when_a_path_is_given(monkeypatch, stub_resolve):
    """An explicit selector must never be second-guessed."""
    from agent_notes.cli.common import _resolve_impl

    stub_resolve(RESOLUTION)
    _stub_registry(
        monkeypatch,
        workspaces=[_Row(1, "hraedon")],
        projects=[_Row(7, "vitrine")],
    )
    called = False

    def spy(start=None):
        nonlocal called
        called = True
        return None

    monkeypatch.setattr(project_discovery, "discover_project", spy)

    _resolve_impl(None, None, "/projects/vitrine")

    assert called is False
