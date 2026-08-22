"""Resolve the canonical v6 actor for agent-notes writes.

v6 deliberately has one identity on an event: ``actor.principal_id``.  Legacy
proxy-principal and model metadata are not part of the v6 envelope.  The
producer block is resolved by regista from the process environment, so this
module only resolves the actor and optional signed action-delegation evidence.

Actor resolution is trust-rooted in operator configuration, never in command
arguments or prompt input.  The tool-specific actor setting wins over the
suite setting, and the suite's canonical principal is the fallback:

``AGENT_NOTES_ACTOR_ID`` > ``REGISTA_PRINCIPAL_ID``

Both values use the normal process-env / per-user suite.env / system suite.env
layering.  A missing or non-canonical value fails closed before a write is
attempted.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_notes.core.suite_env import load_suite_env

_ACTOR_ID_ENV = "AGENT_NOTES_ACTOR_ID"
_SUITE_PRINCIPAL_ID_ENV = "REGISTA_PRINCIPAL_ID"
_ACTION_DELEGATION_PATHS_ENV = "AGENT_NOTES_ACTION_DELEGATION_PATHS"


class ActorConfigurationError(RuntimeError):
    """The ambient actor identity is missing or cannot be used for v6."""

    code = "ACTOR_ID_NOT_CONFIGURED"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        if code is not None:
            self.code = code
        super().__init__(f"{self.code}: {message}")


class DelegationConfigurationError(RuntimeError):
    """Configured v6 action-delegation evidence is absent or malformed."""

    code = "INVALID_ACTION_DELEGATION_CONFIG"

    def __init__(self, message: str) -> None:
        super().__init__(f"{self.code}: {message}")


@dataclass(frozen=True, slots=True)
class Actor:
    """A fully resolved v6 actor.

    ``actor_kind`` is derived from the canonical principal prefix.  Keeping it
    derived prevents a caller from pairing ``human:alice`` with ``agent`` (or
    a service principal with an ordinary agent event).
    """

    actor_id: str
    actor_kind: str
    display_name: str = ""
    role: str = ""
    action_delegation_credentials: tuple[dict[str, Any], ...] = ()

    def actor_metadata(self) -> dict[str, str]:
        # Producer/model fields belong to regista's signed producer member and
        # must not be smuggled into actor metadata.
        return {
            "display_name": self.display_name or self.actor_id,
            "role": self.role or self.actor_kind,
        }


@dataclass(frozen=True, slots=True)
class ActorConfig:
    actor_id: str | None
    action_delegation_credentials: tuple[dict[str, Any], ...] = ()


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


def _load_action_delegation_credentials(raw_paths: str | None) -> tuple[dict[str, Any], ...]:
    """Load the configured root-to-terminal delegation chain.

    The files contain signed public evidence, not private keys.  They are still
    trusted configuration and therefore are never accepted from a command
    argument.  Regista performs the cryptographic and scope checks; this edge
    validates the shape and terminal subject before a write reaches regista.
    """

    if raw_paths is None:
        return ()
    paths = [item.strip() for item in raw_paths.split(os.pathsep) if item.strip()]
    if not paths:
        raise DelegationConfigurationError(
            f"{_ACTION_DELEGATION_PATHS_ENV} names no credential files"
        )
    documents: list[dict[str, Any]] = []
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise DelegationConfigurationError(
                f"cannot load action-delegation credential {path}: {exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise DelegationConfigurationError(
                f"action-delegation credential {path} must be one JSON object"
            )
        documents.append(parsed)
    return tuple(documents)


def load_actor_config() -> ActorConfig:
    """Load ambient identity without silently inventing a principal."""

    suite = load_suite_env()
    return ActorConfig(
        actor_id=_resolve_identity(suite),
        action_delegation_credentials=_load_action_delegation_credentials(
            _layered(_ACTION_DELEGATION_PATHS_ENV, suite)
        ),
    )


def _canonical_actor_kind(actor_id: str) -> str:
    """Validate the shared principal grammar and derive the row actor kind."""

    import regista

    try:
        regista.validate_principal_id(actor_id, path="actor_id")
    except Exception as exc:
        code = getattr(getattr(exc, "code", None), "value", None)
        raise ActorConfigurationError(str(exc), code=code) from exc
    principal_kind = actor_id.split(":", 1)[0]
    if principal_kind == "service":
        return "system"
    if principal_kind in {"agent", "human"}:
        return principal_kind
    # ``validate_principal_id`` has a closed vocabulary, but keep this branch
    # explicit so a future vocabulary expansion cannot silently choose a kind.
    raise ActorConfigurationError(
        f"canonical principal kind {principal_kind!r} has no v6 actor-kind mapping"
    )


def validate_actor(actor: Actor) -> Actor:
    """Validate an explicitly constructed actor before it reaches regista."""

    expected = _canonical_actor_kind(actor.actor_id)
    if actor.actor_kind != expected:
        raise ActorConfigurationError(
            f"actor_id {actor.actor_id!r} requires actor_kind {expected!r}, "
            f"not {actor.actor_kind!r}"
        )
    if actor.action_delegation_credentials:
        import regista

        try:
            parsed = [
                regista.parse_action_delegation(document)
                for document in actor.action_delegation_credentials
            ]
        except Exception as exc:
            raise DelegationConfigurationError(
                f"invalid action-delegation credential: {exc}"
            ) from exc
        if parsed[-1].subject_principal_id != actor.actor_id:
            raise DelegationConfigurationError(
                f"the terminal action-delegation subject does not match actor_id {actor.actor_id!r}"
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
        action_delegation_credentials=cfg.action_delegation_credentials,
    )
    return validate_actor(actor)


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
    "DelegationConfigurationError",
    "load_actor_config",
    "migration_actor",
    "resolve_actor",
    "validate_actor",
]
