"""Private journaled fake-runner orchestration for delegated review (Plan 023)."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Callable

import psycopg

from ._delegated_review import (
    DelegatedReviewError,
    ReviewEvaluation,
    ReviewRequest,
    ReviewRunner,
    evaluate_prepared_review,
    prepare_review,
)
from ._review_journal import (
    AttemptStateError,
    AttemptStatus,
    mark_failed,
    mark_result_validated,
    mark_running,
    plan_attempt,
    store_artifact,
)


@dataclass(frozen=True, slots=True)
class RecordedReviewEvaluation:
    attempt: dict[str, Any]
    evaluation: ReviewEvaluation


def evaluate_and_record(
    *,
    project_id: int,
    request: ReviewRequest,
    runner: ReviewRunner,
    connection_factory: Callable[[], AbstractContextManager[psycopg.Connection]] | None = None,
) -> RecordedReviewEvaluation:
    """Run one private review and durably stop at ``result_validated``.

    The function intentionally cannot perform a lifecycle transition. Commits
    around the external process make ``planned`` and ``running`` observable to
    a fresh process if the runner crashes or the caller disappears.
    """
    if connection_factory is None:
        from agent_notes.core.db import _conn

        connection_factory = _conn

    prepared = prepare_review(request)
    with connection_factory() as conn:
        attempt = plan_attempt(
            conn,
            project_id=project_id,
            request=request,
            target=prepared.target,
            prompt_digest=prepared.prompt_digest,
        )
        conn.commit()
    if attempt["status"] != AttemptStatus.PLANNED.value:
        raise AttemptStateError(
            f"review attempt {attempt['attempt_id']} already has status {attempt['status']}"
        )
    with connection_factory() as conn:
        attempt = mark_running(conn, attempt["attempt_id"])
        conn.commit()

    try:
        evaluation = evaluate_prepared_review(request, prepared, runner)
    except DelegatedReviewError as exc:
        with connection_factory() as conn:
            mark_failed(
                conn,
                attempt["attempt_id"],
                expected=AttemptStatus.RUNNING,
                error_code=exc.code.value,
                error_detail=str(exc),
            )
            conn.commit()
        raise

    with connection_factory() as conn:
        artifact_digest = store_artifact(conn, evaluation.artifact)
        process = evaluation.artifact.process
        attempt = mark_result_validated(
            conn,
            attempt["attempt_id"],
            artifact_digest=artifact_digest,
            reported_model_identity=process.reported_model,
            harness_session_id=process.session_id,
            identity_assurance=evaluation.artifact.identity_assurance,
            evidence_ref=evaluation.artifact.evidence_ref,
        )
        conn.commit()
    return RecordedReviewEvaluation(attempt=attempt, evaluation=evaluation)
