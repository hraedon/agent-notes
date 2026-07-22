-- Plan 023: durable delegated-review attempts and crash-recovery bindings.

CREATE TABLE IF NOT EXISTS review_delegation_attempts (
    attempt_id              UUID PRIMARY KEY,
    idempotency_key         TEXT NOT NULL UNIQUE
                            CHECK (idempotency_key ~ '^[0-9a-f]{64}$'),
    project_id              INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    work_item_entity_id     TEXT NOT NULL,
    work_item_identifier    TEXT NOT NULL,

    repository_identity     TEXT NOT NULL,
    repository_root         TEXT NOT NULL,
    base_sha                TEXT NOT NULL CHECK (length(base_sha) IN (40, 64)),
    head_sha                TEXT NOT NULL CHECK (length(head_sha) IN (40, 64)),
    merge_base_sha          TEXT NOT NULL CHECK (length(merge_base_sha) IN (40, 64)),
    prompt_digest           TEXT NOT NULL CHECK (prompt_digest ~ '^[0-9a-f]{64}$'),

    harness                 TEXT NOT NULL,
    model_identity          TEXT NOT NULL,
    reported_model_identity TEXT,
    harness_session_id      TEXT,
    identity_assurance      TEXT CHECK (
                                identity_assurance IS NULL OR
                                identity_assurance IN ('attested', 'asserted', 'degraded')
                            ),
    evidence_ref            TEXT,

    artifact_digest         TEXT REFERENCES content_blobs(hash),
    intended_transition     TEXT,
    event_id                UUID,
    expected_event_seq      BIGINT CHECK (
                                expected_event_seq IS NULL OR expected_event_seq > 0
                            ),
    native_op_id            TEXT,

    status                  TEXT NOT NULL CHECK (status IN (
                                'planned',
                                'running',
                                'result_validated',
                                'transition_pending',
                                'transition_applied',
                                'failed',
                                'reconciliation_conflict'
                            )),
    error_code              TEXT,
    error_detail            TEXT CHECK (
                                error_detail IS NULL OR length(error_detail) <= 1000
                            ),

    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at              TIMESTAMPTZ,
    result_validated_at     TIMESTAMPTZ,
    transition_pending_at   TIMESTAMPTZ,
    completed_at            TIMESTAMPTZ,

    UNIQUE (work_item_entity_id, head_sha, model_identity, prompt_digest)
);

CREATE INDEX IF NOT EXISTS review_attempts_item_idx
    ON review_delegation_attempts (project_id, work_item_identifier, created_at DESC);

CREATE INDEX IF NOT EXISTS review_attempts_recovery_idx
    ON review_delegation_attempts (status, updated_at);

CREATE UNIQUE INDEX IF NOT EXISTS review_attempts_event_id_uq
    ON review_delegation_attempts (event_id)
    WHERE event_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS review_attempts_native_op_id_uq
    ON review_delegation_attempts (native_op_id)
    WHERE native_op_id IS NOT NULL;
