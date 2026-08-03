"""A second checkout must not steal a registered project's repo_root.

Project slugs default to a directory basename, so two checkouts of the same
repo — a /tmp scratch clone, a git worktree, a CI checkout — collide on slug.
Repointing on collision moves every work item and memory with it, because they
are keyed on project_id rather than on the path.

This happened live on 2026-08-03: usage-dashboard's repo_root was repointed to
a /tmp clone that was then cleaned up, so the real repo stopped resolving
(`PROJECT_NOT_REGISTERED`) and 25 work items sat behind a path that no longer
existed. The data was intact the whole time — only the mapping was wrong —
which is exactly why the failure was so confusing.
"""

from __future__ import annotations

import pytest

from agent_notes.core import db as coredb
from tests.conftest import ephemeral_db  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")


def _ws():
    return coredb.get_or_create_workspace("collide-ws", "Collide WS")


def test_second_checkout_does_not_steal_repo_root():
    ws = _ws()
    first = coredb.get_or_create_project(
        ws.id, slug="widget", name="widget", repo_root="/projects/widget"
    )

    # A scratch clone of the same repo, same basename -> same slug.
    second = coredb.get_or_create_project(
        ws.id, slug="widget", name="widget", repo_root="/tmp/widget"
    )

    assert second.id == first.id, "must be the same project row"
    assert second.repo_root == "/projects/widget", "the real checkout keeps the root"
    assert coredb.resolve_project("/projects/widget")["project"] == "widget"


def test_relocate_opts_in_to_repointing():
    ws = _ws()
    coredb.get_or_create_project(ws.id, slug="gadget", name="gadget", repo_root="/projects/gadget")

    moved = coredb.get_or_create_project(
        ws.id, slug="gadget", name="gadget", repo_root="/srv/gadget", relocate=True
    )

    assert moved.repo_root == "/srv/gadget"
    assert coredb.resolve_project("/srv/gadget")["project"] == "gadget"


def test_first_registration_still_sets_the_root():
    # Guard the fix's blind spot: flipping the COALESCE order must not stop a
    # brand-new project (or one registered with repo_root=None) from getting one.
    ws = _ws()
    coredb.get_or_create_project(ws.id, slug="doohickey", name="doohickey")
    later = coredb.get_or_create_project(
        ws.id, slug="doohickey", name="doohickey", repo_root="/projects/doohickey"
    )

    assert later.repo_root == "/projects/doohickey"


def test_reinit_at_the_same_path_is_idempotent():
    ws = _ws()
    coredb.get_or_create_project(ws.id, slug="thing", name="thing", repo_root="/projects/thing")
    again = coredb.get_or_create_project(
        ws.id, slug="thing", name="thing", repo_root="/projects/thing"
    )

    assert again.repo_root == "/projects/thing"
