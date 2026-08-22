"""Tests for the v6 review-gate transition API (review_transition + CLI).

v6 identity: the reviewer IS the running producer. There is deliberately no
per-call ``actor_id`` / ``model_lineage`` override — tests move the *process*
environment (``AGENT_NOTES_ACTOR_ID`` + the ``REGISTA_PRODUCER_*`` block),
exactly as a real reviewer host would, and every lineage a test signs is
paired with a model from the same family (see ``conftest._set_truthful_producer``).

Covers both the native (degrade) and regista paths:

- Native: adversarial_pass / reject / request_changes drive the status
  transition and stamp the review_note into diagnostic_keys.review_notes.
  accept is blocked (Plan 014 WI-2 — the gate cannot run off-regista).
- Regista: review_transition threads only the review_note into the transition
  payload; regista's signed producer is the reviewer-lineage authority, and a
  producer with no model or canonical lineage is refused before any write.
- CLI: review list / pass / accept / reject / request-changes.
"""

from __future__ import annotations

import argparse
import inspect
import json
from pathlib import Path

import pytest
from regista import RegistaError

from agent_notes.core.actor import Actor, ActorConfigurationError
from agent_notes.core.face_factory import reset_face, set_face_for_test, write_actor
from agent_notes.core.producer import ProducerConfigurationError
from agent_notes.core.regista_face import RegistaFace
from agent_notes.core.work_item_model import WorkItemModel
from tests.conftest import (
    _set_truthful_producer,
    ephemeral_db,  # noqa: F401
    provision_v6_regista,
)

pytestmark = pytest.mark.usefixtures("ephemeral_db")


@pytest.fixture
def default_project():
    from agent_notes.core import db as coredb

    ws = coredb.get_or_create_workspace("default", "Default Workspace")
    return coredb.get_or_create_project(
        ws.id,
        slug="sf2",
        name="sf2",
        repo_root="/projects/sf2",
    )


@pytest.fixture
def v6_key_path(tmp_path: Path) -> str:
    """Path for the throwaway v6 keyset (provision writes the real file)."""

    return str(tmp_path / "v6_keys.json")


def _vec768():
    return [0.0] * 768


def _native(monkeypatch):
    monkeypatch.delenv("AGENT_NOTES_REGISTA_WRITES", raising=False)
    monkeypatch.delenv("REGISTA_DSN", raising=False)
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


def _author_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "agent:author-agent")
    _set_truthful_producer(monkeypatch, model="glm-5.3")


def _reviewer_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "agent:reviewer")
    _set_truthful_producer(monkeypatch, model="kimi-k2.5")


def _accepter_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "agent:accepter")
    _set_truthful_producer(monkeypatch, model="claude-opus-4.6")


def _file_to_in_review_regista(project_id, identifier) -> dict:
    """File + drive to in_review on the regista path (as the author)."""

    WorkItemModel.file_work_item(
        project_id=project_id,
        identifier=identifier,
        title="regista review",
        body="body",
        kind="bug",
        status="open",
        severity="medium",
        embedding=_vec768(),
    )
    WorkItemModel.update_work_item(
        project_id=project_id,
        identifier=identifier,
        status="in_progress",
    )
    WorkItemModel.close_work_item(project_id, identifier)
    wi_row = WorkItemModel.get_work_item(project_id, identifier)
    assert wi_row["status"] == "in_review"
    return wi_row


# ---------------------------------------------------------------------------
# v6 identity boundary (no override surfaces; the reviewer is the producer)
# ---------------------------------------------------------------------------


def test_review_transition_has_no_identity_override_parameters():
    parameters = inspect.signature(WorkItemModel.review_transition).parameters

    assert "actor_id" not in parameters
    assert "model_lineage" not in parameters
    assert "same_lineage_acknowledged" in parameters


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
        assert notes[0]["actor_id"] == "agent:worker"

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

    def test_accept_blocked_in_degrade_mode(self, default_project, monkeypatch):
        _to_in_review(default_project.id, "RV-05", monkeypatch)
        WorkItemModel.review_transition(
            default_project.id,
            "RV-05",
            transition_name="adversarial_pass",
            review_note="pass",
        )
        with pytest.raises(ValueError, match="review gate"):
            WorkItemModel.review_transition(
                default_project.id,
                "RV-05",
                transition_name="accept",
                review_note="accepting",
            )

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


