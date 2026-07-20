"""Tests for the review-gate transition API (review_transition + CLI).

Covers both the native (degrade) and regista paths:

- Native: adversarial_pass / reject / request_changes drive the status
  transition and stamp the review_note into diagnostic_keys.review_notes.
  accept is blocked (Plan 014 WI-2 — the gate cannot run off-regista).
- Regista: review_transition threads the review_note into the transition
  payload so regista's cross-lineage validators accept it, and uses the
  reviewer_actor helper for identity overrides.
- CLI: review list / pass / accept / reject / request-changes.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
from regista.testing import InMemoryRegista

from agent_notes.core import db as coredb
from agent_notes.core.face_factory import reset_face, set_face_for_test
from agent_notes.core.regista_face import RegistaFace
from agent_notes.core.work_item_model import WorkItemModel
from tests.conftest import ephemeral_db  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")


@pytest.fixture
def default_project():
    ws = coredb.get_or_create_workspace("default", "Default Workspace")
    return coredb.get_or_create_project(
        ws.id,
        slug="sf2",
        name="sf2",
        repo_root="/projects/sf2",
    )


@pytest.fixture
def hmac_key_path(tmp_path: Path):
    path = tmp_path / "keys.json"
    path.write_text(
        json.dumps(
            {
                "keys": [
                    {
                        "key_id": "test-key-001",
                        "secret": "dGhpcyBpcyBhIHRlc3Qgc2VjcmV0IGtleSBmb3Igc3Vic3RyYXRl",
                        "status": "active",
                    }
                ]
            }
        )
    )
    return str(path)


def _vec768():
    return [0.0] * 768


def _native(monkeypatch):
    monkeypatch.delenv("AGENT_NOTES_REGISTA_WRITES", raising=False)
    reset_face()


def _to_in_review(project_id, identifier, monkeypatch):
    """Drive a work item to in_review on the native path."""
    _native(monkeypatch)
    WorkItemModel.file_work_item(
        project_id=project_id,
        identifier=identifier,
        title="test item",
        status="open",
        embedding=_vec768(),
    )
    WorkItemModel.update_work_item(
        project_id=project_id,
        identifier=identifier,
        status="in_progress",
    )
    WorkItemModel.close_work_item(project_id, identifier)


# ---------------------------------------------------------------------------
# Native path — review_transition
# ---------------------------------------------------------------------------


class TestNativeReviewTransition:
    def test_adversarial_pass_moves_to_in_human_review(self, default_project, monkeypatch):
        _to_in_review(default_project.id, "RV-01", monkeypatch)
        result = WorkItemModel.review_transition(
            default_project.id,
            "RV-01",
            transition_name="adversarial_pass",
            review_note="Looks correct — tested edge cases.",
            actor_id="reviewer-kimi",
            model_lineage="kimi",
        )
        assert result["status"] == "in_human_review"

    def test_review_note_stored_in_diagnostic_keys(self, default_project, monkeypatch):
        _to_in_review(default_project.id, "RV-02", monkeypatch)
        result = WorkItemModel.review_transition(
            default_project.id,
            "RV-02",
            transition_name="adversarial_pass",
            review_note="Solid work.",
            actor_id="reviewer-glm",
            model_lineage="glm",
        )
        diag = result.get("diagnostic_keys") or {}
        notes = diag.get("review_notes") or []
        assert len(notes) == 1
        assert notes[0]["note"] == "Solid work."
        assert notes[0]["transition"] == "adversarial_pass"
        assert notes[0]["actor_id"] == "reviewer-glm"
        assert notes[0]["model_lineage"] == "glm"

    def test_reject_moves_back_to_in_progress(self, default_project, monkeypatch):
        _to_in_review(default_project.id, "RV-03", monkeypatch)
        WorkItemModel.review_transition(
            default_project.id,
            "RV-03",
            transition_name="adversarial_pass",
            review_note="pass",
            actor_id="r1",
            model_lineage="kimi",
        )
        result = WorkItemModel.review_transition(
            default_project.id,
            "RV-03",
            transition_name="reject",
            review_note="Fundamental flaw in the approach.",
            actor_id="r2",
            model_lineage="opus",
        )
        assert result["status"] == "in_progress"

    def test_request_changes_moves_back_to_in_progress(self, default_project, monkeypatch):
        _to_in_review(default_project.id, "RV-04", monkeypatch)
        result = WorkItemModel.review_transition(
            default_project.id,
            "RV-04",
            transition_name="request_changes",
            review_note="Need more tests for edge case X.",
            actor_id="reviewer-glm",
            model_lineage="glm",
        )
        assert result["status"] == "in_progress"

    def test_accept_blocked_in_degrade_mode(self, default_project, monkeypatch):
        _to_in_review(default_project.id, "RV-05", monkeypatch)
        WorkItemModel.review_transition(
            default_project.id,
            "RV-05",
            transition_name="adversarial_pass",
            review_note="pass",
            actor_id="r1",
            model_lineage="kimi",
        )
        with pytest.raises(ValueError, match="review gate"):
            WorkItemModel.review_transition(
                default_project.id,
                "RV-05",
                transition_name="accept",
                review_note="accepting",
                actor_id="r2",
                model_lineage="opus",
            )

    def test_empty_review_note_raises(self, default_project, monkeypatch):
        _to_in_review(default_project.id, "RV-06", monkeypatch)
        with pytest.raises(ValueError, match="review_note is required"):
            WorkItemModel.review_transition(
                default_project.id,
                "RV-06",
                transition_name="adversarial_pass",
                review_note="",
                actor_id="r1",
                model_lineage="kimi",
            )

    def test_unknown_transition_raises(self, default_project, monkeypatch):
        _to_in_review(default_project.id, "RV-07", monkeypatch)
        with pytest.raises(ValueError, match="Unknown review transition"):
            WorkItemModel.review_transition(
                default_project.id,
                "RV-07",
                transition_name="bogus",
                review_note="x",
                actor_id="r1",
                model_lineage="kimi",
            )

    def test_multiple_review_notes_accumulate(self, default_project, monkeypatch):
        _to_in_review(default_project.id, "RV-08", monkeypatch)
        WorkItemModel.review_transition(
            default_project.id,
            "RV-08",
            transition_name="adversarial_pass",
            review_note="First pass.",
            actor_id="r1",
            model_lineage="kimi",
        )
        result = WorkItemModel.review_transition(
            default_project.id,
            "RV-08",
            transition_name="reject",
            review_note="On second thought, no.",
            actor_id="r2",
            model_lineage="opus",
        )
        diag = result.get("diagnostic_keys") or {}
        notes = diag.get("review_notes") or []
        assert len(notes) == 2
        assert notes[0]["note"] == "First pass."
        assert notes[1]["note"] == "On second thought, no."


# ---------------------------------------------------------------------------
# Regista path — review_transition threads review_note into payload
# ---------------------------------------------------------------------------


class TestRegistaReviewTransition:
    def test_review_pass_threads_note_into_payload(
        self, default_project, hmac_key_path, monkeypatch
    ):
        monkeypatch.setenv("AGENT_NOTES_REGISTA_WRITES", "1")
        monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "author-agent")
        monkeypatch.setenv("AGENT_NOTES_PRINCIPAL_ID", "author@example.com")
        monkeypatch.setenv("AGENT_NOTES_MODEL_LINEAGE", "glm")

        reg = InMemoryRegista(hmac_key_path=hmac_key_path)
        face = RegistaFace(reg)
        reset_face()
        set_face_for_test(face)

        try:
            wi = WorkItemModel.file_work_item(
                project_id=default_project.id,
                identifier="RV-REG-01",
                title="regista review",
                body="body",
                kind="bug",
                status="open",
                severity="medium",
                embedding=_vec768(),
            )
            WorkItemModel.update_work_item(
                project_id=default_project.id,
                identifier="RV-REG-01",
                status="in_progress",
            )
            WorkItemModel.close_work_item(default_project.id, "RV-REG-01")
            wi_row = WorkItemModel.get_work_item(default_project.id, "RV-REG-01")
            assert wi_row["status"] == "in_review"

            result = WorkItemModel.review_transition(
                default_project.id,
                "RV-REG-01",
                transition_name="adversarial_pass",
                review_note="Cross-lineage pass — looks good.",
                actor_id="reviewer-kimi",
                model_lineage="kimi",
            )
            assert result["status"] == "in_human_review"

            regista_id = wi["regista_work_item_id"]
            events = face.history(regista_id)
            pass_events = [
                e for e in events if getattr(e, "transition", None) == "adversarial_pass"
            ]
            assert len(pass_events) == 1
            payload = getattr(pass_events[0], "payload", None) or {}
            assert payload.get("review_note") == "Cross-lineage pass — looks good."
        finally:
            reset_face()
            reg.close()

    def test_review_accept_reaches_done(self, default_project, hmac_key_path, monkeypatch):
        monkeypatch.setenv("AGENT_NOTES_REGISTA_WRITES", "1")
        monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "author-agent")
        monkeypatch.setenv("AGENT_NOTES_PRINCIPAL_ID", "author@example.com")
        monkeypatch.setenv("AGENT_NOTES_MODEL_LINEAGE", "glm")

        reg = InMemoryRegista(hmac_key_path=hmac_key_path)
        face = RegistaFace(reg)
        reset_face()
        set_face_for_test(face)

        try:
            WorkItemModel.file_work_item(
                project_id=default_project.id,
                identifier="RV-REG-02",
                title="accept test",
                body="body",
                kind="bug",
                status="open",
                severity="medium",
                embedding=_vec768(),
            )
            WorkItemModel.update_work_item(
                project_id=default_project.id,
                identifier="RV-REG-02",
                status="in_progress",
            )
            WorkItemModel.close_work_item(default_project.id, "RV-REG-02")

            WorkItemModel.review_transition(
                default_project.id,
                "RV-REG-02",
                transition_name="adversarial_pass",
                review_note="adversarial pass OK",
                actor_id="reviewer-kimi",
                model_lineage="kimi",
            )
            result = WorkItemModel.review_transition(
                default_project.id,
                "RV-REG-02",
                transition_name="accept",
                review_note="accepted — good work",
                actor_id="accepter-opus",
                model_lineage="opus",
            )
            assert result["status"] == "done"
        finally:
            reset_face()
            reg.close()

    def test_same_lineage_acknowledged_passes_through(
        self, default_project, hmac_key_path, monkeypatch
    ):
        monkeypatch.setenv("AGENT_NOTES_REGISTA_WRITES", "1")
        monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "author-agent")
        monkeypatch.setenv("AGENT_NOTES_PRINCIPAL_ID", "author@example.com")
        monkeypatch.setenv("AGENT_NOTES_MODEL_LINEAGE", "glm")

        reg = InMemoryRegista(hmac_key_path=hmac_key_path)
        face = RegistaFace(reg)
        reset_face()
        set_face_for_test(face)

        try:
            WorkItemModel.file_work_item(
                project_id=default_project.id,
                identifier="RV-REG-03",
                title="same lineage",
                body="body",
                kind="bug",
                status="open",
                severity="medium",
                embedding=_vec768(),
            )
            WorkItemModel.update_work_item(
                project_id=default_project.id,
                identifier="RV-REG-03",
                status="in_progress",
            )
            WorkItemModel.close_work_item(default_project.id, "RV-REG-03")

            result = WorkItemModel.review_transition(
                default_project.id,
                "RV-REG-03",
                transition_name="adversarial_pass",
                review_note="same-lineage review acknowledged",
                actor_id="reviewer-glm-2",
                model_lineage="glm",
                same_lineage_acknowledged=True,
            )
            assert result["status"] == "in_human_review"
        finally:
            reset_face()
            reg.close()


# ---------------------------------------------------------------------------
# CLI — review list / pass / accept / reject / request-changes
# ---------------------------------------------------------------------------


class TestCliReview:
    def test_cli_review_list(self, default_project, capsys, monkeypatch):
        _native(monkeypatch)
        _to_in_review(default_project.id, "RV-CLI-01", monkeypatch)

        from agent_notes.cli.work_items import cmd_wi_review_list

        ns = argparse.Namespace(
            workspace=None,
            project=None,
            path="/projects/sf2",
            json=True,
        )
        assert cmd_wi_review_list(ns) == 0
        data = json.loads(capsys.readouterr().out)
        ids = {r["identifier"] for r in data["review_queue"]}
        assert "RV-CLI-01" in ids

    def test_cli_review_list_empty(self, default_project, capsys, monkeypatch):
        _native(monkeypatch)
        WorkItemModel.file_work_item(
            project_id=default_project.id,
            identifier="RV-CLI-EMPTY",
            title="open",
            status="open",
            embedding=_vec768(),
        )

        from agent_notes.cli.work_items import cmd_wi_review_list

        ns = argparse.Namespace(
            workspace=None,
            project=None,
            path="/projects/sf2",
            json=True,
        )
        assert cmd_wi_review_list(ns) == 0
        data = json.loads(capsys.readouterr().out)
        ids = {r["identifier"] for r in data["review_queue"]}
        assert "RV-CLI-EMPTY" not in ids

    def test_cli_review_pass(self, default_project, capsys, monkeypatch):
        _to_in_review(default_project.id, "RV-CLI-02", monkeypatch)

        from agent_notes.cli.work_items import cmd_wi_review_pass

        ns = argparse.Namespace(
            workspace=None,
            project=None,
            path="/projects/sf2",
            json=True,
            identifier="RV-CLI-02",
            note="CLI adversarial pass",
            actor_id="reviewer-kimi",
            model_lineage="kimi",
            same_lineage_acknowledged=False,
        )
        assert cmd_wi_review_pass(ns) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["work_item"]["status"] == "in_human_review"

    def test_cli_review_accept_blocked_native(self, default_project, capsys, monkeypatch):
        _to_in_review(default_project.id, "RV-CLI-03", monkeypatch)
        WorkItemModel.review_transition(
            default_project.id,
            "RV-CLI-03",
            transition_name="adversarial_pass",
            review_note="pass",
            actor_id="r1",
            model_lineage="kimi",
        )

        from agent_notes.cli.work_items import cmd_wi_review_accept

        ns = argparse.Namespace(
            workspace=None,
            project=None,
            path="/projects/sf2",
            json=True,
            identifier="RV-CLI-03",
            note="accepting",
            actor_id="r2",
            model_lineage="opus",
            same_lineage_acknowledged=False,
        )
        assert cmd_wi_review_accept(ns) != 0
        data = json.loads(capsys.readouterr().out)
        assert "review gate" in data["error"]["message"]

    def test_cli_review_reject(self, default_project, capsys, monkeypatch):
        _to_in_review(default_project.id, "RV-CLI-04", monkeypatch)
        WorkItemModel.review_transition(
            default_project.id,
            "RV-CLI-04",
            transition_name="adversarial_pass",
            review_note="pass",
            actor_id="r1",
            model_lineage="kimi",
        )

        from agent_notes.cli.work_items import cmd_wi_review_reject

        ns = argparse.Namespace(
            workspace=None,
            project=None,
            path="/projects/sf2",
            json=True,
            identifier="RV-CLI-04",
            note="rejecting — fundamental flaw",
            actor_id="r2",
            model_lineage="opus",
            same_lineage_acknowledged=False,
        )
        assert cmd_wi_review_reject(ns) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["work_item"]["status"] == "in_progress"

    def test_cli_review_request_changes(self, default_project, capsys, monkeypatch):
        _to_in_review(default_project.id, "RV-CLI-05", monkeypatch)

        from agent_notes.cli.work_items import cmd_wi_review_request_changes

        ns = argparse.Namespace(
            workspace=None,
            project=None,
            path="/projects/sf2",
            json=True,
            identifier="RV-CLI-05",
            note="need more tests",
            actor_id="reviewer-glm",
            model_lineage="glm",
            same_lineage_acknowledged=False,
        )
        assert cmd_wi_review_request_changes(ns) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["work_item"]["status"] == "in_progress"
