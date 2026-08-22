"""Read-only ambient v6 identity probe.

The probe is consumed by the suite genesis gate.  It resolves exactly the
same actor configuration as a write, but never constructs a face, opens a
connection, or appends an event.  Producer/model identity is owned by regista
and is therefore intentionally outside this component's probe.
"""

from __future__ import annotations

import sys
import traceback
from typing import Any

from agent_notes.core.actor import ActorConfigurationError, resolve_actor

PROBE_VERSION = 1
COMPONENT = "agent-notes"
CHECK_PREFIX = "agent_notes."
SESSION_IDENTITY_CHECK = "agent_notes.session_identity_resolvable"

REASONS: frozenset[str] = frozenset(
    {
        "resolved",
        "identity_not_configured",
        "identity_invalid",
        "probe_error",
    }
)


def _identity() -> tuple[bool, str, dict[str, Any]]:
    try:
        actor = resolve_actor()
    except ActorConfigurationError as exc:
        reason = (
            "identity_not_configured"
            if exc.code == "ACTOR_ID_NOT_CONFIGURED"
            else "identity_invalid"
        )
        return (
            False,
            reason,
            {
                "error_type": type(exc).__name__,
                "error_code": exc.code,
            },
        )
    except Exception as exc:
        return False, "identity_invalid", {"error_type": type(exc).__name__}
    return (
        True,
        "resolved",
        {
            "actor_id": actor.actor_id,
            "actor_kind": actor.actor_kind,
            "identity_source": "agent_notes.actor.resolve_actor",
        },
    )


def build_report() -> dict[str, Any]:
    ok, reason, evidence = _identity()
    details = {
        "resolved": "a v6 write would carry the configured canonical actor",
        "identity_not_configured": (
            "no canonical actor identity is configured; set AGENT_NOTES_ACTOR_ID "
            "or REGISTA_PRINCIPAL_ID"
        ),
        "identity_invalid": "the configured actor cannot be used for a v6 write",
    }
    check = {
        "id": SESSION_IDENTITY_CHECK,
        "status": "pass" if ok else "fail",
        "reason": reason,
        "detail": details[reason],
        "evidence": evidence,
    }
    return {
        "component": COMPONENT,
        "probe_version": PROBE_VERSION,
        "ok": ok,
        "checks": [check],
    }


def invariant_probe_report() -> dict[str, Any]:
    """Return a contract-shaped report and never raise."""

    try:
        return build_report()
    except Exception as exc:  # pragma: no cover - defensive process boundary
        traceback.print_exc(file=sys.stderr)
        return {
            "component": COMPONENT,
            "probe_version": PROBE_VERSION,
            "ok": False,
            "checks": [
                {
                    "id": SESSION_IDENTITY_CHECK,
                    "status": "fail",
                    "reason": "probe_error",
                    "detail": (
                        f"the session-identity probe raised {type(exc).__name__}; "
                        "see stderr for the traceback"
                    ),
                    "evidence": {"error_type": type(exc).__name__},
                }
            ],
        }


__all__ = [
    "CHECK_PREFIX",
    "COMPONENT",
    "PROBE_VERSION",
    "REASONS",
    "SESSION_IDENTITY_CHECK",
    "build_report",
    "invariant_probe_report",
]