class TestRegistaReviewTransition:
    def test_review_pass_threads_note_and_signed_producer_into_committed_event(
        self, default_project, v6_key_path, monkeypatch
    ):
        """The committed event carries the note and signed producer lineage."""
        monkeypatch.setenv("AGENT_NOTES_REGISTA_WRITES", "1")

        reg = provision_v6_regista(v6_key_path)
        face = RegistaFace(reg)
        reset_face()
        set_face_for_test(face)

        try:
            _author_env(monkeypatch)
            wi = _file_to_in_review_regista(default_project.id, "RV-REG-01")

            _reviewer_env(monkeypatch)
            result = WorkItemModel.review_transition(
                default_project.id,
                "RV-REG-01",
                transition_name="adversarial_pass",
                review_note="Cross-lineage pass — looks good.",
            )
            assert result["status"] == "in_human_review"

            events = face.history(wi["regista_work_item_id"])
            pass_events = [
                e for e in events if getattr(e, "transition", None) == "adversarial_pass"
            ]
            assert len(pass_events) == 1
            committed = pass_events[0]
            payload = committed.payload or {}
            assert payload.get("review_note") == "Cross-lineage pass — looks good."
            assert "reviewer_claims" not in payload
            envelope = json.loads(committed.canonical_envelope)
            assert envelope["producer"]["model_lineage"] == "kimi"
            assert envelope["producer"]["model"] == "kimi-k2.5"
            # The reviewer actor is the ambient principal, not a per-call one.
            assert committed.actor_id == "agent:reviewer"
        finally:
            reset_face()
            reg.close()

    def test_review_accept_reaches_done(self, default_project, v6_key_path, monkeypatch):
        monkeypatch.setenv("AGENT_NOTES_REGISTA_WRITES", "1")

        reg = provision_v6_regista(v6_key_path)
        face = RegistaFace(reg)
        reset_face()
        set_face_for_test(face)

        try:
            _author_env(monkeypatch)
            _file_to_in_review_regista(default_project.id, "RV-REG-02")

            _reviewer_env(monkeypatch)
            WorkItemModel.review_transition(
                default_project.id,
                "RV-REG-02",
                transition_name="adversarial_pass",
                review_note="adversarial pass OK",
            )

            _accepter_env(monkeypatch)
            result = WorkItemModel.review_transition(
                default_project.id,
                "RV-REG-02",
                transition_name="accept",
                review_note="accepted — good work",
            )
            assert result["status"] == "done"

            row = WorkItemModel.get_work_item(default_project.id, "RV-REG-02")
            events = face.history(row["regista_work_item_id"])
            accept_events = [e for e in events if e.transition == "accept"]
            assert len(accept_events) == 1
            assert "reviewer_claims" not in (accept_events[0].payload or {})
        finally:
            reset_face()
            reg.close()

    def test_same_lineage_verdict_without_ack_is_rejected(
        self, default_project, v6_key_path, monkeypatch
    ):
        """A reviewer whose lineage matches an author's must acknowledge it."""
        monkeypatch.setenv("AGENT_NOTES_REGISTA_WRITES", "1")

        reg = provision_v6_regista(v6_key_path)
        face = RegistaFace(reg)
        reset_face()
        set_face_for_test(face)

        try:
            _author_env(monkeypatch)
            _file_to_in_review_regista(default_project.id, "RV-REG-SAME")

            # Same lineage (glm) reviewer, distinct actor, no acknowledgment.
            monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "agent:reviewer")
            _set_truthful_producer(monkeypatch, model="glm-5.3")
            with pytest.raises(RegistaError, match="same_lineage_acknowledged"):
                WorkItemModel.review_transition(
                    default_project.id,
                    "RV-REG-SAME",
                    transition_name="adversarial_pass",
                    review_note="same lineage, unacknowledged",
                )
            # The refused verdict left no committed pass event.
            row = WorkItemModel.get_work_item(default_project.id, "RV-REG-SAME")
            events = face.history(row["regista_work_item_id"])
            assert not [e for e in events if e.transition == "adversarial_pass"]
        finally:
            reset_face()
            reg.close()

    def test_same_lineage_acknowledged_passes_through(
        self, default_project, v6_key_path, monkeypatch
    ):
        monkeypatch.setenv("AGENT_NOTES_REGISTA_WRITES", "1")

        reg = provision_v6_regista(v6_key_path)
        face = RegistaFace(reg)
        reset_face()
        set_face_for_test(face)

        try:
            _author_env(monkeypatch)
            _file_to_in_review_regista(default_project.id, "RV-REG-03")

            monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "agent:reviewer")
            _set_truthful_producer(monkeypatch, model="glm-5.3")
            result = WorkItemModel.review_transition(
                default_project.id,
                "RV-REG-03",
                transition_name="adversarial_pass",
                review_note="same-lineage review acknowledged",
                same_lineage_acknowledged=True,
            )
            assert result["status"] == "in_human_review"

            row = WorkItemModel.get_work_item(default_project.id, "RV-REG-03")
            events = face.history(row["regista_work_item_id"])
            pass_events = [e for e in events if e.transition == "adversarial_pass"]
            assert len(pass_events) == 1
            assert (pass_events[0].payload or {}).get("same_lineage_acknowledged") is True
        finally:
            reset_face()
            reg.close()

    def test_review_refused_when_producer_declares_no_model(
        self, default_project, v6_key_path, monkeypatch
    ):
        """A no-model producer cannot sign a verdict — refusal happens in
        agent-notes, before any write, and commits nothing."""
        monkeypatch.setenv("AGENT_NOTES_REGISTA_WRITES", "1")

        reg = provision_v6_regista(v6_key_path)
        face = RegistaFace(reg)
        reset_face()
        set_face_for_test(face)

        try:
            _author_env(monkeypatch)
            wi = _file_to_in_review_regista(default_project.id, "RV-REG-NOMODEL")

            monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "agent:reviewer")
            # A harness identity but no model: a legitimate "no model"
            # producer, which must not cast review verdicts.
            monkeypatch.setenv("REGISTA_PRODUCER_HARNESS", "claude-code")
            monkeypatch.setenv("REGISTA_PRODUCER_HARNESS_VERSION", "test-harness/1")
            monkeypatch.delenv("REGISTA_PRODUCER_MODEL", raising=False)
            monkeypatch.delenv("REGISTA_PRODUCER_MODEL_LINEAGE", raising=False)

            with pytest.raises(ProducerConfigurationError, match="REGISTA_PRODUCER_MODEL"):
                WorkItemModel.review_transition(
                    default_project.id,
                    "RV-REG-NOMODEL",
                    transition_name="adversarial_pass",
                    review_note="cannot sign this",
                )
            # Refused before the write: no verdict event, state unchanged.
            events = face.history(wi["regista_work_item_id"])
            assert not [e for e in events if e.transition == "adversarial_pass"]
            assert face.get(wi["regista_work_item_id"]).current_state == "in_review"
        finally:
            reset_face()
            reg.close()

    def test_review_refused_when_producer_lineage_is_not_canonical(
        self, default_project, v6_key_path, monkeypatch
    ):
        monkeypatch.setenv("AGENT_NOTES_REGISTA_WRITES", "1")

        reg = provision_v6_regista(v6_key_path)
        reset_face()
        set_face_for_test(RegistaFace(reg))

        try:
            _author_env(monkeypatch)
            _file_to_in_review_regista(default_project.id, "RV-REG-BADLIN")

            monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "agent:reviewer")
            monkeypatch.setenv("REGISTA_PRODUCER_MODEL", "claude-fable-5")
            monkeypatch.setenv("REGISTA_PRODUCER_MODEL_LINEAGE", "not-a-family")

            with pytest.raises(ProducerConfigurationError):
                WorkItemModel.review_transition(
                    default_project.id,
                    "RV-REG-BADLIN",
                    transition_name="adversarial_pass",
                    review_note="also cannot sign this",
                )
        finally:
            reset_face()
            reg.close()


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

    def test_cli_review_accept_blocked_native(self, default_project, capsys, monkeypatch):
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
