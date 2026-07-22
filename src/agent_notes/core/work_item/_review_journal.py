"""Durable delegated-review attempt journal (Plan 023 D6)."""

from __future__ import annotations

import hashlib
import json
import uuid
from enum import StrEnum
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from agent_notes.core import kernel

from ._delegated_review import IdentityAssurance, ReviewArtifact, ReviewRequest
from ._review_git import ResolvedGitTarget


class AttemptStatus(StrEnum):
    PLANNED = "planned"
    RUNNING = "running"
    RESULT_VALIDATED = "result_validated"
    TRANSITION_PENDING = "transition_pending"
    TRANSITION_APPLIED = "transition_applied"
    FAILED = "failed"
    RECONCILIATION_CONFLICT = "reconciliation_conflict"


class AttemptStateError(RuntimeError):
    pass


def attempt_idempotency_key(
    work_item_entity_id: str,
    head_sha: str,
    model_identity: str,
    prompt_digest: str,
) -> str:
    canonical = json.dumps(
        {
            "work_item_entity_id": work_item_entity_id,
            "head_sha": head_sha,
            "model_identity": model_identity,
            "prompt_digest": prompt_digest,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def plan_attempt(
    conn: psycopg.Connection,
    *,
    project_id: int,
    request: ReviewRequest,
    target: ResolvedGitTarget,
    prompt_digest: str,
) -> dict[str, Any]:
    """Insert the unique planned attempt, returning an existing retry row."""
    key = attempt_idempotency_key(
        request.work_item_entity_id,
        target.head_sha,
        request.requested_model,
        prompt_digest,
    )
    attempt_id = uuid.uuid4()
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        """
        INSERT INTO review_delegation_attempts (
            attempt_id, idempotency_key, project_id,
            work_item_entity_id, work_item_identifier,
            repository_identity, repository_root,
            base_sha, head_sha, merge_base_sha, prompt_digest,
            harness, model_identity, status
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'planned'
        )
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING *
        """,
        (
            attempt_id,
            key,
            project_id,
            request.work_item_entity_id,
            request.work_item_identifier,
            request.repository_identity,
            str(target.repository_root),
            target.base_sha,
            target.head_sha,
            target.merge_base_sha,
            prompt_digest,
            request.harness,
            request.requested_model,
        ),
    )
    row = cur.fetchone()
    if row is not None:
        return dict(row)
    cur.execute(
        "SELECT * FROM review_delegation_attempts WHERE idempotency_key = %s",
        (key,),
    )
    existing = cur.fetchone()
    if existing is None:
        raise RuntimeError("attempt insert conflicted but existing row was not found")
    return dict(existing)


def get_attempt(conn: psycopg.Connection, attempt_id: uuid.UUID) -> dict[str, Any] | None:
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        "SELECT * FROM review_delegation_attempts WHERE attempt_id = %s",
        (attempt_id,),
    )
    row = cur.fetchone()
    return dict(row) if row is not None else None


def store_artifact(conn: psycopg.Connection, artifact: ReviewArtifact) -> str:
    canonical = artifact.canonical_bytes()
    text = canonical.decode("utf-8", errors="strict")
    digest = kernel.store_blob(conn, text)
    if digest != artifact.digest:
        raise RuntimeError("stored review artifact digest does not match canonical artifact")
    return digest


def _compare_and_set(
    conn: psycopg.Connection,
    attempt_id: uuid.UUID,
    expected: AttemptStatus,
    target: AttemptStatus,
    *,
    values: dict[str, Any] | None = None,
    timestamp_field: str | None = None,
) -> dict[str, Any]:
    assignments: list[sql.Composable] = [
        sql.SQL("status = %s"),
        sql.SQL("updated_at = now()"),
    ]
    params: list[Any] = [target.value]
    for field, value in (values or {}).items():
        assignments.append(sql.SQL("{} = %s").format(sql.Identifier(field)))
        params.append(value)
    if timestamp_field is not None:
        assignments.append(sql.SQL("{} = now()").format(sql.Identifier(timestamp_field)))
    params.extend((attempt_id, expected.value))
    query = sql.SQL(
        "UPDATE review_delegation_attempts SET {} WHERE attempt_id = %s AND status = %s RETURNING *"
    ).format(sql.SQL(", ").join(assignments))
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(query, params)
    row = cur.fetchone()
    if row is None:
        current = get_attempt(conn, attempt_id)
        actual = current["status"] if current is not None else "missing"
        raise AttemptStateError(f"attempt {attempt_id} expected {expected.value}, found {actual}")
    return dict(row)


def mark_running(conn: psycopg.Connection, attempt_id: uuid.UUID) -> dict[str, Any]:
    return _compare_and_set(
        conn,
        attempt_id,
        AttemptStatus.PLANNED,
        AttemptStatus.RUNNING,
        timestamp_field="started_at",
    )


def mark_result_validated(
    conn: psycopg.Connection,
    attempt_id: uuid.UUID,
    *,
    artifact_digest: str,
    reported_model_identity: str,
    harness_session_id: str,
    identity_assurance: IdentityAssurance,
    evidence_ref: str | None,
) -> dict[str, Any]:
    return _compare_and_set(
        conn,
        attempt_id,
        AttemptStatus.RUNNING,
        AttemptStatus.RESULT_VALIDATED,
        values={
            "artifact_digest": artifact_digest,
            "reported_model_identity": reported_model_identity,
            "harness_session_id": harness_session_id,
            "identity_assurance": identity_assurance.value,
            "evidence_ref": evidence_ref,
        },
        timestamp_field="result_validated_at",
    )


def mark_transition_pending(
    conn: psycopg.Connection,
    attempt_id: uuid.UUID,
    *,
    intended_transition: str,
    event_id: uuid.UUID | None = None,
    expected_event_seq: int | None = None,
    native_op_id: str | None = None,
) -> dict[str, Any]:
    if (event_id is None) == (native_op_id is None):
        raise ValueError("exactly one of event_id or native_op_id is required")
    if event_id is not None and expected_event_seq is None:
        raise ValueError("Regista transition requires expected_event_seq")
    return _compare_and_set(
        conn,
        attempt_id,
        AttemptStatus.RESULT_VALIDATED,
        AttemptStatus.TRANSITION_PENDING,
        values={
            "intended_transition": intended_transition,
            "event_id": event_id,
            "expected_event_seq": expected_event_seq,
            "native_op_id": native_op_id,
        },
        timestamp_field="transition_pending_at",
    )


def mark_transition_applied(
    conn: psycopg.Connection,
    attempt_id: uuid.UUID,
) -> dict[str, Any]:
    return _compare_and_set(
        conn,
        attempt_id,
        AttemptStatus.TRANSITION_PENDING,
        AttemptStatus.TRANSITION_APPLIED,
        timestamp_field="completed_at",
    )


def mark_failed(
    conn: psycopg.Connection,
    attempt_id: uuid.UUID,
    *,
    expected: AttemptStatus,
    error_code: str,
    error_detail: str,
    reconciliation_conflict: bool = False,
) -> dict[str, Any]:
    target = (
        AttemptStatus.RECONCILIATION_CONFLICT if reconciliation_conflict else AttemptStatus.FAILED
    )
    return _compare_and_set(
        conn,
        attempt_id,
        expected,
        target,
        values={"error_code": error_code, "error_detail": error_detail[:1000]},
        timestamp_field="completed_at",
    )
