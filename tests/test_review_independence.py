"""Reviewer independence on the native review gate (Plan 026 D2), on real Postgres.

Identity moves the way it does on a real host: the process environment
(``AGENT_NOTES_ACTOR_ID`` plus ``AGENT_NOTES_MODEL``), or the calling session's
registered model. Each test files an item as an implementer, drives it into
review, then switches to a reviewer identity.
"""

from __future__ import annotations

import json
import uuid

import pytest

from agent_notes.core import kernel, messaging
from agent_notes.core.db import _conn
from agent_notes.core.work_item import _independence
from agent_notes.core.work_item._independence import IndependenceError
from agent_notes.core.work_item_model import WorkItemModel
from tests.conftest import ephemeral_db  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")

CLAUDE = "claude-opus-5-5"
GPT = "gpt-6.1-sol"
GEMINI = "gemini-4-argon-medium"


@pytest.fixture
def project():
    from agent_notes.core import db as coredb

    ws = coredb.get_or_create_workspace("default", "Default Workspace")
    return coredb.get_or_create_project(
        ws.id, slug="indep", name="indep", repo_root="/projects/indep"
    )


@pytest.fixture(autouse=True)
def _native_and_hermetic(monkeypatch):
    monkeypatch.delenv("AGENT_NOTES_REGISTA_WRITES", raising=False)
    monkeypatch.delenv("REGISTA_DSN", raising=False)
    for name in (
        *_independence.MODEL_ENVS,
        *_independence.HARNESS_ENVS,
        messaging.SESSION_ENV,
        *(var for var, _ in messaging.HARNESS_ENV_VARS),
    ):
        monkeypatch.delenv(name, raising=False)


def _as(monkeypatch, actor: str, model: str | None) -> None:
    monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", actor)
    if model is None:
        monkeypatch.delenv("AGENT_NOTES_MODEL", raising=False)
    else:
        monkeypatch.setenv("AGENT_NOTES_MODEL", model)


def _to_review(project_id: int, identifier: str) -> None:
    WorkItemModel.file_work_item(
        project_id=project_id,
        identifier=identifier,
        title="independence",
        status="open",
        embedding=[0.0] * 768,
    )
    WorkItemModel.update_work_item(
        project_id=project_id, identifier=identifier, status="in_progress"
    )
    WorkItemModel.update_work_item(project_id=project_id, identifier=identifier, status="in_review")


def _status(project_id: int, identifier: str) -> str:
    return WorkItemModel.get_work_item(project_id, identifier)["status"]


def _last_status_payload(project_id: int, identifier: str) -> dict:
    entity_id = WorkItemModel.get_work_item(project_id, identifier)["entity_id"]
    with _conn() as conn:
        ops = [o for o in kernel._get_entity_ops(conn, entity_id) if o["op_type"] == "set_status"]
    return ops[-1]["payload"]


# ---------------------------------------------------------------------------
# Lineage vocabulary (D2.2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "lineage"),
    [
        ("claude-opus-5-5", "anthropic"),
        ("claude-fable-5", "anthropic"),
        ("anthropic/claude-sonnet-5-5", "anthropic"),
        ("claude-opus", "anthropic"),  # a regista lineage value
        ("fable", "anthropic"),
        ("gpt-6.1-sol", "openai"),
        ("gpt-sol", "openai"),
        ("gpt-oss-120b-medium", "openai"),
        ("o4-mini", "openai"),
        ("gemini-4-argon-medium", "google"),
        ("glm-5.3", "zhipu"),
        ("kimi-k2.5", "moonshot"),
        ("ollama/qwen3-coder", "qwen"),
        ("hermes-4-405b", None),
        ("", None),
    ],
)
def test_lineage_for(value, lineage):
    with _conn() as conn:
        assert _independence.lineage_for(conn, value) == lineage


# ---------------------------------------------------------------------------
# Producer stamping (D2.1)
# ---------------------------------------------------------------------------


