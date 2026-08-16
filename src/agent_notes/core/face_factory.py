"""Face selection — the integration seam between the write path and regista
(Plan 009; per-project routing in Plan 011).

``get_face()`` returns the face for the **current project** used by the write
path (``work_item_model.py``):

- regista writes disabled (``AGENT_NOTES_REGISTA_WRITES`` unset / off) → returns
  ``None``; the write path uses the legacy op_log unchanged (the feature gate).
- regista writes enabled, outbox off → a plain ``RegistaFace`` (P1: fail-fast).
- outbox enabled (``AGENT_NOTES_OUTBOX=1``) → the base face wrapped in the
  never-fail outbox layer from ``core.outbox`` (P2: AC-1).

**Per-project routing (Plan 011):** the converged store is one regista project
(schema) per software-project. The CLI's ``_resolve()`` sets the current project
via ``set_current_project()``; ``get_face()`` reads it and returns the cached
face for that project (one ``Regista`` per schema). When no project is set, it
falls back to ``cfg.project`` (the legacy single-project default). A face
injected by ``set_face_for_test`` overrides routing entirely, so unit tests
that drive a single in-memory store keep working.

The outbox import is lazy so P1 does not depend on P2 modules at import time.
"""

from __future__ import annotations

import atexit
import contextvars
import dataclasses
import threading
from typing import Callable

from agent_notes.core.actor import (
    Actor,
    declared_lineage,
    load_actor_config,
    require_declared_lineage,
    resolve_actor,
)
from agent_notes.core.config import RegistaConfig, regista_config
from agent_notes.core.regista_face import RegistaFace

_FACE_LOCK = threading.Lock()
# Per-project face cache, keyed by regista project name (schema).
_FACES: dict[str, RegistaFace] = {}
# Cleanup callables for materialized key-set temp manifests (Plan 017 WI-4.1),
# keyed by the same project name as ``_FACES``. Invoked from ``reset_face`` so a
# backend-sourced manifest is scrubbed when its face is torn down, not just at
# interpreter exit.
_FACE_CLEANUPS: dict[str, Callable[[], None]] = {}
# Test override: when set, get_face() returns this for ANY project.
_TEST_FACE: RegistaFace | None = None
_TEST_FACE_SET = False

# The current regista project name for this execution context. The CLI sets it
# from the resolved software-project; None falls back to cfg.project.
_CURRENT_PROJECT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "agent_notes_regista_project", default=None
)

_OUTBOX_ENV = "AGENT_NOTES_OUTBOX"


def regista_project_name(slug: str) -> str:
    """Map a software-project slug to its regista project (schema) name.

    regista schema names forbid hyphens (``validate_project_name``), so slugs
    like ``cert-watch`` map to ``cert_watch``. This MUST match the migration's
    mapping (see [[reference-production-regista-store]]).
    """
    from regista._connection import validate_project_name

    return validate_project_name(slug.replace("-", "_"))


def set_current_project(regista_name: str | None) -> None:
    """Set the current regista project for this context (Plan 011).

    Pass a regista project NAME (already mapped via ``regista_project_name``),
    or ``None`` to fall back to the configured default.
    """
    _CURRENT_PROJECT.set(regista_name)


def current_project() -> str | None:
    return _CURRENT_PROJECT.get()


def _build_face(cfg: RegistaConfig, project: str) -> tuple[RegistaFace, Callable[[], None] | None]:
    import os

    import regista

    from agent_notes.core import secrets as suite_secrets

    # Resolve suite secrets through the backend (Plan 017 WI-4.1). A literal
    # DSN / bare key path passes through unchanged (no regression); a backend
    # ref resolves at use time. The key-set manifest may materialize to a
    # 0600 temp file whose cleanup is tracked alongside the face.
    dsn = suite_secrets.resolve_dsn(cfg.dsn)
    key_path, cleanup = suite_secrets.materialize_key_manifest(cfg.hmac_key_path)
    reg = regista.Regista(
        dsn,
        project,
        key_path,
        require_ssl=cfg.require_ssl,
    )
    face: RegistaFace = RegistaFace(reg)
    if os.environ.get(_OUTBOX_ENV, "").lower() in {"1", "true", "yes"}:
        from agent_notes.core.outbox import OutboxAwareFace

        return OutboxAwareFace(face, project=project), cleanup
    return face, cleanup


def get_face() -> RegistaFace | None:
    """Return the face for the current project, building+caching on first use.

    Returns ``None`` when regista writes are disabled (legacy op_log path). A
    face injected via ``set_face_for_test`` takes precedence over routing.
    """
    if _TEST_FACE_SET:
        return _TEST_FACE
    cfg = regista_config()
    if not cfg.enabled:
        return None
    target = _CURRENT_PROJECT.get() or cfg.project
    cached = _FACES.get(target)
    if cached is not None:
        return cached
    with _FACE_LOCK:
        cached = _FACES.get(target)
        if cached is None:
            cached, cleanup = _build_face(cfg, target)
            _FACES[target] = cached
            if cleanup is not None:
                _FACE_CLEANUPS[target] = cleanup
        return cached


def default_actor():
    """The env-resolved actor, WITHOUT the declared-lineage requirement.

    Note-shaped entities (memories, reflections — ``core/note_model.py``) still
    use this. They are signed regista note events, but they are not work items:
    ``derive_authors`` never reads them, so an undeclared lineage there does not
    poison a review gate the way a work-item event does (WI-062). Every
    work-item write goes through :func:`actor_with_overrides` instead, which
    does enforce it. If the note path is ever brought under the same rule, move
    the ``require_declared_lineage`` call in here and delete this note — but do
    it deliberately, because it fails `memory add` for every unconfigured
    caller in the estate.
    """
    return resolve_actor()


