"""Actor resolution for the regista face (Plan 009, D3).

agent-notes has no auth (decision 43). The agent face constructs an ``Actor``
whose identity is trust-rooted in environment / git config — never in prompt
input. This is the agent-notes analog of dossier's session-resolved Actor
(``dossier/src/dossier/auth/resolver.py``): provenance guarantee G1 (attribution)
is only as good as this binding, so the binding is structural — callers cannot
pass an ``actor_id`` string into the face; they pass an ``Actor`` resolved here.

Per-user ``principal_id`` (Plan 017 WI-4.2): the principal is resolved through
the suite config layering (blueprint §2.6) so each human's agents attribute to
*their* ``principal_id``:

    AGENT_NOTES_PRINCIPAL_ID env (tool-specific override)
    > REGISTA_PRINCIPAL_ID env (suite canonical, process env)
    > REGISTA_PRINCIPAL_ID from per-user suite.env
    > REGISTA_PRINCIPAL_ID from system suite.env
    > git config user.email (local dev fallback)

The live IdP binding (LDAP/Entra) is a seam: when a live identity source is
configured, ``resolve_principal_id`` would resolve from the authenticated
session. That binding is environment-gated (requires a live LDAP/Entra
connection); the local source (suite.env + git config) is the dev/default path.

**Declared lineage is mandatory for agent-authored work-item events (WI-062).**
``model_lineage`` resolves from ``AGENT_NOTES_MODEL_LINEAGE`` through the same
layering as every other identity fact —

    process env  >  per-user suite.env  >  system suite.env  >  tool default

— so a host can wire its lineage once in ``~/.config/agent-suite/suite.env``
instead of exporting it from every launcher. When it resolves to
nothing, :func:`require_declared_lineage` refuses to build a write-path actor
rather than letting the caller stamp an agent-kind event that regista's
cross-lineage review gate can never clear: ``derive_authors`` sets
``agent_author_undeclared`` per event, and history cannot be cured — a later
declaration by the same actor does not retroactively fix an event already
written. Failing at file time is cheap; a poisoned op-chain is not.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field

from agent_notes.core.suite_env import load_suite_env

_ACTOR_ID_ENV = "AGENT_NOTES_ACTOR_ID"
_PRINCIPAL_ID_ENV = "AGENT_NOTES_PRINCIPAL_ID"
# Canonical suite var for principal_id (multi-user-onboarding §3). The per-user
# suite.env overlay sets this so each human's agents attribute to them.
_SUITE_PRINCIPAL_ID_ENV = "REGISTA_PRINCIPAL_ID"
_PRINCIPAL_KIND_ENV = "AGENT_NOTES_PRINCIPAL_KIND"
_PRINCIPAL_DISPLAY_ENV = "AGENT_NOTES_PRINCIPAL_DISPLAY_NAME"
_MODEL_LINEAGE_ENV = "AGENT_NOTES_MODEL_LINEAGE"
_DEFAULT_ACTOR_ID = "agent-notes"


class UndeclaredLineageError(RuntimeError):
    """An agent-kind write was attempted with no declared ``model_lineage``.

    Deliberately a ``RuntimeError`` and not a ``ValueError``: the write path is
    littered with ``except ValueError`` handlers that translate domain errors
    into ``NOT_FOUND`` / ``VALIDATION_FAILED`` envelopes, and this failure would
    be mislabeled (and, worse, made to look like an ordinary rejection) by
    every one of them. As a ``RuntimeError`` it travels intact to the CLI's
    ``_dispatch``, which renders it under its own ``UNDECLARED_LINEAGE`` code.
    """

    code = "UNDECLARED_LINEAGE"

    def __init__(self, actor_id: str, operation: str | None = None) -> None:
        self.actor_id = actor_id
        self.operation = operation
        what = f"`{operation}`" if operation else "this agent-authored write"
        super().__init__(
            f"UNDECLARED_LINEAGE: {what} refused — actor {actor_id!r} "
            "declares no model lineage.\n"
            "\n"
            "An agent-kind event with no model_lineage is permanently "
            "un-reviewable: regista's cross-lineage review gate flags the item "
            "agent_author_undeclared, and event history cannot be cured — "
            "declaring a lineage later does not fix an event already written "
            "(agent-notes WI-062).\n"
            "\n"
            "Declare the lineage first — the model family, not the exact "
            "build (e.g. claude-opus, gpt-sol, glm, kimi):\n"
            f"    export {_MODEL_LINEAGE_ENV}=<model-family>\n"
            "or pass it per-invocation on the write commands (work-item "
            "file/update/close/delete/claim/release/heartbeat/attest-gate/"
            "review/request/wait/link-cross, breadcrumb "
            "file/update/delete/reconcile):\n"
            "    --model-lineage <model-family>"
        )


class InvalidLineageError(RuntimeError):
    """A declared ``model_lineage`` is not a family in regista's closed registry.

    Same ``RuntimeError`` rationale as :class:`UndeclaredLineageError`: it must
    travel intact past the write path's ``except ValueError`` handlers to the
    CLI's ``_dispatch``, which renders it under its own code. The code string
    deliberately matches regista's ingress error (``INVALID_MODEL_LINEAGE``) so
    operators meet one name for one fact — the difference is *where* it is
    caught: here, at agent-notes' own boundary, before anything is written or
    queued for the outbox (agent-suite WI-072 step 2).
    """

    code = "INVALID_MODEL_LINEAGE"

    def __init__(self, lineage: str, allowed: list[str], operation: str | None = None) -> None:
        self.lineage = lineage
        self.allowed = allowed
        self.operation = operation
        what = f"`{operation}`" if operation else "this agent-authored write"
        super().__init__(
            f"INVALID_MODEL_LINEAGE: {what} refused — {lineage!r} is not a "
            "model-lineage family in regista's closed registry.\n"
            "\n"
            "Lineage is the family, not the exact build or a harness name. "
            f"Allowed: {', '.join(allowed)}.\n"
            "\n"
            "Fix the declaration (env or per-invocation):\n"
            f"    export {_MODEL_LINEAGE_ENV}=<model-family>\n"
            "    --model-lineage <model-family>"
        )


def registry_families() -> frozenset[str] | None:
    """regista's closed lineage registry, or ``None`` when it has none.

    The installed regista predating the closed vocabulary (< 0.6 line) exports
    no ``MODEL_LINEAGE_FAMILIES``; returning ``None`` keeps this boundary check
    dormant there — free-text lineage remains regista's own ingress problem —
    and makes it activate by itself the moment SUITE.lock advances past the
    registry. Safe on both sides of the lock.
    """
    try:
        from regista import MODEL_LINEAGE_FAMILIES
    except ImportError as exc:
        # Dormant ONLY when regista itself is absent or lacks the export
        # (exc.name is the from-module in both cases). A broken install — a
        # failed transitive import inside regista — must NOT read as "no
        # registry": that would silently deactivate boundary validation on the
        # very host where something is already wrong. Re-raise it.
        if exc.name == "regista":
            return None
        raise
    if not isinstance(MODEL_LINEAGE_FAMILIES, frozenset):
        return None
    return MODEL_LINEAGE_FAMILIES


def declared_lineage(value: str | None) -> str | None:
    """The declared lineage in *value*, or ``None`` if it declares nothing.

    Mirrors regista's ``_review_validators.declared_lineage``: a whitespace-only
    string names no model, so it must read as undeclared here too. Otherwise a
    ``AGENT_NOTES_MODEL_LINEAGE="  "`` would pass this side's check and then be
    stripped to nothing by the gate — fail-open on both ends of the same fact.
    """
    if not isinstance(value, str):
        return None
    return value.strip() or None


def require_declared_lineage(actor: Actor, operation: str | None = None) -> Actor:
    """Return *actor* unchanged, or raise if it is an agent with no lineage.

    Only ``actor_kind == "agent"`` is gated. ``system`` actors (the migration
    actor) and any future ``human`` actor carry no model behind them, and
    regista's ``derive_authors`` never counts their kind toward the
    undeclared-agent gate, so requiring a lineage of them would be noise.
    """
    if actor.actor_kind != "agent":
        return actor
    lineage = declared_lineage(actor.model_lineage)
    if lineage is None:
        raise UndeclaredLineageError(actor.actor_id, operation)
    families = registry_families()
    if families is not None and lineage not in families:
        raise InvalidLineageError(lineage, sorted(families), operation)
    return actor


def _git_config(key: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "config", "--get", key],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    val = out.stdout.strip()
    return val or None


@dataclass(frozen=True, slots=True)
class Actor:
    actor_id: str
    actor_kind: str = "agent"
    display_name: str = ""
    on_behalf_of: dict | None = None
    role: str = "agent"
    model_lineage: str | None = None

    def actor_metadata(self) -> dict:
        meta = {"display_name": self.display_name or self.actor_id, "role": self.role}
        if self.model_lineage:
            meta["model_lineage"] = self.model_lineage
        return meta


@dataclass(frozen=True, slots=True)
class ActorConfig:
    actor_id: str
    principal_id: str | None = None
    principal_kind: str = "human"
    principal_display_name: str | None = None
    model_lineage: str | None = None
    signer_overrides: dict = field(default_factory=dict)


def resolve_principal_id(suite: dict[str, str] | None = None) -> str | None:
    """Resolve the principal_id from the suite identity source (Plan 017 WI-4.2).

    Precedence (blueprint §2.6):

    1. ``AGENT_NOTES_PRINCIPAL_ID`` env var (tool-specific override, highest)
    2. ``REGISTA_PRINCIPAL_ID`` env var (suite canonical, process env)
    3. ``REGISTA_PRINCIPAL_ID`` from per-user suite.env
    4. ``REGISTA_PRINCIPAL_ID`` from system suite.env
    5. git config ``user.email`` (local dev fallback)

    The live IdP binding (LDAP/Entra) is a seam — when a live identity source
    is configured, this function would resolve from the authenticated session
    (dossier binds LDAP; agent-notes adopts the one-identity-source binding).
    That binding is environment-gated (requires a live LDAP/Entra connection);
    the local source (suite.env + git config) is the dev/default path.
    """
    val = os.environ.get(_PRINCIPAL_ID_ENV)
    if val:
        return val
    val = os.environ.get(_SUITE_PRINCIPAL_ID_ENV)
    if val:
        return val
    if suite is None:
        suite = load_suite_env()
    val = suite.get(_SUITE_PRINCIPAL_ID_ENV)
    if val:
        return val
    return _git_config("user.email")


def _layered(name: str, suite: dict[str, str]) -> str | None:
    """Resolve *name* through the documented suite precedence.

        process env  >  per-user suite.env  >  system suite.env  >  (None)

    Every ``AGENT_NOTES_*`` identity fact resolves this way. It did not used to:
    ``load_actor_config`` loaded ``suite.env`` but consulted it for
    ``principal_id`` only, so ``actor_id``, ``model_lineage``,
    ``principal_kind`` and ``principal_display_name`` were process-env only. The
    per-user overlay is documented (``suite_env`` module docstring) as the home
    of "personal harness wiring", so setting the lineage there looked correct
    and did nothing — which is the reason no host in the estate had one
    configured when WI-062 was filed. A fail-closed check with no working
    configuration surface would just be a wall, so the surface is fixed here
    at the same time.
    """
    val = os.environ.get(name)
    if val:
        return val
    return suite.get(name) or None


def load_actor_config() -> ActorConfig:
    suite = load_suite_env()
    cfg = ActorConfig(
        actor_id=_layered(_ACTOR_ID_ENV, suite) or _DEFAULT_ACTOR_ID,
        principal_id=resolve_principal_id(suite),
        principal_kind=_layered(_PRINCIPAL_KIND_ENV, suite) or "human",
        principal_display_name=_layered(_PRINCIPAL_DISPLAY_ENV, suite),
        model_lineage=declared_lineage(_layered(_MODEL_LINEAGE_ENV, suite)),
    )
    if cfg.principal_display_name is None:
        name = _git_config("user.name")
        if name:
            object.__setattr__(cfg, "principal_display_name", name)
    return cfg


def resolve_actor(config: ActorConfig | None = None) -> Actor:
    cfg = config or load_actor_config()
    on_behalf_of: dict | None = None
    if cfg.principal_id:
        on_behalf_of = {
            "principal_id": cfg.principal_id,
            "principal_kind": cfg.principal_kind,
            "principal_display_name": cfg.principal_display_name or cfg.principal_id,
        }
    return Actor(
        actor_id=cfg.actor_id,
        actor_kind="agent",
        display_name=cfg.principal_display_name or cfg.actor_id,
        on_behalf_of=on_behalf_of,
        model_lineage=cfg.model_lineage,
    )


def migration_actor() -> Actor:
    return Actor(
        actor_id="agent-notes-migration",
        actor_kind="system",
        display_name="agent-notes migration",
        role="system",
    )