def test_authored_ops_carry_the_producer(monkeypatch, project):
    _as(monkeypatch, "agent:impl", GPT)
    _to_review(project.id, "IND-1")
    entity_id = WorkItemModel.get_work_item(project.id, "IND-1")["entity_id"]
    with _conn() as conn:
        ops = kernel._get_entity_ops(conn, entity_id)
    authored = [o for o in ops if o["op_type"] in ("create", "set_status")]
    assert authored
    for op in authored:
        producer = op["payload"]["producer"]
        assert producer["model"] == GPT
        assert producer["lineage"] == "openai"
        assert producer["source"] == "env"


def test_session_model_is_the_fallback(monkeypatch, project):
    """A hook-registered session model stamps ops when the env names none."""
    with _conn() as conn:
        ws_id = conn.execute(
            "SELECT workspace_id FROM projects WHERE id = %s", (project.id,)
        ).fetchone()["workspace_id"]
    vm = messaging.resolve_vm_id()
    messaging.register_session(vm, "codex", "thread-1", workspace_id=ws_id, model=GPT)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-1")
    _as(monkeypatch, "agent:impl", None)
    WorkItemModel.file_work_item(
        project_id=project.id, identifier="IND-2", title="t", status="open", embedding=[0.0] * 768
    )
    entity_id = WorkItemModel.get_work_item(project.id, "IND-2")["entity_id"]
    with _conn() as conn:
        create = kernel._get_entity_ops(conn, entity_id)[0]
    producer = create["payload"]["producer"]
    assert (producer["model"], producer["lineage"], producer["source"]) == (
        GPT,
        "openai",
        "session",
    )
    assert producer["harness"] == "codex"
    assert producer["session_key"] == messaging.session_key(vm, "codex", "thread-1")


def test_session_model_is_kept_when_a_heartbeat_reports_none():
    with _conn() as conn:
        ws_id = messaging.default_workspace_id(conn=conn)
    vm = messaging.resolve_vm_id()
    messaging.register_session(vm, "claude", "s-1", workspace_id=ws_id, model=CLAUDE)
    row = messaging.register_session(vm, "claude", "s-1", workspace_id=ws_id, model=None)
    assert row["model"] == CLAUDE


# ---------------------------------------------------------------------------
# The rule (D2.4, D2.5)
# ---------------------------------------------------------------------------


def test_same_lineage_pass_is_refused(monkeypatch, project):
    _as(monkeypatch, "agent:impl", CLAUDE)
    _to_review(project.id, "IND-3")
    _as(monkeypatch, "agent:other", "claude-sonnet-5-5")
    with pytest.raises(IndependenceError, match="shares the reviewer's model lineage"):
        WorkItemModel.review_transition(project.id, "IND-3", "adversarial_pass", "looks fine")
    assert _status(project.id, "IND-3") == "in_review"


def test_cross_lineage_pass_and_accept_record_evidence(monkeypatch, project):
    _as(monkeypatch, "agent:impl", GPT)
    _to_review(project.id, "IND-4")
    _as(monkeypatch, "agent:claude-reviewer", CLAUDE)
    WorkItemModel.review_transition(project.id, "IND-4", "adversarial_pass", "verified tests")
    evidence = _last_status_payload(project.id, "IND-4")["independence"]
    assert evidence["independence"] == "verified"
    assert evidence["reviewer"]["lineage"] == "anthropic"
    assert evidence["implementers"] == [
        {"actor_id": "agent:impl", "lineage": "openai", "model": GPT}
    ]

    # The passer's review ops don't make it an implementer: a third lineage accepts.
    _as(monkeypatch, "agent:gemini-accepter", GEMINI)
    wi = WorkItemModel.review_transition(project.id, "IND-4", "accept", "ship it")
    assert wi["status"] == "done"


def test_accept_is_refused_for_the_implementers_lineage(monkeypatch, project):
    _as(monkeypatch, "agent:impl", GPT)
    _to_review(project.id, "IND-5")
    _as(monkeypatch, "agent:reviewer", CLAUDE)
    WorkItemModel.review_transition(project.id, "IND-5", "adversarial_pass", "ok")
    _as(monkeypatch, "agent:impl-2", "gpt-6-astra")
    with pytest.raises(IndependenceError):
        WorkItemModel.review_transition(project.id, "IND-5", "accept", "self-accept")
    assert _status(project.id, "IND-5") == "in_human_review"


