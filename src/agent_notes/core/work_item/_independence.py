"""Reviewer independence on the native review gate (Plan 026 D2).

Every authored work-item op carries a ``producer`` block inside its payload, so
the op hash covers it: the harness, model and coarse vendor lineage of the
process that wrote it (D2.1, D2.2). A positive review verdict, or any other
completion out of review, is refused when the reviewer shares a lineage with
anyone who worked on the item since it last bounced back from review (D2.3 to
D2.5).

This is cooperative enforcement, not authentication: actor and model identity
are self-asserted through the environment and the harness hooks. It stops an
agent from approving its own work by accident or habit, and records what
happened when someone overrides it.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Mapping
from typing import Any

import psycopg
from psycopg.rows import dict_row

from agent_notes.core import kernel

MODEL_ENVS = ("AGENT_NOTES_MODEL", "REGISTA_PRODUCER_MODEL")
HARNESS_ENVS = ("AGENT_NOTES_HARNESS", "REGISTA_PRODUCER_HARNESS")

# Moving an item out of review upward (to the human gate or to a terminal
# state, ``closed`` included) is a positive verdict and runs the check
# (D2.4), whichever command does it and whether or not it's forced. Every
# other move, including closing an item that never entered review (Plan 025
# D7), stays ungated.
REVIEW_STATES = frozenset({"in_review", "in_human_review"})
_ADVANCED = frozenset({"in_human_review", "done", "closed"})
POSITIVE_TRANSITIONS = frozenset({"adversarial_pass", "accept"})
_RESET_TRANSITIONS = frozenset({"reject", "request_changes"})
_AUTHORED_OPS = frozenset({"create", "set_field", "set_status", "claim", "close", "merge"})
# Authored ops that change the item's content, as opposed to its status: work
# after a delegated review was requested makes that review stale (D2.7).
_CONTENT_OPS = frozenset({"create", "set_field", "merge"})


def gated(old_status: str, new_status: str) -> bool:
    """True when ``old -> new`` approves review work."""
    return old_status in REVIEW_STATES and new_status in _ADVANCED and new_status != old_status


def review_transition_for(old_status: str, new_status: str) -> str | None:
    """The review verdict a status move out of review amounts to, if any."""
    if old_status not in REVIEW_STATES or new_status == old_status:
        return None
    if new_status == "in_human_review":
        return "adversarial_pass"
    if new_status in ("done", "closed"):
        return "accept"
    if new_status == "in_progress":
        return "reject" if old_status == "in_human_review" else "request_changes"
    return None


class IndependenceError(ValueError):
    """The reviewer is not independent of the work it would approve."""


def lineage_for(conn: psycopg.Connection, value: str | None) -> str | None:
    """Map a model or lineage string to its vendor lineage (``model_lineages``)."""
    if not value or not value.strip():
        return None
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        """
        SELECT lineage FROM model_lineages
        WHERE lower(%s) ~* pattern
        ORDER BY sort_order, pattern
        LIMIT 1
        """,
        (value.strip(),),
    )
    row = cur.fetchone()
    return row["lineage"] if row is not None else None


def _first(env: Mapping[str, str], names: tuple[str, ...]) -> str | None:
    for name in names:
        value = env.get(name)
        if value and value.strip():
            return value.strip()
    return None


def _session_model(
    conn: psycopg.Connection, env: Mapping[str, str], cwd: str | None
) -> tuple[str | None, str | None, str | None]:
    """``(session_key, harness, model)`` for the calling session, or Nones."""
    from agent_notes.core import messaging

    try:
        session = messaging.resolve_session(cwd=cwd or os.getcwd(), env=env)
    except Exception:  # noqa: BLE001 - stamping must never fail a write
        return None, None, None
    if session is None:
        return None, None, None
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        "SELECT model FROM agent_sessions WHERE session_key = %s",
        (session.session_key,),
    )
    row = cur.fetchone()
    model = row["model"] if row is not None else None
    return session.session_key, session.harness, model


def resolve_producer(
    conn: psycopg.Connection,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
) -> dict[str, Any]:
    """The running process's producer block (D2.1). Never raises.

    Order: an explicit model in the environment, then the calling session's
    registered model, then unknown. The lineage is always derived from the
    model, never declared separately: a declared lineage that disagrees with
    the model would be a false identity claim. The lookups run in a savepoint,
    so a failure leaves the caller's transaction usable and the producer's
    lineage unknown; a reviewer with an unknown lineage is then refused by
    :func:`check`.
    """
    env = os.environ if env is None else env
    model = _first(env, MODEL_ENVS)
    producer: dict[str, Any] = {
        "harness": _first(env, HARNESS_ENVS),
        "model": model,
        "lineage": None,
        "session_key": None,
        "source": "env" if model else "unknown",
    }
    try:
        with conn.transaction():
            session_key, session_harness, session_model = _session_model(conn, env, cwd)
            producer["session_key"] = session_key
            producer["harness"] = producer["harness"] or session_harness
            if model is None and session_model:
                producer["model"] = session_model
                producer["source"] = "session"
            producer["lineage"] = lineage_for(conn, producer["model"])
    except Exception:  # noqa: BLE001 - stamping must never fail a write
        producer["lineage"] = None
    return producer


def authored(payload: dict[str, Any], producer: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``payload`` with the producer block stamped in."""
    return {**payload, "producer": dict(producer)}


