"""Read-only session-identity invariant probe (agent-notes WI-071).

The genesis gate (``agent_suite.genesis_gate``) is a pre-epoch admission
ceremony: it runs each component's ``invariants probe --json`` as a subprocess,
validates the versioned report shape, and refuses to open the epoch unless
every *required* component-owned check reports ``pass``. agent-notes owns
exactly one required check —
``agent_notes.session_identity_resolvable`` — and until this module shipped it
was a named, permanent gate blocker (``ProbeSpec.preflight_capability=False``
for agent-notes in the umbrella).

What the required check claims
------------------------------
**"A work-item write issued from this process, right now, would carry a
resolvable, valid authored identity."**

That is a claim about *this host's ambient environment*, not about a fixture.
So the probe resolves the identity the way real writes resolve it: it calls
:func:`agent_notes.core.face_factory.actor_with_overrides` — the single entry
point every authored work-item write goes through (WI-062 for the authoring
verbs, WI-068 for the lease/cross-project/review verbs; the degrade path's
``assert_declared_lineage`` delegates to the same function, so proving it here
proves both paths) — with no per-invocation overrides, and reports what that
call did.

Deliberately *not* a fixture. The counter-example is cairn's probe
(``cairn/_invariant_probe.py``), whose passing evidence is a synthetic adapter
call against a recording store: it proves the code can behave, not that this
host is wired. That weakness is tracked as agent-provenance WI-046 and is the
thing this module is written to avoid. The only synthetic actors here are in
:func:`_probe_refusal`, and they exist to prove the validator *refuses* — a
deny-case, which cannot fail open.

What the required check does NOT claim
-------------------------------------
It measures agent-notes' *own* boundary — the identity resolution and the
declared-lineage validation that happen before anything leaves this process. It
does not claim regista would accept the resulting event: principal-key
registration and actor-boundary signing are regista's invariants, and the gate
requires regista's own ``regista.actor_boundary_signing`` and
``regista.first_write_admission`` checks for exactly that reason. Nor does it
claim the *store* is reachable — reachability is ``doctor``'s job, and asserting
it here would mean connecting, which would make the probe unsafe to schedule.
Two lesser scope limits worth knowing: a resolvable principal
(``on_behalf_of``) is reported as evidence but is not a verdict, because a write
succeeds without one and ``derive_authors`` does not require it; and the probe
speaks for the interpreter it runs in, so on a host with several venvs it
describes the agent-notes install that was invoked, not every one.

Deliberately no ``--actor-id`` / ``--model-lineage`` flags
---------------------------------------------------------
The write verbs accept those as per-invocation declarations. The probe does not,
because its subject is the *ambient* session identity: a report produced with a
lineage supplied on the probe's own command line would say "pass" about an
environment that is not wired, and the gate has no way to tell the two apart.
To ask "would ``glm`` be accepted here?", set the env var the write path
actually reads — ``AGENT_NOTES_MODEL_LINEAGE=glm agent-notes invariants
probe`` — which exercises the same layered resolution
(``process env > per-user suite.env > system suite.env > default``) rather than
bypassing it.

Why an unavailable lineage registry is a FAILURE, not a degradation
------------------------------------------------------------------
:func:`agent_notes.core.actor.registry_families` returns ``None`` when the
installed regista exports no ``MODEL_LINEAGE_FAMILIES`` (the pre-0.6 line), and
agent-notes' boundary validation is deliberately *dormant* there — free-text
lineage stays regista's own ingress problem, and the check activates by itself
once the lock advances. That dormancy is right for the write path and wrong for
this probe, for three reasons:

1. **"Resolvable" is the estate's word for "names a registry family."** regista's
   own probe counts a lineage token that is not in ``MODEL_LINEAGE_FAMILIES`` as
   an *unresolvable* value (``unresolvable_lineage_value_count``). With no
   registry to consult, agent-notes cannot establish that the token a write
   would stamp is resolvable — so the check's claim is unproven, and an unproven
   claim reported as ``pass`` is precisely the fail-open shape a gate probe
   exists to prevent.
2. **It blocks nothing that could honestly open.** The gate also requires
   ``regista.closed_lineage_registry`` from regista's own probe, so an epoch can
   never open on a regista that has no closed registry. Reporting ``fail`` here
   costs no reachable gate opening; reporting ``pass`` would only make the
   agent-notes half of the gate untrustworthy.
3. **It is a distinct fact from regista's check.** This probe measures the
   registry visible to *agent-notes' own import environment*, which on a
   multi-venv host is not the same regista the ``regista`` CLI runs.

The consequence is honest and worth stating plainly: on an environment pinned to
the locked spine (``SUITE.lock`` [spine].version 0.5.5 at time of writing) this
probe reports ``ok: false`` with reason ``lineage_registry_unavailable`` even
though writes work fine there. That is the probe saying "I cannot prove this",
not "your host is broken" — the ``detail`` and ``evidence`` say so.

Read-only by construction
-------------------------
Nothing here opens a database connection, constructs a regista face, or appends
an event. It reads process env, the ``suite.env`` overlays, ``git config``
(read-only, 2s timeout, via :mod:`agent_notes.core.actor`), and the installed
regista's ``MODEL_LINEAGE_FAMILIES`` constant. It never calls
:func:`agent_notes.core.face_factory.get_face`, so no DSN is resolved and no
connection pool is built. ``tests/test_invariant_probe.py`` pins that: the probe
runs to a valid verdict with an unreachable DSN configured and regista writes
enabled.
"""