def test_close_out_of_review_is_gated(monkeypatch, project):
    _as(monkeypatch, "agent:impl", GPT)
    _to_review(project.id, "IND-6")
    with pytest.raises(IndependenceError):
        WorkItemModel.close_work_item(project.id, "IND-6")
    assert _status(project.id, "IND-6") == "in_review"
    _as(monkeypatch, "agent:reviewer", CLAUDE)
    assert WorkItemModel.close_work_item(project.id, "IND-6")["status"] == "done"


def test_update_to_done_or_human_review_is_gated(monkeypatch, project):
    _as(monkeypatch, "agent:impl", GPT)
    _to_review(project.id, "IND-7")
    with pytest.raises(IndependenceError):
        WorkItemModel.update_work_item(
            project_id=project.id, identifier="IND-7", status="in_human_review"
        )
    assert _status(project.id, "IND-7") == "in_review"
    _as(monkeypatch, "agent:reviewer", CLAUDE)
    WorkItemModel.review_transition(project.id, "IND-7", "adversarial_pass", "ok")
    # The lifecycle already forbids in_review -> done through update; the
    # bypass to close is in_human_review -> done.
    _as(monkeypatch, "agent:impl", GPT)
    with pytest.raises(IndependenceError):
        WorkItemModel.update_work_item(project_id=project.id, identifier="IND-7", status="done")
    assert _status(project.id, "IND-7") == "in_human_review"


def test_forcing_does_not_skip_the_gate(monkeypatch, project):
    _as(monkeypatch, "agent:impl", GPT)
    _to_review(project.id, "IND-8")
    with pytest.raises(IndependenceError):
        WorkItemModel.close_work_item(project.id, "IND-8", force=True)
    with pytest.raises(IndependenceError):
        WorkItemModel.update_work_item(
            project_id=project.id, identifier="IND-8", status="done", force=True
        )
    assert _status(project.id, "IND-8") == "in_review"
    _as(monkeypatch, "agent:reviewer", CLAUDE)
    assert WorkItemModel.close_work_item(project.id, "IND-8", force=True)["status"] == "closed"


def test_closed_alias_is_gated(monkeypatch, project):
    _as(monkeypatch, "agent:impl", GPT)
    _to_review(project.id, "IND-8B")
    _as(monkeypatch, "agent:reviewer", CLAUDE)
    WorkItemModel.review_transition(project.id, "IND-8B", "adversarial_pass", "ok")
    _as(monkeypatch, "agent:impl", GPT)
    with pytest.raises(IndependenceError):
        WorkItemModel.update_work_item(project_id=project.id, identifier="IND-8B", status="closed")
    assert _status(project.id, "IND-8B") == "in_human_review"


def test_close_without_review_stays_ungated(monkeypatch, project):
    """Plan 025 D7: review is opt-in; closing never-reviewed work isn't a verdict."""
    _as(monkeypatch, "agent:impl", GPT)
    WorkItemModel.file_work_item(
        project_id=project.id, identifier="IND-9", title="t", status="open", embedding=[0.0] * 768
    )
    assert WorkItemModel.close_work_item(project.id, "IND-9")["status"] == "done"


def test_unknown_reviewer_lineage_is_refused(monkeypatch, project):
    _as(monkeypatch, "agent:impl", GPT)
    _to_review(project.id, "IND-10")
    _as(monkeypatch, "agent:reviewer", None)
    with pytest.raises(IndependenceError, match="lineage is unknown"):
        WorkItemModel.review_transition(project.id, "IND-10", "adversarial_pass", "ok")


def test_legacy_implementer_needs_a_different_actor(monkeypatch, project):
    _as(monkeypatch, "agent:impl", None)  # no model: as every item filed before D2
    _to_review(project.id, "IND-11")
    _as(monkeypatch, "agent:impl", CLAUDE)
    with pytest.raises(IndependenceError, match="model unknown"):
        WorkItemModel.review_transition(project.id, "IND-11", "adversarial_pass", "ok")
    _as(monkeypatch, "agent:reviewer", CLAUDE)
    WorkItemModel.review_transition(project.id, "IND-11", "adversarial_pass", "ok")
    assert _last_status_payload(project.id, "IND-11")["independence"]["independence"] == (
        "unverified-legacy"
    )


