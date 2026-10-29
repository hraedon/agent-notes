"""Resolve the canonical actor for agent-notes writes.

Every authored write carries one identity: a canonical principal id.  Actor
resolution is trust-rooted in operator configuration, never in command
arguments or prompt input.  The tool-specific actor setting wins over the
suite setting, and the suite's canonical principal is the fallback:

``AGENT_NOTES_ACTOR_ID`` > ``REGISTA_PRINCIPAL_ID``

Both values use the normal process-env / per-user suite.env / system suite.env
layering.  A missing or non-canonical value fails closed before a write is
attempted.

The principal grammar below was vendored from regista 0.7.2
(``regista._principals``) when the dependency was dropped (Plan 026 D3).  It is
a wire contract: ids that validate today must keep validating, and refusals
keep their codes, reasons and messages.  The setting keeps its
``REGISTA_PRINCIPAL_ID`` name (D3: names stay).
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, NoReturn

from agent_notes.core.errors import AgentNotesError, ErrorCode
from agent_notes.core.suite_env import load_suite_env

_ACTOR_ID_ENV = "AGENT_NOTES_ACTOR_ID"
_SUITE_PRINCIPAL_ID_ENV = "REGISTA_PRINCIPAL_ID"

# ---------------------------------------------------------------------------
# Canonical principal grammar (vendored from regista 0.7.2, TRUST-DOMAIN.md §2.1)
#
#     principal-id = kind ":" subject
#     kind         = "human" / "agent" / "service"
#     subject      = 1*247 subject-char
#     subject-char = ALPHA / DIGIT / "." / "_" / "-" / "~" / ":" / "/"
#
# ASCII only, at most 256 UTF-8 bytes, NFC asserted, kind matched
# case-sensitively, and the subject may not begin or end with ``:.-_/``.  The
# subject is everything after the first colon.  ``key:*`` is never a principal.
# ---------------------------------------------------------------------------

#: The closed kind set: case-sensitive, lowercase, no extension mechanism.
PRINCIPAL_KINDS: frozenset[str] = frozenset({"human", "agent", "service"})
#: Refused by name, so the refusal carries a reason a grep can find.
_FORBIDDEN_KINDS: frozenset[str] = frozenset({"key"})
_MAX_PRINCIPAL_ID_BYTES = 256
_MAX_SUBJECT_LENGTH = 247
_SUBJECT_RE = re.compile(r"[A-Za-z0-9._~:/-]+")
_SUBJECT_EDGE_FORBIDDEN = ":.-_/"
#: The legacy bare-name shape, matched only to tell an old-convention name
#: apart from junk so the refusal can say which it is.
_LEGACY_BARE_RE = re.compile(r"[A-Za-z0-9._-]{1,256}")

_FORM_BARE_NAME = "bare_name"
_FORM_UNGRAMMATICAL = "ungrammatical"


def principal_id_problem(value: Any) -> tuple[str, str] | None:
    """Classify ``value`` without raising: ``None`` when canonical, else (form, reason)."""

    if not isinstance(value, str):
        return _FORM_UNGRAMMATICAL, "not_a_string"
    if not value:
        return _FORM_UNGRAMMATICAL, "empty"
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        return _FORM_UNGRAMMATICAL, "non_ascii"
    # A no-op under ASCII, asserted anyway so a relaxation can't change signed bytes.
    if unicodedata.normalize("NFC", value) != value:
        return _FORM_UNGRAMMATICAL, "not_nfc"
    if len(value.encode("utf-8")) > _MAX_PRINCIPAL_ID_BYTES:
        return _FORM_UNGRAMMATICAL, "too_long"
    if ":" not in value:
        if _LEGACY_BARE_RE.fullmatch(value) is not None:
            return _FORM_BARE_NAME, "bare_name_not_canonical"
        return _FORM_UNGRAMMATICAL, "not_kind_colon_subject"
    kind, subject = value.split(":", 1)
    if kind in _FORBIDDEN_KINDS:
        return _FORM_UNGRAMMATICAL, "key_is_never_a_principal"
    if kind not in PRINCIPAL_KINDS:
        return _FORM_UNGRAMMATICAL, "kind_not_canonical"
    if not subject:
        return _FORM_UNGRAMMATICAL, "subject_empty"
    if len(subject) > _MAX_SUBJECT_LENGTH:
        return _FORM_UNGRAMMATICAL, "subject_too_long"
    if _SUBJECT_RE.fullmatch(subject) is None:
        return _FORM_UNGRAMMATICAL, "subject_char_not_allowed"
    if subject[0] in _SUBJECT_EDGE_FORBIDDEN or subject[-1] in _SUBJECT_EDGE_FORBIDDEN:
        return _FORM_UNGRAMMATICAL, "subject_edge_char"
    return None


def _refuse(value: Any, form: str, reason: str, path: str) -> NoReturn:
    # Messages and detail are regista 0.7.2's verbatim, so the envelope a
    # misconfigured host sees is byte-identical to the one before vendoring.
    if form == _FORM_BARE_NAME:
        raise AgentNotesError(
            ErrorCode.PRINCIPAL_ID_NOT_CANONICAL,
            f"{path} {value!r} is a legacy bare name and is refused at creation "
            f"post-cutover (TRUST-DOMAIN.md §2.4 convention 2). Use the canonical "
            f"{sorted(PRINCIPAL_KINDS)} ':' <stable-opaque-subject> form; the legacy "
            f"name keeps binding to its historical events and is joined to the "
            f"canonical principal by a signed, scoped regista.principal-alias/v1 event "
            f"(§2.5) — which joins records for reporting and never satisfies signature "
            f"binding.",
            {
                "reason": reason,
                "path": path,
                "form": form,
                "remedy": "principal_alias_bound",
                "alias_payload_type": "regista.principal-alias",
            },
        )
    raise AgentNotesError(
        ErrorCode.PRINCIPAL_ID_UNGRAMMATICAL,
        f"{path} {value!r} does not satisfy the canonical principal grammar "
        f"(TRUST-DOMAIN.md §2.1): {reason}",
        {"reason": reason, "path": path, "form": form},
    )


def validate_principal_id(value: Any, *, path: str = "principal_id") -> str:
    """Return ``value`` unchanged if it is a canonical principal id; raise otherwise.

    Two refusals, so an operator can tell "the old convention" from "not an
    identifier": ``PRINCIPAL_ID_NOT_CANONICAL`` for a legacy bare name, and
    ``PRINCIPAL_ID_UNGRAMMATICAL`` for everything else, including ``key:*``.
    """

    problem = principal_id_problem(value)
    if problem is not None:
        _refuse(value, problem[0], problem[1], path)
    return str(value)


class ActorConfigurationError(RuntimeError):
    """The ambient actor identity is missing or not a canonical principal."""

    code = "ACTOR_ID_NOT_CONFIGURED"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        if code is not None:
            self.code = code
        super().__init__(f"{self.code}: {message}")


@dataclass(frozen=True, slots=True)
class Actor:
    """A fully resolved actor.

    ``actor_kind`` is derived from the canonical principal prefix.  Keeping it
    derived prevents a caller from pairing ``human:alice`` with ``agent`` (or
    a service principal with an ordinary agent event).
    """

    actor_id: str
    actor_kind: str
    display_name: str = ""
    role: str = ""

    def actor_metadata(self) -> dict[str, str]:
        # The model belongs to the producer block (Plan 026 D2), never to
        # actor metadata.
        return {
            "display_name": self.display_name or self.actor_id,
            "role": self.role or self.actor_kind,
        }


@dataclass(frozen=True, slots=True)
class ActorConfig:
    actor_id: str | None


def _layered(name: str, suite: dict[str, str]) -> str | None:
    """Resolve one setting through process, user, and system suite layers."""

    value = os.environ.get(name)
    if value is not None:
        return value
    return suite.get(name) or None


def _resolve_identity(suite: dict[str, str]) -> str | None:
    """Resolve the actor id with tool-specific precedence."""

    value = _layered(_ACTOR_ID_ENV, suite)
    if value is not None:
        return value.strip()
    value = _layered(_SUITE_PRINCIPAL_ID_ENV, suite)
    return value.strip() if value is not None else None


def load_actor_config() -> ActorConfig:
    """Load ambient identity without silently inventing a principal."""

    return ActorConfig(actor_id=_resolve_identity(load_suite_env()))


def _canonical_actor_kind(actor_id: str) -> str:
    """Validate the principal grammar and derive the row actor kind."""

    try:
        validate_principal_id(actor_id, path="actor_id")
    except AgentNotesError as exc:
        raise ActorConfigurationError(str(exc), code=exc.code.value) from exc
    principal_kind = actor_id.split(":", 1)[0]
    if principal_kind == "service":
        return "system"
    if principal_kind in {"agent", "human"}:
        return principal_kind
    # ``PRINCIPAL_KINDS`` is closed, but keep this branch explicit so a future
    # vocabulary expansion cannot silently choose a kind.
    raise ActorConfigurationError(
        f"canonical principal kind {principal_kind!r} has no actor-kind mapping"
    )


def validate_actor(actor: Actor) -> Actor:
    """Validate an explicitly constructed actor."""

    expected = _canonical_actor_kind(actor.actor_id)
    if actor.actor_kind != expected:
        raise ActorConfigurationError(
            f"actor_id {actor.actor_id!r} requires actor_kind {expected!r}, "
            f"not {actor.actor_kind!r}"
        )
    return actor


def resolve_actor(config: ActorConfig | None = None) -> Actor:
    """Resolve and validate the process actor."""

    cfg = config or load_actor_config()
    if cfg.actor_id is None or not cfg.actor_id:
        raise ActorConfigurationError(
            "no canonical actor identity is configured; set "
            f"{_ACTOR_ID_ENV} or {_SUITE_PRINCIPAL_ID_ENV} in the process "
            "environment or suite.env"
        )
    actor = Actor(
        actor_id=cfg.actor_id,
        actor_kind=_canonical_actor_kind(cfg.actor_id),
        display_name=cfg.actor_id,
    )
    return validate_actor(actor)


def write_actor() -> Actor:
    """Return the configured actor for an authored work-item write.

    There are intentionally no actor or model-lineage override arguments: a
    write is authored by the process's configured principal. Refuses the
    retired regista write-through setting first (Plan 026 D1), so a process
    that still asks for regista writes fails loudly instead of writing
    natively behind the operator's back.
    """

    from agent_notes.core.config import reject_regista_writes

    reject_regista_writes()
    return resolve_actor()


def default_actor() -> Actor:
    """Return the configured actor for note-shaped and ordinary write paths."""

    return write_actor()


def migration_actor() -> Actor:
    """The canonical system actor used for snapshot migration."""

    return Actor(
        actor_id="service:agent-notes-migration",
        actor_kind="system",
        display_name="agent-notes migration",
        role="system",
    )


__all__ = [
    "Actor",
    "ActorConfig",
    "ActorConfigurationError",
    "default_actor",
    "load_actor_config",
    "migration_actor",
    "principal_id_problem",
    "resolve_actor",
    "validate_actor",
    "validate_principal_id",
    "write_actor",
]
