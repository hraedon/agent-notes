"""CLI subprocess tests (Plan 004 Phase 9a, decision 59).

Tests the noun/verb argparse surface via subprocess.run against ephemeral Postgres.
Keeps existing model-layer tests intact; these test the CLI wiring.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from agent_notes.core import db as coredb

# ephemeral_db is session-scoped from conftest
from tests.conftest import ephemeral_db  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")

_CLI = [sys.executable, "-m", "agent_notes.cli"]


def _run(*args: str, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    """Run the CLI and return CompletedProcess."""
    merged_env = {**os.environ, **(env or {})}
    return subprocess.run(
        _CLI + list(args),
        capture_output=True,
        text=True,
        env=merged_env,
        check=check,
    )


@pytest.fixture
def default_project():
    ws = coredb.get_or_create_workspace("default", "Default Workspace")
    proj = coredb.get_or_create_project(ws.id, slug="sf2", name="sf2", repo_root="/projects/sf2")
    return proj


def test_help():
    result = _run("--help", check=False)
    assert result.returncode == 0
    assert "agent-notes" in result.stdout.lower()


def test_init_creates_project():
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "myrepo"
        repo.mkdir()
        (repo / ".git").mkdir()
        result = _run("init", str(repo), check=False)
        assert result.returncode == 0
        assert "registered" in result.stdout.lower()


def test_resolve_not_configured():
    with tempfile.TemporaryDirectory() as td:
        result = _run("resolve", "--path", td, "--json", check=False)
        assert result.returncode == 3  # EXIT_NOT_CONFIGURED
        data = json.loads(result.stdout)
        assert data["code"] == 3


def test_breadcrumb_file(default_project):
    result = _run(
        "breadcrumb",
        "file",
        "--workspace",
        "default",
        "--project",
        "sf2",
        "--title",
        "CLI test BC",
        "--kind",
        "bug",
        "--status",
        "new",
        "--json",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["breadcrumb"]["title"] == "CLI test BC"


def test_breadcrumb_get(default_project):
    from agent_notes.core.breadcrumbs_model import BreadcrumbModel

    BreadcrumbModel.file_breadcrumb(
        default_project.id,
        identifier="BC-CLI-001",
        title="Get me",
        kind="observation",
        status="open",
        embedding=[0.0] * 768,
    )
    result = _run(
        "breadcrumb",
        "get",
        "BC-CLI-001",
        "--workspace",
        "default",
        "--project",
        "sf2",
        "--json",
        check=False,
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data["breadcrumb"]["identifier"] == "BC-CLI-001"


def test_breadcrumb_find(default_project):
    from agent_notes.core.breadcrumbs_model import BreadcrumbModel

    BreadcrumbModel.file_breadcrumb(
        default_project.id,
        identifier="BC-CLI-002",
        title="Findable BC",
        kind="observation",
        status="open",
        embedding=[0.0] * 768,
    )
    result = _run(
        "breadcrumb",
        "find",
        "--workspace",
        "default",
        "--project",
        "sf2",
        "--status",
        "open",
        "--json",
        check=False,
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    identifiers = {r["identifier"] for r in data["breadcrumbs"]}
    assert "BC-CLI-002" in identifiers


def test_memory_add(default_project):
    result = _run(
        "memory",
        "add",
        "--workspace",
        "default",
        "--project",
        "sf2",
        "--name",
        "cli-memory-1",
        "--body",
        "A memory from the CLI.",
        "--type",
        "note",
        "--json",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["memory"]["name"] == "cli-memory-1"


def test_memory_get(default_project):
    from agent_notes.core.memory_model import add_memory

    ws = coredb.get_or_create_workspace("default", "Default Workspace")
    add_memory(
        workspace_id=ws.id,
        project_id=default_project.id,
        name="cli-memory-get",
        memory_type="note",
        body="Retrievable via CLI.",
        embedding=[0.0] * 768,
    )
    result = _run(
        "memory",
        "get",
        "cli-memory-get",
        "--workspace",
        "default",
        "--project",
        "sf2",
        "--json",
        check=False,
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data["memory"]["name"] == "cli-memory-get"


def test_changes_since(default_project):
    result = _run(
        "changes",
        "since",
        "2000-01-01T00:00:00",
        "--json",
        check=False,
    )
    # Should succeed even if empty
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert "changes" in data