from __future__ import annotations

import sys
import traceback
from dataclasses import dataclass, field
from typing import Any

from agent_notes.core import actor as actor_module
from agent_notes.core.actor import (
    Actor,
    InvalidLineageError,
    UndeclaredLineageError,
)

#: The report envelope version the umbrella validates. It must be exactly the
#: int ``1``: ``agent_suite.genesis_gate`` compares ``type(...) is int`` and the
#: value against its own ``PROBE_REPORT_VERSION``, and anything else is
#: MALFORMED at the gate (not FAIL — a different, less legible verdict).
PROBE_VERSION = 1

#: The component name the umbrella matches on. Must equal the ``ProbeSpec``
#: component ("agent-notes"), while every check ID is namespaced with the
#: underscored form — the gate rejects a check owned by another component.
COMPONENT = "agent-notes"
CHECK_PREFIX = "agent_notes."

#: The one check the genesis gate requires from agent-notes.
SESSION_IDENTITY_CHECK = "agent_notes.session_identity_resolvable"
#: Additional agent-notes-owned checks. The gate allows extra ``agent_notes.*``
#: checks and treats any of them reporting ``fail`` as a probe failure, so these
#: are gate-blocking too — that is intended for both.
LINEAGE_REGISTRY_CHECK = "agent_notes.lineage_registry_available"
REFUSAL_CHECK = "agent_notes.write_path_refuses_unresolvable_lineage"

#: Named on the report so an operator (and a reviewer) can tell the failure modes
#: apart without reading prose. Every value that can appear in a check's
#: ``reason`` field is listed here, and ``tests/test_invariant_probe.py`` asserts
#: each one is reachable.
REASONS: frozenset[str] = frozenset(
    {
        # session_identity_resolvable
        "resolved",
        "no_actor_resolvable",
        "unexpected_actor_kind",
        "lineage_undeclared",
        "lineage_not_in_registry",
        "lineage_registry_unavailable",
        "lineage_registry_error",
        "identity_resolution_error",
        # lineage_registry_available
        "registry_available",
        "registry_absent",
        "registry_import_error",
        # write_path_refuses_unresolvable_lineage
        "refusals_enforced",
        "refusal_missing",
        # whole-report fallback
        "probe_error",
    }
)

#: A token guaranteed not to be a registry family, used by the deny-case. It is
#: never written anywhere; it only travels into ``require_declared_lineage``.
_NON_FAMILY_TOKEN = "agent-notes-invariant-probe-not-a-family"
#: Lineage used for the positive control. Any member of the closed registry
#: works; this one is chosen from the registry at run time when one is available,
#: so the control does not hardcode a vocabulary this face does not own.
_PROBE_ACTOR_ID = "agent-notes-invariant-probe"