def _is_bookkeeping(op: Mapping[str, Any], prev: Mapping[str, Any] | None) -> bool:
    """Review records, gate waivers and seal scaffolding: not implementation work."""
    payload = op.get("payload") or {}
    if payload.get("review_transition"):
        return True
    if op["op_type"] == "create" and "title" not in payload:
        # A seal stub (``_seal.seal``): written by whoever first touches a
        # chainless legacy item, and it carries no content.
        return True
    if op["op_type"] != "set_field":
        return False
    fields = {k for k in payload if k not in ("envelope", "producer")}
    diag = payload.get("diagnostic_keys")
    if fields != {"diagnostic_keys"} or not isinstance(diag, Mapping):
        return False
    if set(diag) == {"gate_attestation"}:
        return True
    # Before D2 the review-notes field op carried no marker. It is the op
    # directly after its review status op, by the same actor. Ops written
    # since D2 always carry a producer and mark review notes explicitly, so
    # this heuristic never applies to them.
    if "producer" in payload:
        return False
    prev_payload = (prev or {}).get("payload") or {}
    return (
        "review_notes" in diag
        and prev is not None
        and prev["op_type"] == "set_status"
        and bool(prev_payload.get("review_transition"))
        and prev.get("actor_id") == op.get("actor_id")
    )


def _ops(conn: psycopg.Connection, entity_id: str) -> list[dict[str, Any]]:
    """The item's ops in fold order, with their authors."""
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        """
        SELECT op_type, actor_id, payload, created_at, lamport
        FROM op_log
        WHERE entity_id = %s
        ORDER BY lamport ASC, op_id ASC
        """,
        (entity_id,),
    )
    return [dict(r) for r in cur.fetchall()]


def _work_ops(conn: psycopg.Connection, entity_id: str) -> list[dict[str, Any]]:
    """Authored implementation ops since the item last bounced from review (D2.3)."""
    work: list[dict[str, Any]] = []
    prev: dict[str, Any] | None = None
    for op in _ops(conn, entity_id):
        payload = op.get("payload") or {}
        if op["op_type"] == "set_status" and payload.get("review_transition") in (
            _RESET_TRANSITIONS
        ):
            work.clear()
        elif op["op_type"] in _AUTHORED_OPS and not _is_bookkeeping(op, prev):
            # Every author counts, services included: one that reported no
            # model falls under the legacy rule (a different actor reviews).
            if op.get("actor_id"):
                work.append(op)
        prev = op
    return work


def _content_edits(conn: psycopg.Connection, entity_id: str) -> list[dict[str, Any]]:
    """Every content edit in the item's whole history, bounces included."""
    edits: list[dict[str, Any]] = []
    prev: dict[str, Any] | None = None
    for op in _ops(conn, entity_id):
        if op["op_type"] in _CONTENT_OPS and not _is_bookkeeping(op, prev):
            edits.append(op)
        prev = op
    return edits


def implementers(conn: psycopg.Connection, entity_id: str) -> list[dict[str, Any]]:
    """Everyone who authored work on the item since it last bounced from review."""
    found: dict[tuple[str, str | None], dict[str, Any]] = {}
    for op in _work_ops(conn, entity_id):
        producer = (op.get("payload") or {}).get("producer")
        producer = producer if isinstance(producer, Mapping) else {}
        lineage = producer.get("lineage")
        found.setdefault(
            (op["actor_id"], lineage),
            {"actor_id": op["actor_id"], "lineage": lineage, "model": producer.get("model")},
        )
    return list(found.values())


def process_reviewer(conn: psycopg.Connection, actor_id: str) -> dict[str, Any]:
    """The running process as reviewer."""
    return {"actor_id": actor_id, **resolve_producer(conn)}


