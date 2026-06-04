"""Integration tests for git-history reconciliation (DB-backed).

Exercises BreadcrumbModel.reconcile_with_git end to end: query open breadcrumbs,
scan a real git repo, and (with apply) transition + record provenance. Needs the
ephemeral Postgres fixture and git on PATH.
"""

from __future__ import annotations

import subprocess

import pytest

from agent_notes.core import db as coredb
from agent_notes.core.breadcrumbs_model import BreadcrumbModel
from tests.conftest import ephemeral_db  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _vec():
    return [0.0] * 768


@pytest.fixture
def git_project(tmp_path):
    """A registered project whose repo_root is a fresh git repo we control.

    Slug is derived from the unique tmp dir so the session-scoped DB doesn't
    alias projects across tests (get_or_create keeps the first repo_root).
    """
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.test")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "commit", "--allow-empty", "-q", "-m", "initial")
    ws = coredb.get_or_create_workspace("default", "Default Workspace")
    proj = coredb.get_or_create_project(
        ws.id, slug=f"rec-{tmp_path.name}", name="reconcile-test", repo_root=str(tmp_path)
    )
    return proj, tmp_path


def test_reconcile_dry_run_suggests_without_mutating(git_project):
    proj, repo = git_project
    BreadcrumbModel.file_breadcrumb(
        project_id=proj.id, identifier="BC-001", title="x", body="",
        kind="bug", status="new", severity="high", embedding=_vec(),
    )
    _git(repo, "commit", "--allow-empty", "-q", "-m", "resolve BC-001: done")

    res = BreadcrumbModel.reconcile_with_git(proj.id, str(repo), apply=False)

    assert len(res) == 1
    assert res[0]["identifier"] == "BC-001"
    assert res[0]["applied"] is False
    # Dry run must not touch the DB.
    bc = BreadcrumbModel.get_breadcrumb(proj.id, "BC-001")
    assert bc["status"] == "new"
    assert bc["closed_at"] is None


def test_reconcile_apply_resolves_and_records_provenance(git_project):
    proj, repo = git_project
    BreadcrumbModel.file_breadcrumb(
        project_id=proj.id, identifier="BC-002", title="y", body="",
        kind="bug", status="new", severity="medium", embedding=_vec(),
    )
    _git(repo, "commit", "--allow-empty", "-q", "-m", "fix BC-002 properly")

    res = BreadcrumbModel.reconcile_with_git(proj.id, str(repo), apply=True)

    assert res[0]["applied"] is True
    bc = BreadcrumbModel.get_breadcrumb(proj.id, "BC-002")
    assert bc["status"] == "resolved"
    assert bc["closed_at"] is not None  # the status trigger closed it
    refs = bc["external_refs"]
    assert refs["resolved_by_commit"]  # provenance survives the transition
    assert "BC-002" in refs["resolved_by_subject"]


def test_reconcile_leaves_genuinely_open_bc_untouched(git_project):
    proj, repo = git_project
    BreadcrumbModel.file_breadcrumb(
        project_id=proj.id, identifier="BC-003", title="z", body="",
        kind="bug", status="new", severity="low", embedding=_vec(),
    )
    _git(repo, "commit", "--allow-empty", "-q", "-m", "working on BC-003 (not done)")

    assert BreadcrumbModel.reconcile_with_git(proj.id, str(repo), apply=True) == []
    assert BreadcrumbModel.get_breadcrumb(proj.id, "BC-003")["status"] == "new"
