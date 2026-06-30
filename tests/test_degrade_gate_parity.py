"""Plan 014 — degrade-mode gate parity (Option A(b)).

The native (no-regista / degrade) write path used to write a terminal ``close``
op directly, while the regista path routes ``close → submit_for_review →
in_review`` and cannot complete work without the cross-lineage review gate. That
behavior drift meant a single verb (`close`) produced divergent terminal
outcomes by path. These tests pin the invariant both paths now share: **neither
can complete (reach ``done``) work unilaterally**; native ``close`` defers to
``in_review`` too, and only ``force=True`` writes a terminal op.
"""

from __future__ import annotations

import pytest

from agent_notes.core import db as coredb
from agent_notes.core.face_factory import reset_face
from agent_notes.core.work_item_model import WorkItemModel
from tests.conftest import ephemeral_db  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")


@pytest.fixture
def default_project():
    ws = coredb.get_or_create_workspace("default", "Default Workspace")
    return coredb.get_or_create_project(
        ws.id, slug="sf2", name="sf2", repo_root="/projects/sf2",
    )


def _vec768():
    return [0.0] * 768


def _native(monkeypatch):
    """Force the native (degrade) path: no regista face."""
    monkeypatch.delenv("AGENT_NOTES_REGISTA_WRITES", raising=False)
    reset_face()


class TestDegradeModeClose:
    def test_native_close_defers_to_in_review_not_terminal(self, default_project, monkeypatch):
        _native(monkeypatch)
        WorkItemModel.file_work_item(
            project_id=default_project.id, identifier="DG-01",
            title="defer me", status="open", embedding=_vec768(),
        )
        closed = WorkItemModel.close_work_item(default_project.id, "DG-01")
        # Defers to in_review — NOT terminal, the old bug.
        assert closed["status"] == "in_review"
        assert closed["status"] not in ("done", "closed")
        assert closed.get("closed_at") is None

    def test_native_close_from_in_progress_defers(self, default_project, monkeypatch):
        _native(monkeypatch)
        WorkItemModel.file_work_item(
            project_id=default_project.id, identifier="DG-02",
            title="x", status="open", embedding=_vec768(),
        )
        WorkItemModel.update_work_item(
            project_id=default_project.id, identifier="DG-02", status="in_progress",
        )
        closed = WorkItemModel.close_work_item(default_project.id, "DG-02")
        assert closed["status"] == "in_review"

    def test_native_close_already_in_review_is_noop(self, default_project, monkeypatch):
        _native(monkeypatch)
        WorkItemModel.file_work_item(
            project_id=default_project.id, identifier="DG-03",
            title="x", status="open", embedding=_vec768(),
        )
        WorkItemModel.close_work_item(default_project.id, "DG-03")  # -> in_review
        again = WorkItemModel.close_work_item(default_project.id, "DG-03")
        assert again["status"] == "in_review"

    def test_native_accept_to_done_is_blocked_without_force(self, default_project, monkeypatch):
        # WI-2: degrade mode cannot run the gate, so `accept` (in_human_review →
        # done) must be refused — it would fake the cross-lineage requirement.
        _native(monkeypatch)
        WorkItemModel.file_work_item(
            project_id=default_project.id, identifier="DG-04",
            title="x", status="open", embedding=_vec768(),
        )
        for st in ("in_progress", "in_review", "in_human_review"):
            WorkItemModel.update_work_item(
                project_id=default_project.id, identifier="DG-04", status=st,
            )
        with pytest.raises(ValueError, match="review gate"):
            WorkItemModel.update_work_item(
                project_id=default_project.id, identifier="DG-04", status="done",
            )

    def test_native_force_close_writes_terminal(self, default_project, monkeypatch):
        # The admin/repair escape hatch keeps the legacy terminal close.
        _native(monkeypatch)
        WorkItemModel.file_work_item(
            project_id=default_project.id, identifier="DG-05",
            title="x", status="open", embedding=_vec768(),
        )
        out = WorkItemModel.close_work_item(default_project.id, "DG-05", force=True)
        assert out["status"] in ("done", "closed")

    def test_close_from_open_dismissal_still_allowed_native(self, default_project, monkeypatch):
        # The review-exempt won't-fix/duplicate dismissal (open → done) stays
        # reachable, matching the regista path — it declines work, not completes it.
        _native(monkeypatch)
        WorkItemModel.file_work_item(
            project_id=default_project.id, identifier="DG-06",
            title="x", status="open", embedding=_vec768(),
        )
        out = WorkItemModel.update_work_item(
            project_id=default_project.id, identifier="DG-06", status="done",
        )
        assert out["status"] == "done"