def attempt_reviewer(
    conn: psycopg.Connection,
    attempt_id: str,
    *,
    project_id: int,
    entity_id: str,
) -> dict[str, Any]:
    """A delegated review attempt as reviewer (D2.7).

    The attempt must be a validated ``accept`` verdict for this exact item,
    from a reviewer whose identity wasn't degraded. It's consumed when the
    transition commits (:func:`consume_attempt`), so it can't be replayed.
    """
    from agent_notes.core.work_item import _review_journal

    try:
        key = uuid.UUID(str(attempt_id))
    except ValueError as exc:
        raise IndependenceError(f"--attempt {attempt_id!r} is not a review attempt id") from exc
    row = _review_journal.get_attempt(conn, key)
    if row is None:
        raise IndependenceError(f"review attempt {attempt_id} not found")
    if row["project_id"] != project_id or row["work_item_entity_id"] != entity_id:
        raise IndependenceError(f"review attempt {attempt_id} was for a different work item")
    if row["status"] != "result_validated":
        raise IndependenceError(
            f"review attempt {attempt_id} is {row['status']}, not result_validated "
            "(attempts can be applied once)"
        )
    assurance = row.get("identity_assurance")
    if assurance not in ("attested", "asserted"):
        raise IndependenceError(
            f"review attempt {attempt_id} has {assurance or 'no'} reviewer identity assurance"
        )
    # Work items don't record git heads, so a content edit after the review
    # was requested is the staleness signal (D2.7). The lamport watermark is
    # recorded when the attempt is planned (migration 860).
    watermark = row.get("item_lamport")
    if watermark is None:
        raise IndependenceError(
            f"review attempt {attempt_id} predates freshness tracking, so it can't be "
            "shown to cover the item as it is now; request a fresh review"
        )
    if any(op["lamport"] > watermark for op in _content_edits(conn, entity_id)):
        raise IndependenceError(
            f"the item changed after review attempt {attempt_id} was requested; "
            "request a fresh review"
        )
    verdict = _artifact_verdict(conn, row.get("artifact_digest"))
    if verdict != "accept":
        raise IndependenceError(
            f"review attempt {attempt_id} has verdict {verdict or 'unknown'!r}; only an "
            "accept verdict can pass or accept an item"
        )
    model = row.get("reported_model_identity") or row["model_identity"]
    return {
        "actor_id": f"attempt:{key}",
        "harness": row["harness"],
        "model": model,
        "lineage": lineage_for(conn, model),
        "session_key": None,
        "source": "attempt",
        "attempt_id": str(key),
        "identity_assurance": assurance,
        "head_sha": row["head_sha"],
    }


def _artifact_verdict(conn: psycopg.Connection, digest: str | None) -> str | None:
    import json

    if not digest:
        return None
    text = kernel.get_blob(conn, digest)
    if not text:
        return None
    try:
        artifact = json.loads(text)
    except ValueError:
        return None
    if not isinstance(artifact, Mapping):
        return None
    result = artifact.get("result")
    verdict = artifact.get("verdict")
    if verdict is None and isinstance(result, Mapping):
        verdict = result.get("verdict")
    return verdict if isinstance(verdict, str) else None


def consume_attempt(
    conn: psycopg.Connection, reviewer: Mapping[str, Any], transition: str, op_id: str
) -> None:
    """Mark a delegated attempt applied, in the caller's transaction."""
    attempt_id = reviewer.get("attempt_id")
    if not attempt_id:
        return
    from agent_notes.core.work_item import _review_journal

    key = uuid.UUID(attempt_id)
    _review_journal.mark_transition_pending(
        conn, key, intended_transition=transition, native_op_id=op_id
    )
    _review_journal.mark_transition_applied(conn, key)


def _describe(person: Mapping[str, Any]) -> str:
    detail = person.get("model") or person.get("lineage")
    return f"{person['actor_id']} ({detail})" if detail else str(person["actor_id"])


def check(
    conn: psycopg.Connection,
    entity_id: str,
    reviewer: Mapping[str, Any],
    *,
    acknowledgement: str | None = None,
) -> dict[str, Any]:
    """Refuse a non-independent reviewer; return the evidence to record (D2.5, D2.6)."""
    people = implementers(conn, entity_id)
    evidence: dict[str, Any] = {"reviewer": dict(reviewer), "implementers": people}
    if acknowledgement is not None:
        if not acknowledgement.strip():
            raise IndependenceError("--same-lineage-acknowledged needs a reason")
        evidence.update(independence="acknowledged", acknowledgement=acknowledgement.strip())
        return evidence

    actor = reviewer.get("actor_id")
    hatch = (
        "Have a reviewer from another model family do it (or apply a delegated "
        "review with --attempt), or pass --same-lineage-acknowledged REASON."
    )
    if isinstance(actor, str) and actor.startswith("human:"):
        clash = [p for p in people if p["actor_id"] == actor]
        if clash:
            raise IndependenceError(
                f"{actor} worked on this item and can't also review it. {hatch}"
            )
        evidence["independence"] = "human"
        return evidence

    lineage = reviewer.get("lineage")
    if not lineage:
        raise IndependenceError(
            "the reviewer's model lineage is unknown, so independence can't be checked. "
            "Set AGENT_NOTES_MODEL (for example claude-opus-5-5) in the reviewing "
            "process, or apply a delegated review with --attempt."
        )
    unverified = False
    for person in people:
        if person["lineage"] is not None:
            if person["lineage"] == lineage:
                raise IndependenceError(
                    f"{_describe(person)} worked on this item and shares the reviewer's "
                    f"model lineage ({lineage}). {hatch}"
                )
            continue
        unverified = True
        if person["actor_id"] == actor:
            raise IndependenceError(
                f"{actor} worked on this item (model unknown) and can't also review it. {hatch}"
            )
    if unverified:
        evidence["independence"] = "unverified-legacy"
    else:
        evidence["independence"] = "verified" if people else "no-recorded-implementers"
    return evidence