def test_human_reviewer_skips_lineage_but_not_actor(monkeypatch, project):
    _as(monkeypatch, "human:operator", None)
    _to_review(project.id, "IND-12")
    with pytest.raises(IndependenceError, match="worked on this item"):
        WorkItemModel.review_transition(project.id, "IND-12", "adversarial_pass", "ok")

    _as(monkeypatch, "agent:impl", CLAUDE)
    _to_review(project.id, "IND-13")
    _as(monkeypatch, "human:operator", None)
    WorkItemModel.review_transition(project.id, "IND-13", "adversarial_pass", "ok")
    assert _last_status_payload(project.id, "IND-13")["independence"]["independence"] == "human"


def test_acknowledgement_is_recorded_and_needs_a_reason(monkeypatch, project):
    _as(monkeypatch, "agent:impl", CLAUDE)
    _to_review(project.id, "IND-14")
    with pytest.raises(IndependenceError, match="needs a reason"):
        WorkItemModel.review_transition(
            project.id, "IND-14", "adversarial_pass", "ok", acknowledgement="  "
        )
    WorkItemModel.review_transition(
        project.id, "IND-14", "adversarial_pass", "ok", acknowledgement="trivial docs typo"
    )
    evidence = _last_status_payload(project.id, "IND-14")["independence"]
    assert evidence["independence"] == "acknowledged"
    assert evidence["acknowledgement"] == "trivial docs typo"


def test_request_changes_resets_the_implementer_set(monkeypatch, project):
    """After a bounce, only work done since counts; the reviewer's finding doesn't."""
    _as(monkeypatch, "agent:claude-impl", CLAUDE)
    _to_review(project.id, "IND-15")
    _as(monkeypatch, "agent:gpt-reviewer", GPT)
    WorkItemModel.review_transition(project.id, "IND-15", "request_changes", "fix the test")
    _as(monkeypatch, "agent:gemini-impl", GEMINI)
    WorkItemModel.update_work_item(project_id=project.id, identifier="IND-15", status="in_review")
    # Claude's earlier work is behind the bounce, so a Claude reviewer may pass it now.
    _as(monkeypatch, "agent:claude-reviewer", CLAUDE)
    WorkItemModel.review_transition(project.id, "IND-15", "adversarial_pass", "fixed")
    evidence = _last_status_payload(project.id, "IND-15")["independence"]
    assert [p["lineage"] for p in evidence["implementers"]] == ["google"]


def test_negative_verdicts_are_ungated(monkeypatch, project):
    _as(monkeypatch, "agent:impl", CLAUDE)
    _to_review(project.id, "IND-16")
    WorkItemModel.review_transition(project.id, "IND-16", "request_changes", "self-review finding")
    assert _status(project.id, "IND-16") == "in_progress"


# ---------------------------------------------------------------------------
# Delegated review attempts (D2.7)
# ---------------------------------------------------------------------------


def _attempt(
    project_id: int,
    identifier: str,
    *,
    verdict: str = "accept",
    model: str = CLAUDE,
    assurance: str = "asserted",
    status: str = "result_validated",
) -> str:
    wi = WorkItemModel.get_work_item(project_id, identifier)
    attempt_id = uuid.uuid4()
    with _conn() as conn:
        digest = kernel.store_blob(conn, json.dumps({"result": {"verdict": verdict}}))
        conn.execute(
            """
            INSERT INTO review_delegation_attempts
                (attempt_id, idempotency_key, project_id, work_item_entity_id,
                 work_item_identifier, repository_identity, repository_root,
                 base_sha, head_sha, merge_base_sha, prompt_digest, harness,
                 model_identity, identity_assurance, artifact_digest, status,
                 item_lamport)
            VALUES (%s, %s, %s, %s, %s, 'repo', '/repo', %s, %s, %s, %s, 'claude',
                    %s, %s, %s, %s,
                    (SELECT max(lamport) FROM op_log WHERE entity_id = %s))
            """,
            (
                attempt_id,
                uuid.uuid4().hex * 2,
                project_id,
                wi["entity_id"],
                identifier,
                "a" * 40,
                "b" * 40,
                "a" * 40,
                uuid.uuid4().hex * 2,
                model,
                assurance,
                digest,
                status,
                wi["entity_id"],
            ),
        )
        conn.commit()
    return str(attempt_id)


