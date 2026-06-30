"""Plan 014 — degrade-mode gate parity (Option A(b)).

The native (no-regista / degrade) write path used to write a terminal ``close``
op directly, while the regista path routes ``close → submit_for_review →
in_review`` and cannot complete work without the cross-lineage review gate. That
behavior drift meant a single verb (`close`) produced divergent terminal
outcomes by path. These tests pin the invariant both paths now share: **neither
can complete (reach ``done``) work unilaterally**; native ``close`` defers to
``in_review`` too, and only ``force=True`` writes a terminal op.

WI-3 adds the degraded-completion detector in ``verify`` (flags terminal items
completed without a preceding ``adversarial_pass``), and WI-4 adds the
operator-invoked gate-waiver attestation that the detector recognizes.
"""

from __future__ import annotations

import pytest

from agent_notes.core import db as coredb
from agent_notes.core.face_factory import reset_face
from agent_notes.core.verifier import verify_gate_integrity
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


def _gate_violations(result):
    return [v for v in result.violations if v.rule == "gate"]


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


# ---------------------------------------------------------------------------
# WI-3 — degraded-completion detector in verify
# ---------------------------------------------------------------------------


class TestGateIntegrityDetector:
    """``verify_gate_integrity`` flags terminal items completed without the
    cross-lineage review gate (``adversarial_pass``), and passes legitimate
    completions / review-exempt dismissals."""

    def test_force_close_is_flagged(self, default_project, monkeypatch):
        _native(monkeypatch)
        wi = WorkItemModel.file_work_item(
            project_id=default_project.id, identifier="DG-GATE-01",
            title="force close", status="open", embedding=_vec768(),
        )
        WorkItemModel.close_work_item(default_project.id, "DG-GATE-01", force=True)

        result = verify_gate_integrity(entity_id=wi["entity_id"])
        gate_vs = _gate_violations(result)
        assert len(gate_vs) == 1
        assert gate_vs[0].severity == "warning"
        assert "force-close" in gate_vs[0].message

    def test_force_set_status_to_done_is_flagged(self, default_project, monkeypatch):
        _native(monkeypatch)
        wi = WorkItemModel.file_work_item(
            project_id=default_project.id, identifier="DG-GATE-02",
            title="force done", status="open", embedding=_vec768(),
        )
        WorkItemModel.update_work_item(
            project_id=default_project.id, identifier="DG-GATE-02",
            status="in_progress",
        )
        WorkItemModel.update_work_item(
            project_id=default_project.id, identifier="DG-GATE-02",
            status="in_review",
        )
        # Force-bypass WI-2: in_review → done is not a valid transition.
        WorkItemModel.update_work_item(
            project_id=default_project.id, identifier="DG-GATE-02",
            status="done", force=True,
        )

        result = verify_gate_integrity(entity_id=wi["entity_id"])
        gate_vs = _gate_violations(result)
        assert len(gate_vs) == 1
        assert gate_vs[0].severity == "warning"

    def test_accept_without_adversarial_pass_is_flagged(self, default_project, monkeypatch):
        # Gate-faking: force in_human_review then force done, skipping the
        # adversarial_pass (in_review → in_human_review) entirely.
        _native(monkeypatch)
        wi = WorkItemModel.file_work_item(
            project_id=default_project.id, identifier="DG-GATE-03",
            title="fake gate", status="open", embedding=_vec768(),
        )
        WorkItemModel.update_work_item(
            project_id=default_project.id, identifier="DG-GATE-03",
            status="in_human_review", force=True,
        )
        WorkItemModel.update_work_item(
            project_id=default_project.id, identifier="DG-GATE-03",
            status="done", force=True,
        )

        result = verify_gate_integrity(entity_id=wi["entity_id"])
        gate_vs = _gate_violations(result)
        assert len(gate_vs) == 1
        assert "accept" in gate_vs[0].message

    def test_close_from_open_dismissal_not_flagged(self, default_project, monkeypatch):
        _native(monkeypatch)
        wi = WorkItemModel.file_work_item(
            project_id=default_project.id, identifier="DG-GATE-04",
            title="dismissal", status="open", embedding=_vec768(),
        )
        WorkItemModel.update_work_item(
            project_id=default_project.id, identifier="DG-GATE-04",
            status="done",
        )

        result = verify_gate_integrity(entity_id=wi["entity_id"])
        assert _gate_violations(result) == []
        assert result.ok()

    def test_full_gate_completion_not_flagged(self, default_project, monkeypatch):
        # Drive the full review gate on the native path. The adversarial_pass
        # (in_review → in_human_review) is in the chain, so the force-accept
        # to done is a legitimate completion, not a degraded one.
        _native(monkeypatch)
        wi = WorkItemModel.file_work_item(
            project_id=default_project.id, identifier="DG-GATE-05",
            title="legit gate", status="open", embedding=_vec768(),
        )
        WorkItemModel.update_work_item(
            project_id=default_project.id, identifier="DG-GATE-05",
            status="in_progress",
        )
        WorkItemModel.update_work_item(
            project_id=default_project.id, identifier="DG-GATE-05",
            status="in_review",
        )
        WorkItemModel.update_work_item(
            project_id=default_project.id, identifier="DG-GATE-05",
            status="in_human_review",
        )
        WorkItemModel.update_work_item(
            project_id=default_project.id, identifier="DG-GATE-05",
            status="done", force=True,
        )

        result = verify_gate_integrity(entity_id=wi["entity_id"])
        assert _gate_violations(result) == []
        assert result.ok()

    def test_non_terminal_item_not_flagged(self, default_project, monkeypatch):
        _native(monkeypatch)
        wi = WorkItemModel.file_work_item(
            project_id=default_project.id, identifier="DG-GATE-06",
            title="open item", status="open", embedding=_vec768(),
        )
        result = verify_gate_integrity(entity_id=wi["entity_id"])
        assert _gate_violations(result) == []
        assert result.ok()

    def test_attested_completion_not_flagged(self, default_project, monkeypatch):
        # WI-4: a force-closed item with a gate-waiver attestation is skipped.
        _native(monkeypatch)
        wi = WorkItemModel.file_work_item(
            project_id=default_project.id, identifier="DG-GATE-07",
            title="attested", status="open", embedding=_vec768(),
        )
        WorkItemModel.close_work_item(default_project.id, "DG-GATE-07", force=True)
        WorkItemModel.attest_gate_waiver(
            default_project.id, "DG-GATE-07", reason="admin override",
        )

        result = verify_gate_integrity(entity_id=wi["entity_id"])
        assert _gate_violations(result) == []
        assert result.ok()


