"""Standalone verifier for the work-log kernel (Plan 008 P1).

Public surface:
- ``verify_entity`` — verify all ops for a single entity.
- ``verify_all`` — verify every op in the log.
- ``verify_cache`` — verify the ``work_items`` cache matches the op-log fold.
- ``verify_op_id`` — check that ``op_id`` matches the canonical hash.
- ``verify_signature`` — check DSSE envelope signature (if signed).
- ``verify_hash_chain`` — check parent references and acyclicity.
- ``apply_policy`` — run built-in policy rules against an op.

Design:
- The verifier is **standalone**; it reads from the DB but makes no writes.
- It can be called as a CLI gate step (sf2, CI, auditor) or imported as a
  library.
- P1 ships built-in Python policies; OPA/Rego integration is P1+ (optional
  capability behind the same interface).
- Public keys are loaded from a directory (``~/.config/agent-notes/keys/``)
  or passed explicitly.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field

import psycopg
from psycopg.rows import dict_row

from agent_notes.core.db import _conn
from agent_notes.core.envelope import verify_envelope
from agent_notes.core.kernel import (
    _get_entity_ops,
    _make_op_id,
    _resolve_status_lattice,
    fold_work_item_state,
)
from agent_notes.core.lifecycle import (
    ALL_VALID_STATES,
    is_terminal,
    transition_for,
)

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class Violation:
    """A single verification failure."""

    op_id: str
    rule: str
    message: str
    severity: str = "error"  # "error" | "warning"


@dataclass
class VerificationResult:
    """Aggregate result for a set of ops."""

    checked: int = 0
    passed: int = 0
    failed: int = 0
    violations: list[Violation] = field(default_factory=list)

    def ok(self) -> bool:
        return self.failed == 0


# ---------------------------------------------------------------------------
# Core checks
# ---------------------------------------------------------------------------


def verify_op_id(op: dict) -> Violation | None:
    """Check that ``op_id`` is the correct content hash of the canonical payload.

    Returns ``None`` if valid, else a ``Violation``.
    """
    entity_type = op["entity_type"]
    op_type = op["op_type"]
    # The stored payload contains the envelope; the inner payload (without
    # envelope) is what was hashed.  We reconstruct it by dropping the key.
    stored_payload = op.get("payload") or {}
    inner_payload = {k: v for k, v in stored_payload.items() if k != "envelope"}
    parent_op_ids = op.get("parent_op_ids") or []

    expected = _make_op_id(entity_type, op_type, inner_payload, parent_op_ids)
    if expected != op["op_id"]:
        return Violation(
            op_id=op["op_id"],
            rule="hash",
            message=f"op_id mismatch: expected {expected[:16]}…, got {op['op_id'][:16]}…",
        )
    return None


def verify_signature(op: dict, public_key: bytes | None = None) -> Violation | None:
    """Check DSSE envelope signature.

    If the envelope is unsigned (``NullSigner``), returns ``None`` without
    checking (P1 enforcement is opt-in per key).

    Migrated ops (actor_id == "migration") that lack an envelope are treated
    as warnings rather than errors — the migration script did not produce
    signing envelopes, but the hash-chain is verified separately by
    ``verify_op_id`` and the cache-rebuild match.
    """
    stored_payload = op.get("payload") or {}
    envelope = stored_payload.get("envelope")
    if not envelope:
        actor_id = op.get("actor_id")
        if actor_id == "migration":
            return Violation(
                op_id=op["op_id"],
                rule="signature",
                message=(
                    "Unsigned migration op (no signing envelope; hash-chain verified separately)"
                ),
                severity="warning",
            )
        return Violation(
            op_id=op["op_id"],
            rule="signature",
            message="Missing envelope in payload",
        )

    for sig_entry in envelope.get("signatures", []):
        if sig_entry.get("keyid") == "null":
            # Unsigned / placeholder — accepted if no public key is provided.
            if public_key is None:
                return None
            # If a key is provided, placeholder signatures are rejected.
            return Violation(
                op_id=op["op_id"],
                rule="signature",
                message="Placeholder signature but a public key was supplied for verification",
            )

    if public_key is None:
        # Signed by a real key, but we have no key to verify with.
        return Violation(
            op_id=op["op_id"],
            rule="signature",
            message="Signed by real key but no public key available for verification",
            severity="warning",
        )

    try:
        verify_envelope(envelope, public_key)
        return None
    except ValueError as exc:
        return Violation(
            op_id=op["op_id"],
            rule="signature",
            message=f"Signature verification failed: {exc}",
        )


def verify_hash_chain(op: dict, existing_op_ids: set[str]) -> Violation | None:
    """Check that every parent ``op_id`` references an op that already exists.

    For P1 (single-writer) we assume strict ordering; a parent must appear
    earlier in the log.  P2 relaxes this for merge.
    """
    parents = op.get("parent_op_ids") or []
    for parent in parents:
        if parent not in existing_op_ids:
            return Violation(
                op_id=op["op_id"],
                rule="parent",
                message=f"Parent op_id not found: {parent[:16]}…",
            )
    return None


# ---------------------------------------------------------------------------
# Built-in policy (P1)
# ---------------------------------------------------------------------------

_VALID_OP_TYPES = frozenset(
    [
        "create",
        "set_status",
        "set_field",
        "add_link",
        "remove_link",
        "claim",
        "release",
        "heartbeat",
        "request",
        "wait",
        "close",
        "snapshot",
        "merge",
    ]
)

_VALID_ENTITY_TYPES = frozenset(["work_item", "memory", "link"])

# Plan 013: the valid status set is defined once in ``lifecycle.ALL_VALID_STATES``
# (the union of canonical + legacy states). Mirrors the work_items_status CHECK
# constraint in 820_canonical_lifecycle.sql; cross-checked by
# test_lifecycle_module_matches_db_vocab.
_VALID_STATUS = ALL_VALID_STATES


def apply_policy(op: dict) -> Violation | None:
    """Run built-in policy rules against an op.

    P1 rules:
    1. op_type must be in the known set.
    2. entity_type must be in the known set.
    3. actor_id must be present (non-empty string).
    4. For set_status ops, status must be in the known set.
    """
    op_type = op.get("op_type")
    if op_type not in _VALID_OP_TYPES:
        return Violation(
            op_id=op["op_id"],
            rule="policy",
            message=f"Unknown op_type: {op_type!r}",
        )

    entity_type = op.get("entity_type")
    if entity_type not in _VALID_ENTITY_TYPES:
        return Violation(
            op_id=op["op_id"],
            rule="policy",
            message=f"Unknown entity_type: {entity_type!r}",
        )

    actor_id = op.get("actor_id")
    if not actor_id:
        return Violation(
            op_id=op["op_id"],
            rule="policy",
            message="Missing actor_id",
        )

    if op_type == "set_status":
        status = (op.get("payload") or {}).get("status")
        if status not in _VALID_STATUS:
            return Violation(
                op_id=op["op_id"],
                rule="policy",
                message=f"Invalid status in set_status: {status!r}",
            )

    return None


# ---------------------------------------------------------------------------
# Entity-level verification
# ---------------------------------------------------------------------------


def verify_entity(
    entity_id: str,
    conn: psycopg.Connection | None = None,
    public_key: bytes | None = None,
    check_policy: bool = True,
) -> VerificationResult:
    """Verify all ops for a single entity, ordered by lamport."""
    if conn is None:
        with _conn() as conn:
            return _verify_entity(conn, entity_id, public_key, check_policy)
    return _verify_entity(conn, entity_id, public_key, check_policy)


def _verify_entity(
    conn: psycopg.Connection,
    entity_id: str,
    public_key: bytes | None = None,
    check_policy: bool = True,
) -> VerificationResult:
    """Internal: verify all ops for a single entity."""
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        """
        SELECT op_id, entity_type, op_type, lamport, payload, parent_op_ids, actor_id
        FROM op_log
        WHERE entity_id = %s
        ORDER BY lamport ASC, op_id ASC
        """,
        (entity_id,),
    )
    ops = [dict(r) for r in cur.fetchall()]
    return _verify_ops(ops, public_key, check_policy)


def _verify_ops(
    ops: list[dict],
    public_key: bytes | None = None,
    check_policy: bool = True,
) -> VerificationResult:
    result = VerificationResult()
    seen_op_ids: set[str] = set()

    for op in ops:
        result.checked += 1
        violations: list[Violation] = []

        v = verify_op_id(op)
        if v:
            violations.append(v)

        v = verify_hash_chain(op, seen_op_ids)
        if v:
            violations.append(v)

        v = verify_signature(op, public_key)
        if v:
            violations.append(v)

        if check_policy:
            v = apply_policy(op)
            if v:
                violations.append(v)

        if violations:
            has_error = any(v.severity == "error" for v in violations)
            if has_error:
                result.failed += 1
            else:
                result.passed += 1
            result.violations.extend(violations)
        else:
            result.passed += 1

        seen_op_ids.add(op["op_id"])

    return result


# ---------------------------------------------------------------------------
# Global verification
# ---------------------------------------------------------------------------


def verify_all(
    public_key: bytes | None = None,
    check_policy: bool = True,
    entity_type: str | None = None,
) -> VerificationResult:
    """Verify every op in the log.

    If ``entity_type`` is given, only verify ops of that type.
    """
    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        if entity_type:
            cur.execute(
                """
                SELECT op_id, entity_type, op_type, lamport, payload, parent_op_ids, actor_id
                FROM op_log
                WHERE entity_type = %s
                ORDER BY lamport ASC, op_id ASC
                """,
                (entity_type,),
            )
        else:
            cur.execute(
                """
                SELECT op_id, entity_type, op_type, lamport, payload, parent_op_ids, actor_id
                FROM op_log
                ORDER BY lamport ASC, op_id ASC
                """
            )
        ops = [dict(r) for r in cur.fetchall()]
        return _verify_ops(ops, public_key, check_policy)


# ---------------------------------------------------------------------------
# Cache verification
# ---------------------------------------------------------------------------

# Fields compared between the folded state and the live cache row.
# ``embedding`` is excluded (it may be recomputed independently) and
# ``updated_at`` is excluded (timestamp drift is expected).  ``created_at`` and
# ``closed_at`` are maintained by triggers, not the fold, so they are excluded.
_CACHE_COMPARE_FIELDS = (
    "project_id",
    "identifier",
    "title",
    "body_hash",
    "kind",
    "status",
    "severity",
    "external_refs",
    "diagnostic_keys",
    "frontmatter_version",
)


def verify_cache(
    entity_id: str | None = None,
    conn: psycopg.Connection | None = None,
) -> VerificationResult:
    """Verify that the ``work_items`` cache matches what the op-log fold produces.

    For each work_item entity in ``op_log`` (or just ``entity_id`` when given),
    fold the entity in-memory via :func:`fold_work_item_state` and compare the
    result against the current ``work_items`` cache row.  Any difference is
    reported as a ``cache``-rule violation.

    ``embedding`` and ``updated_at`` are excluded from the comparison: the
    embedding may be recomputed independently of the op-log, and timestamp drift
    on ``updated_at`` is expected.

    Returns a :class:`VerificationResult` with the count of checked / passed /
    failed entities.
    """
    if conn is None:
        with _conn() as conn:
            return _verify_cache(conn, entity_id)
    return _verify_cache(conn, entity_id)


def _verify_cache(conn: psycopg.Connection, entity_id: str | None) -> VerificationResult:
    result = VerificationResult()

    cur = conn.cursor(row_factory=dict_row)
    if entity_id is None:
        # UNION of op_log and work_items entity_ids catches both:
        # - entities with ops but no cache row (dangling)
        # - entities with cache row but no ops (orphan/stale)
        cur.execute(
            """
            SELECT entity_id FROM op_log WHERE entity_type = 'work_item'
            UNION
            SELECT entity_id FROM work_items
            ORDER BY entity_id
            """
        )
    else:
        cur.execute(
            "SELECT entity_id FROM op_log "
            "WHERE entity_type = 'work_item' AND entity_id = %s "
            "UNION "
            "SELECT entity_id FROM work_items WHERE entity_id = %s "
            "ORDER BY entity_id",
            (entity_id, entity_id),
        )
    entity_ids = [r["entity_id"] for r in cur.fetchall()]

    for eid in entity_ids:
        result.checked += 1

        folded = fold_work_item_state(conn, eid)

        # Fetch the live cache row (if any).
        ccur = conn.cursor(row_factory=dict_row)
        ccur.execute("SELECT * FROM work_items WHERE entity_id = %s", (eid,))
        row = ccur.fetchone()

        if folded is None:
            # Dangling entity (no create op) — no cache row should exist.
            if row is not None:
                result.failed += 1
                result.violations.append(
                    Violation(
                        op_id=eid,
                        rule="cache",
                        message="Dangling entity (no create op) has a stale cache row",
                    )
                )
            else:
                result.passed += 1
            continue

        if row is None:
            result.failed += 1
            result.violations.append(
                Violation(
                    op_id=eid,
                    rule="cache",
                    message="Entity has ops but no cache row in work_items",
                )
            )
            continue

        # Compare each comparable field.
        diffs: list[tuple[str, object, object]] = []
        for field_name in _CACHE_COMPARE_FIELDS:
            folded_val = folded.get(field_name)
            cache_val = row.get(field_name)
            if folded_val != cache_val:
                diffs.append((field_name, folded_val, cache_val))

        if diffs:
            result.failed += 1
            for field_name, folded_val, cache_val in diffs:
                result.violations.append(
                    Violation(
                        op_id=eid,
                        rule="cache",
                        message=(
                            f"Cache mismatch for field {field_name!r}: "
                            f"folded={folded_val!r} cache={cache_val!r}"
                        ),
                    )
                )
        else:
            result.passed += 1

    return result


# ---------------------------------------------------------------------------
# Gate-integrity verification (Plan 014 WI-3)
# ---------------------------------------------------------------------------

# The cross-lineage review gate (Invariant G) is: work may reach ``done`` only
# after an adversarial pass (``in_review → in_human_review``) followed by
# ``accept`` (``in_human_review → done``). On the regista path regista's own
# validators enforce this; on the native (degrade) path WI-1/WI-2 make
# unilateral completion impossible *except* via ``force=True``. This detector
# surfaces any terminal work item whose op-chain reached a terminal state
# without the gate having run — i.e. degraded / unverified completions.

# The transition names that matter (mirrors ``lifecycle.VALID_TRANSITIONS``).
# The adversarial pass is the cross-lineage check: in_review → in_human_review.
_ADVERSARIAL_PASS = ("in_review", "in_human_review")


def _status_from_op(op: dict) -> str | None:
    """Return the status an op sets (``close`` → ``closed``), or ``None``."""
    if op["op_type"] == "close":
        return "closed"
    return (op.get("payload") or {}).get("status")


def _canonical(status: str | None) -> str | None:
    """Map ``closed`` (legacy terminal) to ``done`` for transition lookups."""
    if status is None:
        return None
    return "done" if status == "closed" else status


def _reconstruct_status_transitions(
    ops: list[dict],
) -> tuple[list[tuple[str | None, str, dict]], str | None]:
    """Ordered list of ``(old, new, op)`` status transitions from the op chain.

    Groups ops by ``lamport`` (mirroring :func:`fold_work_item_state`) and
    resolves concurrent status ops via the fail-safe lattice, so the
    reconstructed chain matches the folded status. Non-status ops (create,
    set_field, snapshot) are applied for their side effects (create sets the
    initial status). Returns ``(transitions, final_status)``.
    """
    groups: list[list[dict]] = []
    current_lamport: int | None = None
    current_group: list[dict] = []
    for op in ops:
        if op["lamport"] != current_lamport:
            if current_group:
                groups.append(current_group)
            current_lamport = op["lamport"]
            current_group = [op]
        else:
            current_group.append(op)
    if current_group:
        groups.append(current_group)

    transitions: list[tuple[str | None, str, dict]] = []
    status: str | None = None
    for group in groups:
        status_ops = [o for o in group if o["op_type"] in ("set_status", "close")]
        if not status_ops:
            for o in group:
                if o["op_type"] == "create":
                    status = (o.get("payload") or {}).get("status", "open")
            continue
        new_status = _resolve_status_lattice(status_ops, status)
        if new_status is not None and new_status != status:
            winning_op = _winning_status_op(status_ops, new_status)
            transitions.append((status, new_status, winning_op))
            status = new_status
    return transitions, status


def _winning_status_op(status_ops: list[dict], winning_status: str) -> dict:
    """Find the op that produced ``winning_status`` in a concurrent group.

    Ops are already ordered by ``op_id`` within a lamport group; the first
    op setting the winning status is the lattice winner (ties break by
    lexicographically smaller ``op_id``, which is the iteration order).
    """
    for op in status_ops:
        if _status_from_op(op) == winning_status:
            return op
    return status_ops[-1]


def _has_gate_attestation(folded: dict) -> bool:
    """Return ``True`` if the folded state carries a WI-4 gate-waiver attestation."""
    diag = folded.get("diagnostic_keys") or {}
    return isinstance(diag, dict) and "gate_attestation" in diag


def _check_degraded_completion(
    entity_id: str, ops: list[dict], folded: dict
) -> Violation | None:
    """Return a ``gate`` warning if the item is a degraded completion.

    A degraded completion is a terminal work item (``done``/``closed``) whose
    op-chain reached a terminal state without the cross-lineage review gate
    having run. The review-exempt dismissal (``close_from_open``: ``open →
    done``) is NOT flagged — it declines work (won't-fix/duplicate) rather
    than completing it. Items carrying a ``gate_attestation`` (Plan 014 WI-4)
    are skipped: an operator has explicitly waived the gate.
    """
    status = folded.get("status")
    if not is_terminal(status):
        return None

    if _has_gate_attestation(folded):
        return None

    transitions, _ = _reconstruct_status_transitions(ops)
    if not transitions:
        # Created terminal with no status-changing op — treat as a dismissal,
        # not a degraded completion (the work was never started through a gate).
        return None

    # Find the last transition INTO a terminal state (the current completion).
    terminal_idx: int | None = None
    for i in range(len(transitions) - 1, -1, -1):
        if is_terminal(transitions[i][1]):
            terminal_idx = i
            break
    if terminal_idx is None:
        return None

    old, new, op = transitions[terminal_idx]

    # A ``close`` op always bypasses the gate (force-close escape hatch, WI-1).
    if op["op_type"] == "close":
        return Violation(
            op_id=entity_id,
            rule="gate",
            message=(
                f"Degraded completion: terminal via force-close op "
                f"({_canonical(old)}→{new}) without the review gate. "
                "Run 'agent-notes verify attest-gate' to record a waiver."
            ),
            severity="warning",
        )

    old_c = _canonical(old)
    new_c = _canonical(new)
    try:
        tname = transition_for(old_c or "", new_c or "")
    except ValueError:
        tname = None

    if tname == "close_from_open":
        return None  # review-exempt dismissal — not a degraded completion

    if tname == "accept":
        # Accept is only legitimate if an adversarial pass preceded it.
        prefix = transitions[:terminal_idx]
        if any(o == _ADVERSARIAL_PASS[0] and n == _ADVERSARIAL_PASS[1] for (o, n, _) in prefix):
            return None
        return Violation(
            op_id=entity_id,
            rule="gate",
            message=(
                "Degraded completion: reached 'done' via accept without a "
                "preceding adversarial_pass (in_review→in_human_review)."
            ),
            severity="warning",
        )

    return Violation(
        op_id=entity_id,
        rule="gate",
        message=(
            f"Degraded completion: terminal via {tname or 'invalid'} transition "
            f"({old_c}→{new_c}) without the review gate."
        ),
        severity="warning",
    )


def verify_gate_integrity(
    entity_id: str | None = None,
    conn: psycopg.Connection | None = None,
) -> VerificationResult:
    """Flag terminal work items completed without the cross-lineage review gate.

    For each work_item entity that has ops in ``op_log`` and is currently
    terminal (``done``/``closed``), reconstruct the status-transition chain
    and surface a ``gate`` warning when the terminal state was reached without
    a preceding ``adversarial_pass`` (the cross-lineage review gate).

    Regista-path items (projection-only, no ops in ``op_log``) are skipped —
    regista's own validators enforce the gate by construction, so there is
    nothing to detect. This makes the detector naturally scoped to the native
    (degrade) write path, which is where degraded completions originate.

    Violations are warnings (severity ``"warning"``): a force-close is a
    legitimate admin action and the op-log is not corrupt; the detector
    surfaces provenance weakness for visibility, not as a hard integrity error.
    """
    if conn is None:
        with _conn() as conn:
            return _verify_gate_integrity(conn, entity_id)
    return _verify_gate_integrity(conn, entity_id)


def _verify_gate_integrity(
    conn: psycopg.Connection, entity_id: str | None
) -> VerificationResult:
    result = VerificationResult()
    cur = conn.cursor(row_factory=dict_row)
    if entity_id is None:
        cur.execute(
            """
            SELECT entity_id FROM op_log
            WHERE entity_type = 'work_item'
            GROUP BY entity_id
            ORDER BY entity_id
            """
        )
        entity_ids = [r["entity_id"] for r in cur.fetchall()]
    else:
        entity_ids = [entity_id]

    for eid in entity_ids:
        ops = _get_entity_ops(conn, eid)
        if not ops:
            continue
        folded = fold_work_item_state(conn, eid)
        if folded is None:
            continue
        result.checked += 1
        v = _check_degraded_completion(eid, ops, folded)
        if v is not None:
            result.violations.append(v)
        else:
            result.passed += 1

    return result


# ---------------------------------------------------------------------------
# Key management
# ---------------------------------------------------------------------------


def load_public_key(key_id: str) -> bytes | None:
    """Load a public key from the key store by its key_id (first 16 hex chars).

    Key store: ``~/.config/agent-notes/keys/<key_id>.pub``
    """
    import os
    from pathlib import Path

    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    key_dir = Path(base).expanduser() / "agent-notes" / "keys"
    key_file = key_dir / f"{key_id}.pub"
    if key_file.is_file():
        return base64.b64decode(key_file.read_text().strip())
    return None


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------


def verify_with_auto_key(
    entity_id: str | None = None,
    public_key: bytes | None = None,
    check_policy: bool = True,
) -> VerificationResult:
    """High-level entry: verify all or one entity, auto-loading key if needed.

    If ``public_key`` is not given, we try to load the key from the first
    non-null signature we encounter.
    """
    if entity_id is None:
        return verify_all(public_key=public_key, check_policy=check_policy)
    with _conn() as conn:
        return verify_entity(conn, entity_id, public_key=public_key, check_policy=check_policy)
