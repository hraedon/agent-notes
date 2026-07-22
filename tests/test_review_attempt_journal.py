"""Durable delegated-review attempt journal tests (Plan 023 D6)."""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import psycopg
import pytest

from agent_notes.core.work_item._delegated_review import (
    DelegatedReviewError,
    IdentityAssurance,
    ReviewArtifact,
    ReviewErrorCode,
    ReviewProcessResult,
    ReviewRequest,
    parse_review_result,
)
from agent_notes.core.work_item._review_git import ResolvedGitTarget
from agent_notes.core.work_item._review_journal import (
    AttemptStateError,
    AttemptStatus,
    mark_failed,
    mark_result_validated,
    mark_running,
    mark_transition_applied,
    mark_transition_pending,
    plan_attempt,
    store_artifact,
)
from agent_notes.core.work_item._review_orchestrator import evaluate_and_record


def _request(prompt_suffix: str = "") -> ReviewRequest:
    return ReviewRequest(
        work_item_entity_id=f"entity-{prompt_suffix or 'one'}",
        work_item_identifier="WI-123",
        work_item_title="Review",
        work_item_body="Body",
        work_item_status="in_review",
        repository_identity="default/agent-notes-test",
        repository_root=Path("/repository"),
        base_revision="base",
        head_revision="head",
        harness="fake",
        requested_model="provider/model",
        identity_assurance=IdentityAssurance.ATTESTED,
    )


def _target(head: str = "b") -> ResolvedGitTarget:
    return ResolvedGitTarget(
        repository_root=Path("/repository"),
        base_sha="a" * 40,
        head_sha=head * 40,
        merge_base_sha="a" * 40,
        changed_paths=("code.py",),
    )


def _artifact(target: ResolvedGitTarget) -> ReviewArtifact:
    result = parse_review_result(
        b'{"schema_version":1,"verdict":"accept","summary":"Good.",'
        b'"blocking_findings":[],"non_blocking_risks":[],'
        b'"reviewed_paths":["code.py"]}'
    )
    process = ReviewProcessResult(
        exit_code=0,
        structured_outputs=(),
        harness="fake",
        harness_version="1",
        requested_model="provider/model",
        reported_model="provider/model",
        session_id="session-1",
        started_at="2026-07-22T00:00:00Z",
        ended_at="2026-07-22T00:00:01Z",
    )
    return ReviewArtifact(
        result=result,
        target=target,
        repository_identity="default/agent-notes-test",
        process=process,
        prompt_digest="d" * 64,
        identity_assurance=IdentityAssurance.ATTESTED,
        evidence_ref="cairn:test",
    )