@dataclass(frozen=True)
class RegistryProbe:
    """What the installed regista's closed lineage registry looks like from here."""

    available: bool
    reason: str
    families: frozenset[str] | None = None
    error_type: str | None = None

    @property
    def family_count(self) -> int | None:
        return None if self.families is None else len(self.families)


@dataclass(frozen=True)
class IdentityProbe:
    """The identity a work-item write issued right now would carry."""

    ok: bool
    reason: str
    actor_id: str | None = None
    actor_kind: str | None = None
    model_lineage: str | None = None
    principal_resolved: bool = False
    principal_kind: str | None = None
    lineage_in_registry: bool | None = None
    error_code: str | None = None
    error_type: str | None = None


@dataclass(frozen=True)
class RefusalProbe:
    """Whether the installed boundary validation still refuses what it must."""

    ok: bool
    reason: str
    undeclared_refused: bool = False
    whitespace_refused: bool = False
    non_family_refused: bool | None = None
    declared_family_accepted: bool = False
    system_actor_exempt: bool | None = None
    unexpected: tuple[str, ...] = field(default_factory=tuple)


def probe_registry() -> RegistryProbe:
    """Measure regista's closed lineage registry as agent-notes sees it.

    Three outcomes, all named:

    - a non-empty ``frozenset`` → available;
    - ``None`` → the installed regista exports no registry (pre-0.6 line) or is
      absent entirely, so agent-notes' boundary validation is dormant;
    - an exception → ``registry_families`` deliberately re-raises an
      ``ImportError`` whose ``name`` is not ``regista`` (a *broken* regista
      install, as distinct from an absent one), because a failed transitive
      import must never read as "no registry". Recorded as its own reason.

    An empty registry counts as absent: a closed vocabulary with no members
    cannot admit any lineage, so nothing could be proven resolvable against it.

    Reached through the ``actor`` *module* rather than a from-import on purpose:
    ``require_declared_lineage`` consults ``actor.registry_families`` too, so one
    attribute is the single source of truth for what the probe reports and what
    the write path enforces. A from-imported alias here could drift from the
    function the write path actually calls (and would let a test stub one
    without the other, which is how a probe starts describing a registry the
    write path never consulted).
    """
    try:
        families = actor_module.registry_families()
    except Exception as exc:
        return RegistryProbe(
            available=False,
            reason="registry_import_error",
            error_type=type(exc).__name__,
        )
    if not families:
        return RegistryProbe(available=False, reason="registry_absent", families=families)
    return RegistryProbe(available=True, reason="registry_available", families=families)