def actor_with_overrides(
    actor_id: str | None = None,
    model_lineage: str | None = None,
    *,
    clear_principal: bool = False,
    operation: str | None = None,
) -> Actor:
    """Resolve a **work-item write-path** actor with optional identity overrides.

    Used by both authoring operations (file, update, close) and review-gate
    transitions (adversarial_pass, accept, reject, request_changes). This
    helper builds an Actor from the env-resolved config, applying per-call
    overrides so a subagent can declare its own lineage without env-var
    mutation.

    When ``actor_id`` is overridden and ``clear_principal=True``, the
    env-resolved principal (``on_behalf_of``) is cleared — a reviewer with a
    distinct identity is its own principal, not acting on behalf of the
    author's principal. Without this, the gate's separation-of-duties check
    would flag a "delegated self-review." When ``clear_principal=False``
    (the default, used for authoring), the principal is preserved — the
    agent is still acting on behalf of the human who invoked it, just with
    a distinct per-session identity.

    When only ``model_lineage`` is overridden (no ``actor_id``), the
    principal is always preserved — the actor is still the same agent,
    just declaring its lineage explicitly.

    **Fails closed on an undeclared lineage (WI-062).** An agent-kind actor
    with no resolvable ``model_lineage`` raises
    :class:`~agent_notes.core.actor.UndeclaredLineageError` here rather than
    stamping an event that regista's cross-lineage gate can never clear. Pass
    ``operation`` to name the caller in that error.
    """
    config = load_actor_config()
    if actor_id is not None and clear_principal:
        config = dataclasses.replace(
            config,
            actor_id=actor_id,
            principal_id=None,
            principal_display_name=None,
        )
    elif actor_id is not None:
        config = dataclasses.replace(
            config,
            actor_id=actor_id,
        )
    if model_lineage is not None:
        # Normalize like the env path (actor.py declared_lineage at resolve):
        # a padded token must not propagate un-stripped to the actor/outbox/
        # regista after passing the stripped registry check. A whitespace-only
        # override declares nothing and overwrites any env value — explicit
        # garbage refuses loudly rather than silently deferring to env.
        config = dataclasses.replace(config, model_lineage=declared_lineage(model_lineage))
    return require_declared_lineage(resolve_actor(config), operation)


def assert_declared_lineage(
    actor_id: str | None = None,
    model_lineage: str | None = None,
    *,
    operation: str | None = None,
) -> None:
    """Guard-only form of :func:`actor_with_overrides` (WI-062).

    The native (degrade) write path records ``actor_id`` / ``model_lineage`` as
    plain columns on an op rather than building an ``Actor``, so it has nothing
    to guard *through*. It calls this instead, so the same refusal applies on
    both paths: the op-log is replayed into regista by
    ``migrate-to-regista`` / outbox reconcile, and an undeclared-lineage op
    written today becomes an undeclared-lineage regista event tomorrow. Gating
    only the regista path would just defer the poisoning.
    """
    actor_with_overrides(actor_id, model_lineage, operation=operation)


def reviewer_actor(
    actor_id: str | None = None,
    model_lineage: str | None = None,
) -> Actor:
    """Deprecated alias for :func:`actor_with_overrides` with principal clearing.

    Reviewers act on their own behalf, not on behalf of the author's
    principal, so ``clear_principal=True`` is passed.
    """
    return actor_with_overrides(actor_id, model_lineage, clear_principal=True)


def _close_faces_quietly() -> None:
    """Close every cached face from the interpreter-exit path, swallowing errors.

    WI-062 discoverability: the failure that motivated that ticket was invisible
    because the real error was buried under
    ``PythonFinalizationError: cannot join thread at interpreter shutdown``.
    That noise is psycopg_pool's ``ConnectionPool.__del__`` joining its daemon
    workers *after* finalization has begun — regista fixed it on its side with a
    ``weakref.finalize`` (regista WI-218), but only in 0.5.5; the deployed uv
    tool is pinned to an older regista, and any consumer pinned below 0.5.5 gets
    the traceback. Closing the pools from an ``atexit`` hook here runs before
    finalization regardless of which regista is installed, so the last thing on
    stderr is the error the operator needs to read.

    Deliberately silent: nothing downstream of process exit can act on a failure
    to close, and logging at exit would reintroduce the noise this removes.
    """
    for face in list(_FACES.values()):
        try:
            face.close()
        except BaseException:
            pass
    for cleanup in list(_FACE_CLEANUPS.values()):
        try:
            cleanup()
        except BaseException:
            pass


atexit.register(_close_faces_quietly)


def reset_face() -> None:
    """Reset all faces (tests). Closes any open regista handles."""
    global _TEST_FACE, _TEST_FACE_SET
    with _FACE_LOCK:
        for face in _FACES.values():
            try:
                face.close()
            except Exception:
                pass
        _FACES.clear()
        # Scrub materialized key-set temp files so a test that resets + rebuilds
        # does not accumulate stale manifests (atexit would clean them only at
        # interpreter exit).
        for cleanup in _FACE_CLEANUPS.values():
            try:
                cleanup()
            except Exception:
                pass
        _FACE_CLEANUPS.clear()
        if _TEST_FACE is not None:
            try:
                _TEST_FACE.close()
            except Exception:
                pass
        _TEST_FACE = None
        _TEST_FACE_SET = False
    _CURRENT_PROJECT.set(None)


def set_face_for_test(face: RegistaFace | None) -> None:
    """Inject a project-agnostic face (e.g. InMemoryRegista-backed) for tests."""
    global _TEST_FACE, _TEST_FACE_SET
    with _FACE_LOCK:
        _TEST_FACE = face
        _TEST_FACE_SET = True