def _project_id(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO workspaces (slug, name) VALUES (%s, %s) RETURNING id",
            (f"journal-{uuid.uuid4().hex}", "Journal"),
        )
        workspace_id = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO projects (workspace_id, slug, name, repo_root)
            VALUES (%s, %s, %s, %s) RETURNING id
            """,
            (workspace_id, "journal", "Journal", "/repository"),
        )
        return cur.fetchone()[0]


def test_attempt_journal_happy_path_is_idempotent_and_cas_guarded(ephemeral_db) -> None:
    with psycopg.connect(ephemeral_db) as conn:
        project_id = _project_id(conn)
        request = _request()
        target = _target()
        planned = plan_attempt(
            conn,
            project_id=project_id,
            request=request,
            target=target,
            prompt_digest="d" * 64,
        )
        duplicate = plan_attempt(
            conn,
            project_id=project_id,
            request=request,
            target=target,
            prompt_digest="d" * 64,
        )
        assert planned["attempt_id"] == duplicate["attempt_id"]
        assert planned["status"] == AttemptStatus.PLANNED.value

        running = mark_running(conn, planned["attempt_id"])
        assert running["started_at"] is not None
        with pytest.raises(AttemptStateError):
            mark_running(conn, planned["attempt_id"])

        artifact = _artifact(target)
        digest = store_artifact(conn, artifact)
        validated = mark_result_validated(
            conn,
            planned["attempt_id"],
            artifact_digest=digest,
            reported_model_identity="provider/model",
            harness_session_id="session-1",
            identity_assurance=IdentityAssurance.ATTESTED,
            evidence_ref="cairn:test",
        )
        assert validated["artifact_digest"] == artifact.digest
        assert validated["result_validated_at"] is not None

        event_id = uuid.uuid4()
        pending = mark_transition_pending(
            conn,
            planned["attempt_id"],
            intended_transition="adversarial_pass",
            event_id=event_id,
            expected_event_seq=3,
        )
        assert pending["event_id"] == event_id
        assert pending["transition_pending_at"] is not None

        applied = mark_transition_applied(conn, planned["attempt_id"])
        assert applied["status"] == AttemptStatus.TRANSITION_APPLIED.value
        assert applied["completed_at"] is not None


def test_attempt_journal_records_bounded_reconciliation_conflict(ephemeral_db) -> None:
    with psycopg.connect(ephemeral_db) as conn:
        project_id = _project_id(conn)
        request = _request("two")
        target = _target("c")
        attempt = plan_attempt(
            conn,
            project_id=project_id,
            request=request,
            target=target,
            prompt_digest="e" * 64,
        )
        mark_running(conn, attempt["attempt_id"])
        digest = store_artifact(conn, _artifact(target))
        mark_result_validated(
            conn,
            attempt["attempt_id"],
            artifact_digest=digest,
            reported_model_identity="provider/model",
            harness_session_id="session-2",
            identity_assurance=IdentityAssurance.ASSERTED,
            evidence_ref=None,
        )
        mark_transition_pending(
            conn,
            attempt["attempt_id"],
            intended_transition="adversarial_pass",
            native_op_id="f" * 64,
        )
        failed = mark_failed(
            conn,
            attempt["attempt_id"],
            expected=AttemptStatus.TRANSITION_PENDING,
            error_code="REVIEW_RECONCILIATION_CONFLICT",
            error_detail="x" * 2000,
            reconciliation_conflict=True,
        )
        assert failed["status"] == AttemptStatus.RECONCILIATION_CONFLICT.value
        assert len(failed["error_detail"]) == 1000


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(root), *args),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _review_repository(tmp_path: Path) -> tuple[Path, str, str]:
    root = tmp_path / "journal-review-repo"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.name", "Journal Test")
    _git(root, "config", "user.email", "journal@example.test")
    (root / "code.py").write_text("one = 1\n", encoding="utf-8")
    _git(root, "add", "code.py")
    _git(root, "commit", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    (root / "code.py").write_text("one = 2\n", encoding="utf-8")
    _git(root, "commit", "-am", "head")
    return root, base, _git(root, "rev-parse", "HEAD")


class _JournalRunner:
    def __init__(self, document: bytes) -> None:
        self.document = document

    def run(self, *, prompt: str, cwd: Path, timeout_seconds: float) -> ReviewProcessResult:
        assert "Review exactly this local Git range" in prompt
        assert (cwd / "code.py").exists()
        return ReviewProcessResult(
            exit_code=0,
            structured_outputs=(self.document,),
            harness="fake",
            harness_version="1",
            requested_model="provider/model",
            reported_model="provider/model",
            session_id="journal-session",
            started_at="2026-07-22T00:00:00Z",
            ended_at="2026-07-22T00:00:01Z",
        )


def test_journaled_fake_runner_stops_at_validated_result(
    ephemeral_db,
    tmp_path: Path,
) -> None:
    root, base, head = _review_repository(tmp_path)
    request = ReviewRequest(
        work_item_entity_id=f"entity-{uuid.uuid4().hex}",
        work_item_identifier="WI-124",
        work_item_title="Journal orchestration",
        work_item_body="Review it.",
        work_item_status="in_review",
        repository_identity="default/agent-notes-test",
        repository_root=root,
        base_revision=base,
        head_revision=head,
        harness="fake",
        requested_model="provider/model",
        identity_assurance=IdentityAssurance.ATTESTED,
        evidence_ref="cairn:test-session",
    )
    document = (
        b'{"schema_version":1,"verdict":"accept","summary":"Good.",'
        b'"blocking_findings":[],"non_blocking_risks":[],'
        b'"reviewed_paths":["code.py"]}'
    )
    with psycopg.connect(ephemeral_db) as conn:
        project_id = _project_id(conn)
    recorded = evaluate_and_record(
        project_id=project_id,
        request=request,
        runner=_JournalRunner(document),
        connection_factory=lambda: psycopg.connect(ephemeral_db),
    )
    assert recorded.attempt["status"] == AttemptStatus.RESULT_VALIDATED.value
    assert recorded.attempt["artifact_digest"] == recorded.evaluation.artifact.digest
    assert recorded.attempt["event_id"] is None
    assert recorded.attempt["native_op_id"] is None


def test_journaled_fake_runner_records_validation_failure(
    ephemeral_db,
    tmp_path: Path,
) -> None:
    root, base, head = _review_repository(tmp_path)
    entity_id = f"entity-{uuid.uuid4().hex}"
    request = ReviewRequest(
        work_item_entity_id=entity_id,
        work_item_identifier="WI-125",
        work_item_title="Bad result",
        work_item_body="Reject malformed output.",
        work_item_status="in_review",
        repository_identity="default/agent-notes-test",
        repository_root=root,
        base_revision=base,
        head_revision=head,
        harness="fake",
        requested_model="provider/model",
    )
    with psycopg.connect(ephemeral_db) as conn:
        project_id = _project_id(conn)
    with pytest.raises(DelegatedReviewError) as exc:
        evaluate_and_record(
            project_id=project_id,
            request=request,
            runner=_JournalRunner(b"{}"),
            connection_factory=lambda: psycopg.connect(ephemeral_db),
        )
    assert exc.value.code is ReviewErrorCode.OUTPUT_INVALID
    with psycopg.connect(ephemeral_db) as conn:
        row = conn.execute(
            "SELECT status, error_code FROM review_delegation_attempts "
            "WHERE work_item_entity_id = %s",
            (entity_id,),
        ).fetchone()
        assert row == (AttemptStatus.FAILED.value, ReviewErrorCode.OUTPUT_INVALID.value)
