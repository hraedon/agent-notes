"""The CLI error-envelope vocabulary (Plan 026 D3).

``ErrorCode`` is the closed set of ``error.code`` values agent-notes puts in
the suite CLI contract v1 error envelope (``cli.common.emit_error``). Skills,
hooks and other suite tools match on these strings, so a value here is a wire
contract: renaming or removing one is a breaking change. Values equal their
names, and ``ErrorCode`` is a ``StrEnum``, so a member serializes to exactly
the same JSON as the string literal it stands for.

``AgentNotesError`` is the coded exception, vendored from regista 0.7.2's
``RegistaError`` (the same ``code``/``message``/``detail`` fields and the same
``[CODE] message`` rendering) when the ``regista-hraedon`` dependency was
dropped.

``tests/test_error_envelope.py`` pins the envelope bytes for one sample per
code, and fails if a string code used in ``src/`` is missing here.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    # Configuration and identity, raised before any write.
    ACTOR_ID_NOT_CONFIGURED = "ACTOR_ID_NOT_CONFIGURED"
    PRINCIPAL_ID_NOT_CANONICAL = "PRINCIPAL_ID_NOT_CANONICAL"
    PRINCIPAL_ID_UNGRAMMATICAL = "PRINCIPAL_ID_UNGRAMMATICAL"
    RETIRED_SETTING = "RETIRED_SETTING"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    STORE_NOT_CONFIGURED = "STORE_NOT_CONFIGURED"
    STORE_UNAVAILABLE = "STORE_UNAVAILABLE"
    ENGINE_UNAVAILABLE = "ENGINE_UNAVAILABLE"
    MANIFEST_UNAVAILABLE = "MANIFEST_UNAVAILABLE"
    # Scope resolution.
    PROJECT_NOT_RESOLVED = "PROJECT_NOT_RESOLVED"
    WORKSPACE_NOT_FOUND = "WORKSPACE_NOT_FOUND"
    VM_ID_UNRESOLVED = "VM_ID_UNRESOLVED"
    VM_NOT_RESOLVED = "VM_NOT_RESOLVED"
    # Arguments and input.
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    FLAG_CONFLICT = "FLAG_CONFLICT"
    FLAG_MISSING = "FLAG_MISSING"
    FILE_UNREADABLE = "FILE_UNREADABLE"
    MODEL_INVALID = "MODEL_INVALID"
    REVISION_INVALID = "REVISION_INVALID"
    HEAD_NOT_PUSHED = "HEAD_NOT_PUSHED"
    # Operation results.
    NOT_FOUND = "NOT_FOUND"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    OPERATION_FAILED = "OPERATION_FAILED"
    CHECKOUT_CONFLICT = "CHECKOUT_CONFLICT"
    # `memory add` near-duplicate gate (Plan 027 WI-1.1); detail lists candidates.
    POSSIBLE_DUPLICATE = "POSSIBLE_DUPLICATE"
    # Sessions and messaging.
    NO_SESSION = "NO_SESSION"
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    AMBIGUOUS_SESSION = "AMBIGUOUS_SESSION"
    AWAIT_TIMEOUT = "AWAIT_TIMEOUT"


class AgentNotesError(Exception):
    """A coded failure carrying an optional structured ``detail`` dict."""

    def __init__(self, code: ErrorCode, message: str, detail: dict[str, Any] | None = None) -> None:
        self.code = code
        self.message = message
        self.detail = detail
        super().__init__(str(self))

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


__all__ = ["AgentNotesError", "ErrorCode"]