# ---------------------------------------------------------------------------
# WI-4 — operator-invoked gate-waiver attestation
# ---------------------------------------------------------------------------


class TestGateAttestation:
    def test_attest_records_diagnostic_key(self, default_project, monkeypatch):
        _native(monkeypatch)
        WorkItemModel.file_work_item(
            project_id=default_project.id, identifier="DG-ATT-01",
            title="attest me", status="open", embedding=_vec768(),
        )
        WorkItemModel.close_work_item(default_project.id, "DG-ATT-01", force=True)
        out = WorkItemModel.attest_gate_waiver(
            default_project.id, "DG-ATT-01", reason="retroactive review by operator",
        )
        diag = out.get("diagnostic_keys") or {}
        assert "gate_attestation" in diag
        att = diag["gate_attestation"]
        assert att["status"] == "waived"
        assert att["reason"] == "retroactive review by operator"
        assert "waived_at" in att

    def test_attest_rejects_non_terminal(self, default_project, monkeypatch):
        _native(monkeypatch)
        WorkItemModel.file_work_item(
            project_id=default_project.id, identifier="DG-ATT-02",
            title="still open", status="open", embedding=_vec768(),
        )
        with pytest.raises(ValueError, match="not terminal"):
            WorkItemModel.attest_gate_waiver(
                default_project.id, "DG-ATT-02", reason="n/a",
            )

    def test_attest_clears_detector_warning(self, default_project, monkeypatch):
        _native(monkeypatch)
        wi = WorkItemModel.file_work_item(
            project_id=default_project.id, identifier="DG-ATT-03",
            title="clear flag", status="open", embedding=_vec768(),
        )
        WorkItemModel.close_work_item(default_project.id, "DG-ATT-03", force=True)
        # Before attestation: flagged.
        before = verify_gate_integrity(entity_id=wi["entity_id"])
        assert len(_gate_violations(before)) == 1
        WorkItemModel.attest_gate_waiver(
            default_project.id, "DG-ATT-03", reason="ops waiver",
        )
        # After attestation: cleared.
        after = verify_gate_integrity(entity_id=wi["entity_id"])
        assert _gate_violations(after) == []