def test_attempt_is_the_reviewer_and_is_consumed(monkeypatch, project):
    """Plan 025's headline flow: Codex implements, Claude reviews, Codex applies it."""
    _as(monkeypatch, "agent:codex", GPT)
    _to_review(project.id, "IND-17")
    attempt = _attempt(project.id, "IND-17")
    WorkItemModel.review_transition(
        project.id, "IND-17", "adversarial_pass", "Claude accepted", attempt_id=attempt
    )
    evidence = _last_status_payload(project.id, "IND-17")["independence"]
    assert evidence["reviewer"]["attempt_id"] == attempt
    assert evidence["reviewer"]["lineage"] == "anthropic"
    with _conn() as conn:
        row = conn.execute(
            "SELECT status, native_op_id FROM review_delegation_attempts WHERE attempt_id = %s",
            (uuid.UUID(attempt),),
        ).fetchone()
    assert row["status"] == "transition_applied"
    assert row["native_op_id"]

    # Single use: the same attempt can't also accept.
    with pytest.raises(IndependenceError, match="not result_validated"):
        WorkItemModel.review_transition(
            project.id, "IND-17", "accept", "replay", attempt_id=attempt
        )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"verdict": "request_changes"}, "only an accept verdict"),
        ({"model": "gpt-6-astra"}, "shares the reviewer's model lineage"),
        ({"assurance": "degraded"}, "identity assurance"),
        ({"status": "running"}, "not result_validated"),
    ],
)
def test_attempt_refusals(monkeypatch, project, kwargs, message):
    _as(monkeypatch, "agent:codex", GPT)
    identifier = f"IND-A-{message.split()[0]}"
    _to_review(project.id, identifier)
    attempt = _attempt(project.id, identifier, **kwargs)
    with pytest.raises(IndependenceError, match=message):
        WorkItemModel.review_transition(
            project.id, identifier, "adversarial_pass", "x", attempt_id=attempt
        )


def test_attempt_for_another_item_is_refused(monkeypatch, project):
    _as(monkeypatch, "agent:codex", GPT)
    _to_review(project.id, "IND-18")
    _to_review(project.id, "IND-19")
    attempt = _attempt(project.id, "IND-18")
    with pytest.raises(IndependenceError, match="different work item"):
        WorkItemModel.review_transition(
            project.id, "IND-19", "adversarial_pass", "x", attempt_id=attempt
        )


# ---------------------------------------------------------------------------
# Bypasses found in review (GPT-6.1-sol, 2026-10-29)
# ---------------------------------------------------------------------------


def test_release_does_not_take_an_item_out_of_review(monkeypatch, project):
    _as(monkeypatch, "agent:impl", GPT)
    _to_review(project.id, "IND-20")
    WorkItemModel.release_work_item(project.id, "IND-20")
    assert _status(project.id, "IND-20") == "in_review"


def test_release_of_a_claim_still_reopens(monkeypatch, project):
    _as(monkeypatch, "agent:impl", GPT)
    WorkItemModel.file_work_item(
        project_id=project.id, identifier="IND-21", title="t", status="open", embedding=[0.0] * 768
    )
    WorkItemModel.claim_work_item(project.id, "IND-21")
    assert WorkItemModel.release_work_item(project.id, "IND-21")["status"] == "open"


def test_approving_and_editing_in_one_update_is_refused(monkeypatch, project):
    _as(monkeypatch, "agent:impl", GPT)
    _to_review(project.id, "IND-22")
    _as(monkeypatch, "agent:reviewer", CLAUDE)
    with pytest.raises(IndependenceError, match="editing the item in one update"):
        WorkItemModel.update_work_item(
            project_id=project.id,
            identifier="IND-22",
            status="in_human_review",
            body="the reviewer's own rewrite",
        )
    assert _status(project.id, "IND-22") == "in_review"


def test_attempt_is_stale_after_a_content_edit(monkeypatch, project):
    _as(monkeypatch, "agent:codex", GPT)
    _to_review(project.id, "IND-23")
    attempt = _attempt(project.id, "IND-23")
    WorkItemModel.update_work_item(project_id=project.id, identifier="IND-23", body="changed")
    with pytest.raises(IndependenceError, match="changed after review attempt"):
        WorkItemModel.review_transition(
            project.id, "IND-23", "adversarial_pass", "x", attempt_id=attempt
        )


