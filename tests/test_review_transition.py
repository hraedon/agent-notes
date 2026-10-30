"""Tests for the v6 review-gate transition API (review_transition + CLI).

v6 identity: the reviewer IS the running producer. There is deliberately no
per-call ``actor_id`` / ``model_lineage`` override — tests move the *process*
environment (``AGENT_NOTES_ACTOR_ID`` + the ``REGISTA_PRODUCER_*`` block),
exactly as a real reviewer host would, and every lineage a test signs is
paired with a model from the same family.

Covers the native op-log path (the only path since Plan 026 D1):

- adversarial_pass / accept / reject / request_changes drive the status
  transition and stamp the review_note into diagnostic_keys.review_notes.
  Reviewer independence itself is covered in ``test_review_independence.py``.
- CLI: review list / pass / accept / reject / request-changes.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os

import pytest

from agent_notes.core.actor import Actor, ActorConfigurationError, write_actor
from agent_notes.core.work_item_model import WorkItemModel
from tests.conftest import ephemeral_db  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")


@pytest.fixture
def default_project():
    from agent_notes.core import db as coredb

    ws = coredb.get_or_create_workspace("default", "Default Workspace")
    return coredb.get_or_create_project(
        ws.id,
        slug="sf2",
        name="sf2",
        repo_root=os.path.abspath("/projects/sf2"),
    )


def _vec768():
    return [0.0] * 768


def _native(monkeypatch):
    monkeypatch.delenv("AGENT_NOTES_REGISTA_WRITES", raising=False)
    monkeypatch.delenv("REGISTA_DSN", raising=False)


def _to_in_review(project_id, identifier, monkeypatch):
    """Drive a work item to in_review on the native path, then become a reviewer.

    The implementer and the reviewer are different actors from different model
    families, as the native gate requires (Plan 026 D2).
    """
    _native(monkeypatch)
    monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "agent:native-author")
    monkeypatch.setenv("AGENT_NOTES_MODEL", "glm-5.3")
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
    # Plan 025: native close completes the item; review is explicit.
    WorkItemModel.update_work_item(
        project_id=project_id,
        identifier=identifier,
        status="in_review",
    )
    monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "agent:native-reviewer")
    monkeypatch.setenv("AGENT_NOTES_MODEL", "kimi-k2.5")


# ---------------------------------------------------------------------------
# v6 identity boundary (no override surfaces; the reviewer is the producer)
# ---------------------------------------------------------------------------


def test_review_transition_has_no_identity_override_parameters():
    parameters = inspect.signature(WorkItemModel.review_transition).parameters

    assert "actor_id" not in parameters
    assert "model_lineage" not in parameters
    # Plan 026 D2.6/D2.7: a recorded override reason, and a delegated review
    # attempt whose identity comes from the attempt journal, not the caller.
    assert "acknowledgement" in parameters
    assert "attempt_id" in parameters


def test_write_actor_requires_canonical_ambient_identity(monkeypatch):
    monkeypatch.delenv("AGENT_NOTES_ACTOR_ID", raising=False)
    monkeypatch.delenv("REGISTA_PRINCIPAL_ID", raising=False)

    with pytest.raises(ActorConfigurationError):
        write_actor()


def test_work_item_model_rejects_identity_override_at_python_boundary():
    with pytest.raises(TypeError):
        WorkItemModel.review_transition(
            1,
            "WI-1",
            "adversarial_pass",
            "note",
            actor_id="agent:reviewer",
        )


def test_actor_metadata_uses_actor_kind_as_default_role():
    assert Actor(actor_id="human:operator", actor_kind="human").actor_metadata() == {
        "display_name": "human:operator",
        "role": "human",
    }


# ---------------------------------------------------------------------------
# Native path — review_transition (degrade mode)
# ---------------------------------------------------------------------------


class TestNativeReviewTransition:
    def test_adversarial_pass_moves_to_in_human_review(self, default_project, monkeypatch):
        _to_in_review(default_project.id, "RV-01", monkeypatch)
        result = WorkItemModel.review_transition(
            default_project.id,
            "RV-01",
            transition_name="adversarial_pass",
            review_note="Looks correct — tested edge cases.",
        )
        assert result["status"] == "in_human_review"

    def test_repeated_adversarial_pass_is_rejected_not_a_silent_noop(
        self, default_project, monkeypatch
    ):
        _to_in_review(default_project.id, "RV-NOOP", monkeypatch)
        WorkItemModel.review_transition(
            default_project.id,
            "RV-NOOP",
            transition_name="adversarial_pass",
            review_note="pass",
        )
        with pytest.raises(ValueError, match="already in"):
            WorkItemModel.review_transition(
                default_project.id,
                "RV-NOOP",
                transition_name="adversarial_pass",
                review_note="pass again",
            )

    def test_review_note_stored_in_diagnostic_keys(self, default_project, monkeypatch):
        _to_in_review(default_project.id, "RV-02", monkeypatch)
        result = WorkItemModel.review_transition(
            default_project.id,
            "RV-02",
            transition_name="adversarial_pass",
            review_note="Solid work.",
        )
        diag = result.get("diagnostic_keys") or {}
        notes = diag.get("review_notes") or []
        assert len(notes) == 1
        assert notes[0]["note"] == "Solid work."
        assert notes[0]["transition"] == "adversarial_pass"
        # v6: the note is attributed to the ambient actor (the running
        # reviewer), never to a per-call identity.
        assert notes[0]["actor_id"] == "agent:native-reviewer"

    def test_reject_moves_back_to_in_progress(self, default_project, monkeypatch):
        _to_in_review(default_project.id, "RV-03", monkeypatch)
        WorkItemModel.review_transition(
            default_project.id,
            "RV-03",
            transition_name="adversarial_pass",
            review_note="pass",
        )
        result = WorkItemModel.review_transition(
            default_project.id,
            "RV-03",
            transition_name="reject",
            review_note="Fundamental flaw in the approach.",
        )
        assert result["status"] == "in_progress"

    def test_request_changes_moves_back_to_in_progress(self, default_project, monkeypatch):
        _to_in_review(default_project.id, "RV-04", monkeypatch)
        result = WorkItemModel.review_transition(
            default_project.id,
            "RV-04",
            transition_name="request_changes",
            review_note="Need more tests for edge case X.",
        )
        assert result["status"] == "in_progress"

    def test_accept_completes_on_native_path(self, default_project, monkeypatch):
        # Plan 025 D7: native is the authority, so accept reaches done.
        _to_in_review(default_project.id, "RV-05", monkeypatch)
        WorkItemModel.review_transition(
            default_project.id,
            "RV-05",
            transition_name="adversarial_pass",
            review_note="pass",
        )
        result = WorkItemModel.review_transition(
            default_project.id,
            "RV-05",
            transition_name="accept",
            review_note="accepting",
        )
        assert result["status"] == "done"

    def test_empty_review_note_raises(self, default_project, monkeypatch):
        _to_in_review(default_project.id, "RV-06", monkeypatch)
        with pytest.raises(ValueError, match="review_note is required"):
            WorkItemModel.review_transition(
                default_project.id,
                "RV-06",
                transition_name="adversarial_pass",
                review_note="",
            )

    def test_unknown_transition_raises(self, default_project, monkeypatch):
        _to_in_review(default_project.id, "RV-07", monkeypatch)
        with pytest.raises(ValueError, match="Unknown review transition"):
            WorkItemModel.review_transition(
                default_project.id,
                "RV-07",
                transition_name="bogus",
                review_note="x",
            )

    def test_multiple_review_notes_accumulate(self, default_project, monkeypatch):
        _to_in_review(default_project.id, "RV-08", monkeypatch)
        WorkItemModel.review_transition(
            default_project.id,
            "RV-08",
            transition_name="adversarial_pass",
            review_note="First pass.",
        )
        result = WorkItemModel.review_transition(
            default_project.id,
            "RV-08",
            transition_name="reject",
            review_note="On second thought, no.",
        )
        diag = result.get("diagnostic_keys") or {}
        notes = diag.get("review_notes") or []
        assert len(notes) == 2
        assert notes[0]["note"] == "First pass."
        assert notes[1]["note"] == "On second thought, no."


# ---------------------------------------------------------------------------
# Regista path — review_transition (the gate runs)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CLI — review list / pass / accept / reject / request-changes
# ---------------------------------------------------------------------------


class TestReviewCLI:
    def test_cli_review_list(self, default_project, capsys, monkeypatch):
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
            same_lineage_acknowledged=False,
        )
        assert cmd_wi_review_pass(ns) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["work_item"]["status"] == "in_human_review"

    def test_cli_review_accept_native(self, default_project, capsys, monkeypatch):
        _to_in_review(default_project.id, "RV-CLI-03", monkeypatch)
        WorkItemModel.review_transition(
            default_project.id,
            "RV-CLI-03",
            transition_name="adversarial_pass",
            review_note="pass",
        )

        from agent_notes.cli.work_items import cmd_wi_review_accept

        ns = argparse.Namespace(
            workspace=None,
            project=None,
            path="/projects/sf2",
            json=True,
            identifier="RV-CLI-03",
            note="accepting",
            same_lineage_acknowledged=False,
        )
        assert cmd_wi_review_accept(ns) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["work_item"]["status"] == "done"

    def test_cli_review_reject(self, default_project, capsys, monkeypatch):
        _to_in_review(default_project.id, "RV-CLI-04", monkeypatch)
        WorkItemModel.review_transition(
            default_project.id,
            "RV-CLI-04",
            transition_name="adversarial_pass",
            review_note="pass",
        )

        from agent_notes.cli.work_items import cmd_wi_review_reject

        ns = argparse.Namespace(
            workspace=None,
            project=None,
            path="/projects/sf2",
            json=True,
            identifier="RV-CLI-04",
            note="rejecting — fundamental flaw",
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
            same_lineage_acknowledged=False,
        )
        assert cmd_wi_review_request_changes(ns) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["work_item"]["status"] == "in_progress"
