"""Select and cache the regista face for the current project."""

from __future__ import annotations

import atexit
import contextvars
import threading
from typing import Callable

from agent_notes.core.actor import Actor, resolve_actor
from agent_notes.core.config import RegistaConfig, regista_config
from agent_notes.core.regista_face import RegistaFace

_FACE_LOCK = threading.Lock()
_FACES: dict[str, RegistaFace] = {}
_FACE_CLEANUPS: dict[str, Callable[[], None]] = {}
_TEST_FACE: RegistaFace | None = None
_TEST_FACE_SET = False
_CURRENT_PROJECT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "agent_notes_regista_project", default=None
)
_OUTBOX_ENV = "AGENT_NOTES_OUTBOX"


def regista_project_name(slug: str) -> str:
    """Map a software-project slug to its regista schema name."""

    from regista._connection import validate_project_name

    return validate_project_name(slug.replace("-", "_"))


def set_current_project(regista_name: str | None) -> None:
    _CURRENT_PROJECT.set(regista_name)


def current_project() -> str | None:
    return _CURRENT_PROJECT.get()


def _build_face(cfg: RegistaConfig, project: str) -> tuple[RegistaFace, Callable[[], None] | None]:
    import os

    import regista

    from agent_notes.core import secrets as suite_secrets

    dsn = suite_secrets.resolve_dsn(cfg.dsn)
    key_path, cleanup = suite_secrets.materialize_key_manifest(cfg.key_path)
    reg = regista.Regista(dsn, project, key_path, require_ssl=cfg.require_ssl)
    face: RegistaFace = RegistaFace(reg)
    if os.environ.get(_OUTBOX_ENV, "").lower() in {"1", "true", "yes"}:
        from agent_notes.core.outbox import OutboxAwareFace

        return OutboxAwareFace(face, project=project), cleanup
    return face, cleanup


def get_face() -> RegistaFace | None:
    """Return the cached face, or ``None`` when regista writes are disabled."""

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


def default_actor() -> Actor:
    """Return the configured actor for note and ordinary write paths."""

    return resolve_actor()


def write_actor() -> Actor:
    """Return the configured actor for an authored v6 write.

    There are intentionally no actor or model-lineage override arguments. A
    reviewer uses the process's configured principal and regista derives the
    producer identity from its process environment.
    """

    return resolve_actor()


def _close_faces_quietly() -> None:
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
    """Reset all cached faces (tests)."""

    global _TEST_FACE, _TEST_FACE_SET
    with _FACE_LOCK:
        for face in _FACES.values():
            try:
                face.close()
            except Exception:
                pass
        _FACES.clear()
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
    """Inject a project-agnostic face for tests."""

    global _TEST_FACE, _TEST_FACE_SET
    with _FACE_LOCK:
        _TEST_FACE = face
        _TEST_FACE_SET = True


__all__ = [
    "current_project",
    "default_actor",
    "get_face",
    "regista_project_name",
    "reset_face",
    "set_current_project",
    "set_face_for_test",
    "write_actor",
]
