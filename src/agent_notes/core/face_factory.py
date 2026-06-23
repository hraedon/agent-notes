"""Face selection — the integration seam between the write path and regista
(Plan 009).

``get_face()`` returns the process-wide face singleton used by the write path
(``work_item_model.py``):

- regista writes disabled (``AGENT_NOTES_REGISTA_WRITES`` unset / off) → returns
  ``None``; the write path uses the legacy op_log unchanged (the feature gate).
- regista writes enabled, outbox off → a plain ``RegistaFace`` (P1: fail-fast).
- outbox enabled (``AGENT_NOTES_OUTBOX=1``) → the base face wrapped in the
  never-fail outbox layer from ``core.outbox`` (P2: AC-1).

The outbox import is lazy so P1 does not depend on P2 modules at import time.
"""

from __future__ import annotations

import threading

from agent_notes.core.actor import resolve_actor
from agent_notes.core.config import RegistaConfig, regista_config
from agent_notes.core.regista_face import RegistaFace

_FACE_LOCK = threading.Lock()
_FACE: RegistaFace | None = None
_FACE_BUILT = False

_OUTBOX_ENV = "AGENT_NOTES_OUTBOX"


def _build_base_face(cfg: RegistaConfig) -> RegistaFace:
    import regista

    reg = regista.Regista(
        cfg.dsn,
        cfg.project,
        cfg.hmac_key_path,
        require_ssl=cfg.require_ssl,
    )
    return RegistaFace(reg)


def _build_face(cfg: RegistaConfig) -> RegistaFace | None:
    if not cfg.enabled:
        return None
    import os

    face = _build_base_face(cfg)
    if os.environ.get(_OUTBOX_ENV, "").lower() in {"1", "true", "yes"}:
        from agent_notes.core.outbox import OutboxAwareFace

        return OutboxAwareFace(face, project=cfg.project)
    return face


def get_face() -> RegistaFace | None:
    """Return the process-wide face, building it on first call.

    Returns ``None`` when regista writes are disabled (legacy op_log path).
    Re-reads config only until the first successful build; later calls return
    the cached face. Call ``reset_face()`` from tests.
    """
    global _FACE, _FACE_BUILT
    if _FACE_BUILT:
        return _FACE
    with _FACE_LOCK:
        if _FACE_BUILT:
            return _FACE
        cfg = regista_config()
        _FACE = _build_face(cfg)
        _FACE_BUILT = True
        return _FACE


def default_actor():
    """The actor used by the regista write path (env-resolved; Plan 009 D3)."""
    return resolve_actor()


def reset_face() -> None:
    """Reset the face singleton (tests). Closes any open regista handle."""
    global _FACE, _FACE_BUILT
    with _FACE_LOCK:
        if _FACE is not None:
            try:
                _FACE.close()
            except Exception:
                pass
        _FACE = None
        _FACE_BUILT = False


def set_face_for_test(face: RegistaFace | None) -> None:
    """Inject a face (e.g. an InMemoryRegista-backed one) for tests."""
    global _FACE, _FACE_BUILT
    with _FACE_LOCK:
        _FACE = face
        _FACE_BUILT = True