def probe_identity(registry: RegistryProbe) -> IdentityProbe:
    """Resolve the write-path identity for this process and judge it.

    Calls ``face_factory.actor_with_overrides()`` with no overrides — the exact
    function every authored work-item write calls — so the verdict is the write
    path's own verdict, not a re-implementation of it. On refusal the resolved
    facts are recovered best-effort (``resolve_actor(load_actor_config())``,
    which never raises) purely so the report can *name* what was resolved; the
    verdict itself always comes from the write-path call.
    """
    from agent_notes.core.actor import load_actor_config, resolve_actor
    from agent_notes.core.face_factory import actor_with_overrides

    operation = "invariants probe"
    actor: Actor | None = None
    reason = "resolved"
    error_code: str | None = None
    error_type: str | None = None

    try:
        actor = actor_with_overrides(operation=operation)
    except UndeclaredLineageError as exc:
        reason, error_code, error_type = "lineage_undeclared", exc.code, type(exc).__name__
    except InvalidLineageError as exc:
        reason, error_code, error_type = "lineage_not_in_registry", exc.code, type(exc).__name__
    except Exception as exc:
        reason, error_type = "identity_resolution_error", type(exc).__name__

    if actor is None:
        # Best-effort facts for the report only. This path never decides the
        # verdict, and it is wrapped because a resolver that raises here is
        # itself the finding already recorded above.
        try:
            actor = resolve_actor(load_actor_config())
        except Exception:  # pragma: no cover - defensive
            actor = None

    actor_id = actor.actor_id if actor is not None else None
    actor_kind = actor.actor_kind if actor is not None else None
    lineage = actor.model_lineage if actor is not None else None
    on_behalf_of = actor.on_behalf_of if actor is not None else None
    principal_resolved = bool(on_behalf_of and on_behalf_of.get("principal_id"))
    principal_kind = (on_behalf_of or {}).get("principal_kind") if on_behalf_of else None
    lineage_in_registry: bool | None = None
    if registry.families is not None and isinstance(lineage, str):
        lineage_in_registry = lineage.strip() in registry.families

    # The write path's own refusals rank first: they are the facts about what a
    # write would actually do. Only when it *accepted* the identity do the
    # remaining, stricter conditions get a say.
    if reason == "resolved":
        if not isinstance(actor_id, str) or not actor_id.strip():
            # Stricter than the write path on purpose: `require_declared_lineage`
            # never inspects `actor_id`, so a blank/whitespace
            # AGENT_NOTES_ACTOR_ID is accepted by a write and stamped as the
            # author. An identity that names nobody is not resolvable, whatever
            # the write path tolerates.
            reason = "no_actor_resolvable"
        elif actor_kind != "agent":
            # `resolve_actor` hardcodes "agent"; anything else means the
            # resolution path changed under the gate's feet.
            reason = "unexpected_actor_kind"
        elif not registry.available:
            # See the module docstring: dormant boundary validation means the
            # claim cannot be proven, and unproven is not pass.
            reason = (
                "lineage_registry_error"
                if registry.reason == "registry_import_error"
                else "lineage_registry_unavailable"
            )
        elif lineage_in_registry is not True:  # pragma: no cover - defensive
            # Unreachable while `actor_with_overrides` validates membership;
            # kept so a future divergence surfaces as a failure, not a pass.
            reason = "lineage_not_in_registry"
    elif reason == "identity_resolution_error" and registry.reason == "registry_import_error":
        # The write-path call raises the *same* ImportError `probe_registry`
        # caught (``require_declared_lineage`` consults the registry), so name
        # the root cause rather than the symptom.
        reason = "lineage_registry_error"

    return IdentityProbe(
        ok=reason == "resolved",
        reason=reason,
        actor_id=actor_id,
        actor_kind=actor_kind,
        model_lineage=lineage,
        principal_resolved=principal_resolved,
        principal_kind=principal_kind,
        lineage_in_registry=lineage_in_registry,
        error_code=error_code,
        error_type=error_type,
    )


def _non_family_token(families: frozenset[str]) -> str:
    token = _NON_FAMILY_TOKEN
    while token in families:  # pragma: no cover - collision is not reachable
        token += "-x"
    return token