def test_a_service_that_reports_a_model_is_an_implementer(monkeypatch, project):
    _as(monkeypatch, "service:bot", CLAUDE)
    _to_review(project.id, "IND-24")
    _as(monkeypatch, "agent:other", "claude-sonnet-5-5")
    with pytest.raises(IndependenceError, match="shares the reviewer's model lineage"):
        WorkItemModel.review_transition(project.id, "IND-24", "adversarial_pass", "ok")


def test_a_service_without_a_model_falls_under_the_actor_rule(monkeypatch, project):
    _as(monkeypatch, "service:bot", None)
    _to_review(project.id, "IND-25")
    # The same principal can't implement without a model and review with one.
    _as(monkeypatch, "service:bot", CLAUDE)
    with pytest.raises(IndependenceError, match="model unknown"):
        WorkItemModel.review_transition(project.id, "IND-25", "adversarial_pass", "ok")
    _as(monkeypatch, "agent:reviewer", CLAUDE)
    WorkItemModel.review_transition(project.id, "IND-25", "adversarial_pass", "ok")
    evidence = _last_status_payload(project.id, "IND-25")["independence"]
    assert evidence["implementers"] == [{"actor_id": "service:bot", "lineage": None, "model": None}]
    assert evidence["independence"] == "unverified-legacy"


def test_a_diagnostic_edit_that_keeps_old_review_notes_is_still_work(monkeypatch, project):
    _as(monkeypatch, "agent:impl", GPT)
    _to_review(project.id, "IND-26")
    _as(monkeypatch, "agent:reviewer", CLAUDE)
    WorkItemModel.review_transition(project.id, "IND-26", "request_changes", "add a test")
    # A Claude implementer edits diagnostics; the old review notes ride along.
    _as(monkeypatch, "agent:claude-impl", CLAUDE)
    diag = WorkItemModel.get_work_item(project.id, "IND-26")["diagnostic_keys"]
    WorkItemModel.update_work_item(
        project_id=project.id,
        identifier="IND-26",
        diagnostic_keys={**diag, "test_added": True},
    )
    WorkItemModel.update_work_item(project_id=project.id, identifier="IND-26", status="in_review")
    _as(monkeypatch, "agent:claude-reviewer", CLAUDE)
    with pytest.raises(IndependenceError, match="shares the reviewer's model lineage"):
        WorkItemModel.review_transition(project.id, "IND-26", "adversarial_pass", "ok")


def test_a_bounce_by_plain_update_resets_the_implementers(monkeypatch, project):
    _as(monkeypatch, "agent:claude-impl", CLAUDE)
    _to_review(project.id, "IND-27")
    _as(monkeypatch, "agent:gpt-reviewer", GPT)
    WorkItemModel.update_work_item(project_id=project.id, identifier="IND-27", status="in_progress")
    _as(monkeypatch, "agent:gemini-impl", GEMINI)
    WorkItemModel.update_work_item(project_id=project.id, identifier="IND-27", status="in_review")
    _as(monkeypatch, "agent:claude-reviewer", CLAUDE)
    WorkItemModel.review_transition(project.id, "IND-27", "adversarial_pass", "ok")
    evidence = _last_status_payload(project.id, "IND-27")["independence"]
    assert [p["lineage"] for p in evidence["implementers"]] == ["google"]


def test_a_broken_lineage_pattern_does_not_fail_writes(monkeypatch, project):
    with _conn() as conn:
        conn.execute(
            "INSERT INTO model_lineages (pattern, lineage, sort_order) VALUES ('(', 'x', 0)"
        )
        conn.commit()
    try:
        _as(monkeypatch, "agent:impl", GPT)
        WorkItemModel.file_work_item(
            project_id=project.id,
            identifier="IND-28",
            title="t",
            status="open",
            embedding=[0.0] * 768,
        )
        entity_id = WorkItemModel.get_work_item(project.id, "IND-28")["entity_id"]
        with _conn() as conn:
            create = kernel._get_entity_ops(conn, entity_id)[0]
        assert create["payload"]["producer"]["model"] == GPT
        assert create["payload"]["producer"]["lineage"] is None
    finally:
        with _conn() as conn:
            conn.execute("DELETE FROM model_lineages WHERE pattern = '('")
            conn.commit()


