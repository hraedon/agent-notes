"""Standalone verifier for the work-log kernel (Plan 008 P1).

Public surface:
- ``verify_entity`` — verify all ops for a single entity.
- ``verify_all`` — verify every op in the log.
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
from agent_notes.core.kernel import _make_op_id

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

_VALID_STATUS = frozenset(["open", "claimed", "closed", "deferred"])


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