def probe_refusal(registry: RegistryProbe) -> RefusalProbe:
    """Deny-case: prove the installed boundary validation still refuses.

    ``probe_identity`` reports what this host *is*; a green report there is only
    meaningful if the validator that produced it can still say no. This check
    drives :func:`agent_notes.core.actor.require_declared_lineage` with synthetic
    actors — the only fixtures in this module — and requires:

    - an agent actor with no lineage is refused (``UndeclaredLineageError``);
    - an agent actor whose lineage is whitespace-only is refused too (a padded
      token declares nothing on both sides of the boundary);
    - an agent actor whose lineage is not a registry family is refused
      (``InvalidLineageError``) — only assertable when a registry is available,
      otherwise recorded as ``None`` (not exercised) rather than assumed;
    - **positive control:** an agent actor with a genuine family lineage is
      *accepted*. Without it, a validator that refused everything would pass a
      pure deny-case — the tautology this repo's process notes call out.

    The ``system``-kind exemption (the migration actor carries no model) is
    recorded as evidence, not asserted in the verdict: it is a scope fact, and
    folding it in would make the check id describe something it does not claim.

    One pathological state has no positive control available: a registry that is
    present but *empty* refuses every lineage, including the control. That
    surfaces as ``refusal_missing`` with the control's refusal recorded in
    ``unexpected`` — a broken-registry report rather than a silent pass, which is
    the right side to land on for a state regista should never ship.
    """
    from agent_notes.core.actor import require_declared_lineage

    unexpected: list[str] = []

    def _refused(lineage: str | None, expect: type[Exception]) -> bool:
        try:
            require_declared_lineage(
                Actor(actor_id=_PROBE_ACTOR_ID, actor_kind="agent", model_lineage=lineage),
                "invariants probe (deny-case)",
            )
        except expect:
            return True
        except Exception as exc:
            unexpected.append(f"{type(exc).__name__} for lineage={lineage!r}")
            return False
        return False

    undeclared_refused = _refused(None, UndeclaredLineageError)
    whitespace_refused = _refused("   ", UndeclaredLineageError)

    non_family_refused: bool | None = None
    declared_family_accepted = False
    if registry.families:
        non_family_refused = _refused(_non_family_token(registry.families), InvalidLineageError)
        control_lineage = sorted(registry.families)[0]
    else:
        # With no registry there is no closed vocabulary to control against, so
        # the positive control uses a plausible free-text family: the dormant
        # boundary accepts any declared token, which is exactly what must be
        # observed rather than assumed.
        control_lineage = "claude-opus"
    try:
        require_declared_lineage(
            Actor(actor_id=_PROBE_ACTOR_ID, actor_kind="agent", model_lineage=control_lineage),
            "invariants probe (positive control)",
        )
        declared_family_accepted = True
    except Exception as exc:
        unexpected.append(f"{type(exc).__name__} for control lineage={control_lineage!r}")

    system_actor_exempt: bool | None
    try:
        require_declared_lineage(
            Actor(actor_id=_PROBE_ACTOR_ID, actor_kind="system", model_lineage=None),
            "invariants probe (system exemption)",
        )
        system_actor_exempt = True
    except Exception:
        system_actor_exempt = False

    ok = (
        undeclared_refused
        and whitespace_refused
        and non_family_refused is not False
        and declared_family_accepted
        and not unexpected
    )
    return RefusalProbe(
        ok=ok,
        reason="refusals_enforced" if ok else "refusal_missing",
        undeclared_refused=undeclared_refused,
        whitespace_refused=whitespace_refused,
        non_family_refused=non_family_refused,
        declared_family_accepted=declared_family_accepted,
        system_actor_exempt=system_actor_exempt,
        unexpected=tuple(unexpected),
    )


_IDENTITY_DETAIL: dict[str, str] = {
    "resolved": (
        "a work-item write issued now would carry a resolvable actor identity "
        "and a lineage in regista's closed registry"
    ),
    "no_actor_resolvable": (
        "no actor identity resolvable: AGENT_NOTES_ACTOR_ID resolved to a blank "
        "value, so a write would stamp an author that names nobody"
    ),
    "unexpected_actor_kind": (
        "the resolved actor is not agent-kind; the write-path resolution no "
        "longer produces the actor the review gate reads"
    ),
    "lineage_undeclared": (
        "no model lineage declared: the write path refuses (UNDECLARED_LINEAGE), "
        "so no work-item write can be issued from this environment. Set "
        "AGENT_NOTES_MODEL_LINEAGE in process env or suite.env"
    ),
    "lineage_not_in_registry": (
        "the declared model lineage is not a family in regista's closed registry, "
        "so the write path refuses it (INVALID_MODEL_LINEAGE)"
    ),
    "lineage_registry_unavailable": (
        "regista's closed lineage registry is not available to agent-notes, so "
        "the declared lineage cannot be proven resolvable; the boundary "
        "validation a write would run is dormant here"
    ),
    "lineage_registry_error": (
        "regista is installed but its lineage registry could not be imported; "
        "boundary validation cannot be trusted on this host"
    ),
    "identity_resolution_error": (
        "the write-path identity resolution raised; no write could be attributed"
    ),
}