def test_seal_stubs_and_legacy_review_notes_are_bookkeeping():
    stub = {"op_type": "create", "actor_id": "agent:r", "payload": {"identifier": "X"}}
    assert _independence._is_bookkeeping(stub, None)
    review = {
        "op_type": "set_status",
        "actor_id": "agent:r",
        "payload": {"status": "in_human_review", "review_transition": "adversarial_pass"},
    }
    notes = {
        "op_type": "set_field",
        "actor_id": "agent:r",
        "payload": {"diagnostic_keys": {"review_notes": [{"note": "ok"}], "other": 1}},
    }
    assert _independence._is_bookkeeping(notes, review)
    assert not _independence._is_bookkeeping(notes, None)
    assert not _independence._is_bookkeeping({**notes, "actor_id": "agent:x"}, review)


def test_reconcile_cannot_carry_an_item_out_of_review(monkeypatch, project):
    _as(monkeypatch, "agent:impl", GPT)
    _to_review(project.id, "IND-29")
    entity_id = WorkItemModel.get_work_item(project.id, "IND-29")["entity_id"]
    with _conn() as conn:
        local = kernel._get_entity_ops(conn, entity_id)
        remote = {
            "op_id": "f" * 64,
            "entity_id": entity_id,
            "op_type": "set_status",
            "lamport": max(op["lamport"] for op in local) + 1,
            "payload": {"status": "done"},
            "parent_op_ids": [],
        }
        with pytest.raises(ValueError, match="after review completes"):
            kernel.reconcile_entity(conn, entity_id, [remote], actor_id="agent:impl")


# ---------------------------------------------------------------------------
# Round 2 review cases (GPT-6.1-sol, 2026-10-29)
# ---------------------------------------------------------------------------


def test_release_keeps_progress_when_the_lease_outlives_the_claim(monkeypatch, project):
    _as(monkeypatch, "agent:impl", GPT)
    WorkItemModel.file_work_item(
        project_id=project.id, identifier="IND-30", title="t", status="open", embedding=[0.0] * 768
    )
    WorkItemModel.claim_work_item(project.id, "IND-30")
    # The lifecycle has no claimed -> in_progress move; only a forced one
    # leaves the lease behind.
    WorkItemModel.update_work_item(
        project_id=project.id, identifier="IND-30", status="in_progress", force=True
    )
    assert WorkItemModel.release_work_item(project.id, "IND-30")["status"] == "in_progress"
    entity_id = WorkItemModel.get_work_item(project.id, "IND-30")["entity_id"]
    with _conn() as conn:
        leases = conn.execute(
            "SELECT count(*) AS n FROM work_item_leases WHERE entity_id = %s", (entity_id,)
        ).fetchone()["n"]
    assert leases == 0


def test_a_bounce_with_a_diagnostic_edit_counts_the_edit_as_work(monkeypatch, project):
    _as(monkeypatch, "agent:claude-impl", CLAUDE)
    _to_review(project.id, "IND-31")
    _as(monkeypatch, "agent:gpt-reviewer", GPT)
    diag = WorkItemModel.get_work_item(project.id, "IND-31")["diagnostic_keys"]
    WorkItemModel.update_work_item(
        project_id=project.id,
        identifier="IND-31",
        status="in_progress",
        diagnostic_keys={**diag, "review_notes": [{"note": "old"}], "fix": "applied"},
    )
    _as(monkeypatch, "agent:gemini-impl", GEMINI)
    WorkItemModel.update_work_item(project_id=project.id, identifier="IND-31", status="in_review")
    _as(monkeypatch, "agent:gpt-reviewer-2", "gpt-6-astra")
    with pytest.raises(IndependenceError, match="shares the reviewer's model lineage"):
        WorkItemModel.review_transition(project.id, "IND-31", "adversarial_pass", "ok")


def test_an_attempt_stays_stale_across_a_bounce(monkeypatch, project):
    _as(monkeypatch, "agent:codex", GPT)
    _to_review(project.id, "IND-32")
    attempt = _attempt(project.id, "IND-32")
    WorkItemModel.update_work_item(project_id=project.id, identifier="IND-32", body="changed")
    WorkItemModel.update_work_item(project_id=project.id, identifier="IND-32", status="in_progress")
    WorkItemModel.update_work_item(project_id=project.id, identifier="IND-32", status="in_review")
    with pytest.raises(IndependenceError, match="changed after review attempt"):
        WorkItemModel.review_transition(
            project.id, "IND-32", "adversarial_pass", "x", attempt_id=attempt
        )


def test_status_moves_alone_do_not_make_an_attempt_stale(monkeypatch, project):
    _as(monkeypatch, "agent:codex", GPT)
    WorkItemModel.file_work_item(
        project_id=project.id, identifier="IND-33", title="t", status="open", embedding=[0.0] * 768
    )
    WorkItemModel.update_work_item(project_id=project.id, identifier="IND-33", status="in_progress")
    attempt = _attempt(project.id, "IND-33")
    WorkItemModel.update_work_item(project_id=project.id, identifier="IND-33", status="in_review")
    WorkItemModel.review_transition(
        project.id, "IND-33", "adversarial_pass", "Claude accepted", attempt_id=attempt
    )
    assert _status(project.id, "IND-33") == "in_human_review"


def test_a_failing_session_lookup_does_not_fail_writes(monkeypatch, project):
    def broken(conn, env, cwd):
        conn.execute("SELECT 1 / 0")

    monkeypatch.setattr(_independence, "_session_model", broken)
    _as(monkeypatch, "agent:impl", GPT)
    WorkItemModel.file_work_item(
        project_id=project.id, identifier="IND-34", title="t", status="open", embedding=[0.0] * 768
    )
    entity_id = WorkItemModel.get_work_item(project.id, "IND-34")["entity_id"]
    with _conn() as conn:
        create = kernel._get_entity_ops(conn, entity_id)[0]
    assert create["payload"]["producer"]["model"] == GPT
    assert create["payload"]["producer"]["lineage"] is None


def test_reconcile_uses_the_folded_status(monkeypatch, project):
    """After a legitimate forced close, a harmless reconcile isn't refused."""
    _as(monkeypatch, "agent:impl", GPT)
    _to_review(project.id, "IND-35")
    _as(monkeypatch, "agent:reviewer", CLAUDE)
    WorkItemModel.close_work_item(project.id, "IND-35", force=True)
    entity_id = WorkItemModel.get_work_item(project.id, "IND-35")["entity_id"]
    with _conn() as conn:
        local = kernel._get_entity_ops(conn, entity_id)
        remote = {
            "op_id": "e" * 64,
            "entity_id": entity_id,
            "op_type": "set_field",
            "lamport": max(op["lamport"] for op in local) + 1,
            "payload": {"severity": "low"},
            "parent_op_ids": [],
        }
        kernel.reconcile_entity(conn, entity_id, [remote], actor_id="agent:reviewer")


def test_the_same_actor_can_claim_and_release_repeatedly(monkeypatch, project):
    """Op ids are content hashes: repeated claims and releases must not collide."""
    _as(monkeypatch, "agent:impl", GPT)
    WorkItemModel.file_work_item(
        project_id=project.id, identifier="IND-36", title="t", status="open", embedding=[0.0] * 768
    )
    for _ in range(2):
        assert WorkItemModel.claim_work_item(project.id, "IND-36")["status"] == "claimed"
        assert WorkItemModel.release_work_item(project.id, "IND-36")["status"] == "open"


def test_an_attempt_without_a_freshness_watermark_is_refused(monkeypatch, project):
    _as(monkeypatch, "agent:codex", GPT)
    _to_review(project.id, "IND-37")
    attempt = _attempt(project.id, "IND-37")
    with _conn() as conn:
        conn.execute(
            "UPDATE review_delegation_attempts SET item_lamport = NULL WHERE attempt_id = %s",
            (uuid.UUID(attempt),),
        )
        conn.commit()
    with pytest.raises(IndependenceError, match="predates freshness tracking"):
        WorkItemModel.review_transition(
            project.id, "IND-37", "adversarial_pass", "x", attempt_id=attempt
        )