_REGISTRY_DETAIL: dict[str, str] = {
    "registry_available": "regista's closed lineage registry is importable and non-empty",
    "registry_absent": (
        "the installed regista exports no non-empty MODEL_LINEAGE_FAMILIES, so "
        "agent-notes' lineage boundary validation is dormant"
    ),
    "registry_import_error": (
        "importing regista's lineage registry raised; a broken install must not "
        "read as an absent registry"
    ),
}

_REFUSAL_DETAIL: dict[str, str] = {
    "refusals_enforced": (
        "the installed boundary validation refuses undeclared and unresolvable "
        "lineages and accepts a declared one"
    ),
    "refusal_missing": (
        "the installed boundary validation did not refuse an unresolvable "
        "lineage (or refused a valid one), so a passing identity report proves "
        "nothing"
    ),
}


def _check(check_id: str, ok: bool, reason: str, detail: str, evidence: dict[str, Any]) -> dict:
    return {
        "id": check_id,
        "status": "pass" if ok else "fail",
        "reason": reason,
        "detail": detail,
        "evidence": evidence,
    }


def build_report() -> dict[str, Any]:
    """The probe report, without the crash guard (see :func:`invariant_probe_report`)."""
    registry = probe_registry()
    identity = probe_identity(registry)
    refusal = probe_refusal(registry)

    checks = [
        _check(
            SESSION_IDENTITY_CHECK,
            identity.ok,
            identity.reason,
            _IDENTITY_DETAIL[identity.reason],
            {
                # The principal_id itself is deliberately absent: it is a human's
                # email/UPN on most hosts and this report is persisted in gate
                # artifacts. Whether one resolved, and its kind, is the
                # gate-relevant fact.
                "actor_id": identity.actor_id,
                "actor_kind": identity.actor_kind,
                "model_lineage": identity.model_lineage,
                "lineage_in_registry": identity.lineage_in_registry,
                "lineage_registry_available": registry.available,
                "principal_resolved": identity.principal_resolved,
                "principal_kind": identity.principal_kind,
                "write_path_error_code": identity.error_code,
                "write_path_error_type": identity.error_type,
                "resolution_entry_point": "face_factory.actor_with_overrides",
                "overrides_applied": False,
            },
        ),
        _check(
            LINEAGE_REGISTRY_CHECK,
            registry.available,
            registry.reason,
            _REGISTRY_DETAIL[registry.reason],
            {
                "family_count": registry.family_count,
                "import_error_type": registry.error_type,
            },
        ),
        _check(
            REFUSAL_CHECK,
            refusal.ok,
            refusal.reason,
            _REFUSAL_DETAIL[refusal.reason],
            {
                "undeclared_refused": refusal.undeclared_refused,
                "whitespace_lineage_refused": refusal.whitespace_refused,
                "non_family_refused": refusal.non_family_refused,
                "declared_family_accepted": refusal.declared_family_accepted,
                "system_actor_exempt": refusal.system_actor_exempt,
                "unexpected": list(refusal.unexpected),
            },
        ),
    ]
    return {
        "component": COMPONENT,
        "probe_version": PROBE_VERSION,
        "ok": all(check["status"] == "pass" for check in checks),
        "checks": checks,
    }


def invariant_probe_report() -> dict[str, Any]:
    """The contract-shaped report. Never raises.

    A probe that dies mid-run reaches the gate as MALFORMED or ERROR, which
    closes the gate with a verdict that names the process rather than the
    invariant. Emitting a well-formed report whose *required* check fails with
    reason ``probe_error`` closes it just as firmly and says which component
    could not measure itself. The exception message is deliberately kept out of
    the report — ``suite.env`` parsing sits on this path and its lines can carry
    a DSN (the same reason ``doctor`` reports type names only); the full
    traceback goes to stderr, which the gate captures but does not persist.
    """
    try:
        return build_report()
    except Exception as exc:
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
    "LINEAGE_REGISTRY_CHECK",
    "PROBE_VERSION",
    "REASONS",
    "REFUSAL_CHECK",
    "SESSION_IDENTITY_CHECK",
    "IdentityProbe",
    "RefusalProbe",
    "RegistryProbe",
    "build_report",
    "invariant_probe_report",
    "probe_identity",
    "probe_refusal",
    "probe_registry",
]
