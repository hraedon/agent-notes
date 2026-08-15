"""Session-scoped identity resolution (WI-067).

A host-wide lineage declares one model for every session on that host. On
multi-agent hosts that is false for most sessions and is *worse* than declaring
nothing: an undeclared lineage fails closed (UNKNOWN), while a wrong one passes
a same-lineage review as cross-lineage (fail-open). So lineage is
session-scoped — a record keyed by the harness session id, written once per
session by ``/start`` (or a SessionStart hook) and garbage-collected on age.
A host-wide value (suite.env) is a *fallback* only: it is legitimate where the
host runs exactly one model, and nothing here ever writes it.

Precedence (WI-067, revised per cross-lineage review):

    session record (once declared) > explicit --model-lineage > process env
    > per-user suite.env > system suite.env > default

The session record is the **stable source once declared**: an explicit
``--model-lineage`` that contradicts a declared record would let the same
session manufacture false cross-lineage independence (author as one lineage,
then "review" as another). Such a conflict is refused — the record is
authoritative for the session. Before a session declares, the explicit flag /
env / suite.env still apply (unattended callers, pre-declaration).

The session record lives under the platform state dir
(``~/.local/state/agent-notes/sessions/<session-id>.env`` on Linux;
``%LOCALAPPDATA%/agent-notes/sessions/`` on Windows), overridable via
``AGENT_NOTES_SESSION_DIR`` for tests. Records are written atomically
(temp file + ``os.replace``) with private permissions (0600 file / 0700 dirs;
a pre-existing directory is tightened to 0700, never left loose).
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import time
import warnings
from pathlib import Path
from typing import Mapping

import platformdirs

from agent_notes.core.suite_env import load_suite_env

#: The env var that declares lineage (same name actor.py reads). Kept as a
#: module constant here so actor resolution can consult the session chain
#: without importing actor (which imports this module).
MODEL_LINEAGE_ENV = "AGENT_NOTES_MODEL_LINEAGE"

#: Harness session-id env vars, in precedence order. The first present value
#: keys the session record. Claude Code exports ``CLAUDE_CODE_SESSION_ID``
#: (verified in WI-067); the opencode plugin passes ``OPENCODE_SESSION_ID``
#: through to spawned subprocesses; the codex hook adapter forwards the
#: payload's ``session_id`` as ``CODEX_SESSION_ID``.
_HARNESS_SESSION_ENV_VARS: tuple[str, ...] = (
    "CLAUDE_CODE_SESSION_ID",
    "OPENCODE_SESSION_ID",
    "CODEX_SESSION_ID",
)

#: Generic fallback env var, used only when no harness-native id is present.
#: It is *not* a harness session id: the outbox writes a per-process UUID here,
#: so a session record keyed by it will not be found by later tool calls in the
#: same harness session. Using it is a degraded, warn-worthy state.
_FALLBACK_SESSION_ENV = "AGENT_NOTES_SESSION"

#: Env override for the session-records directory (tests, exotic layouts).
_SESSION_DIR_ENV = "AGENT_NOTES_SESSION_DIR"

#: Default age after which a session record is garbage-collected.
_SESSION_RECORD_MAX_AGE_DAYS = 30

#: A session id is a UUID or slug; anything else (path separators, traversal)
#: is mapped to a deterministic digest before it becomes a filename.
_SAFE_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

#: One-shot warning flag so the fallback warning fires once per process, not
#: on every ``harness_session_id()`` call from a hot actor-resolution path.
_WARNED_FALLBACK_SESSION = False


class SessionIdentityConflictError(ValueError):
    """Raised when an explicit lineage contradicts a declared session record."""


def harness_session_id(env: Mapping[str, str] | None = None) -> str | None:
    """Return the current harness session id, or ``None`` when unresolvable.

    Precedence: ``CLAUDE_CODE_SESSION_ID`` (Claude Code) >
    ``OPENCODE_SESSION_ID`` (opencode plugin) > ``CODEX_SESSION_ID`` (codex
    hook). If none of the harness-native vars is present, the generic
    ``AGENT_NOTES_SESSION`` fallback is used *once with a warning* — it is a
    per-process UUID from the outbox, not a stable harness session, so an
    identity keyed by it will not survive to the next tool call. An empty
    string is treated as unset.
    """
    global _WARNED_FALLBACK_SESSION
    source = dict(os.environ) if env is None else dict(env)
    for var in _HARNESS_SESSION_ENV_VARS:
        value = source.get(var)
        if value:
            return value
    fallback = source.get(_FALLBACK_SESSION_ENV)
    if fallback:
        if not _WARNED_FALLBACK_SESSION:
            _WARNED_FALLBACK_SESSION = True
            warnings.warn(
                "session id resolved only from AGENT_NOTES_SESSION (a "
                "per-process fallback, not a harness session id); a session "
                "record keyed by it will not be found by later tool calls in "
                "the same harness session. Declare identity inside a harness "
                "that exports its session id, or pass the id explicitly.",
                UserWarning,
                stacklevel=2,
            )
        return fallback
    return None


def harness_session_source(env: Mapping[str, str] | None = None) -> str | None:
    """Return which env var supplied the session id (or ``None``).

    Purely descriptive: no warning, no fallback mutation. Used by the probe
    and ``session status`` so an auditor can see whether identity came from a
    real harness var or the generic fallback.
    """
    source = dict(os.environ) if env is None else dict(env)
    for var in (*_HARNESS_SESSION_ENV_VARS, _FALLBACK_SESSION_ENV):
        if source.get(var):
            return var
    return None


def session_records_dir() -> Path:
    """Return the directory holding per-session identity records.

    ``AGENT_NOTES_SESSION_DIR`` overrides (tests); otherwise the platformdirs
    user-state default for agent-notes (``~/.local/state/agent-notes/sessions``
    on Linux, ``%LOCALAPPDATA%/agent-notes/sessions`` on Windows).
    """
    override = os.environ.get(_SESSION_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return Path(platformdirs.user_state_dir("agent-notes")) / "sessions"


def _safe_session_key(session_id: str) -> str:
    """Map a session id to a safe filename key.

    A well-formed id (letters/digits/``._-``, <= 128 chars) is used verbatim.
    Anything else is deterministically digested so a hostile or malformed id
    can never traverse out of the records directory.
    """
    if _SAFE_SESSION_ID_RE.fullmatch(session_id):
        return session_id
    return hashlib.sha256(session_id.encode("utf-8", errors="replace")).hexdigest()


def session_record_path(session_id: str) -> Path:
    """Return the record path for a session id (does not touch the fs)."""
    return session_records_dir() / f"{_safe_session_key(session_id)}.env"


def _parse_record(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` record lines, mirroring suite_env's parser."""
    result: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        result[key.strip()] = value.strip()
    return result


def read_session_record(session_id: str) -> dict[str, str]:
    """Return the session record for a session id, or ``{}`` when absent."""
    path = session_record_path(session_id)
    if not path.is_file():
        return {}
    try:
        return _parse_record(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return {}


def _ensure_private_dir(directory: Path) -> None:
    """Create or tighten *directory* to 0700.

    A pre-existing session-records directory with loose permissions (e.g. 0755
    from an earlier buggy version) must be tightened, not left readable by
    other users — the records carry identity, not secrets, but per-session
    privacy is the point. ``mkdir`` alone does not change an existing
    directory's mode, so an explicit ``chmod`` follows. Failures are tolerated
    on filesystems that cannot represent the mode; the write itself proceeds.
    """
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass


def write_session_record(session_id: str, values: Mapping[str, str]) -> Path:
    """Atomically write a private session record.

    The record is written to a temp file in the records directory, fsynced,
    chmod 0600, and ``os.replace``-d into place; the directory itself is
    created or tightened to 0700. A partial write is never visible at the final
    path. Stale records are garbage-collected after a successful write.
    """
    path = session_record_path(session_id)
    _ensure_private_dir(path.parent)
    content = "".join(f"{key}={value}\n" for key, value in sorted(values.items()))
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".env")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    gc_session_records()
    return path


def gc_session_records(max_age_days: int = _SESSION_RECORD_MAX_AGE_DAYS) -> int:
    """Remove session records older than *max_age_days*; return count removed."""
    root = session_records_dir()
    if not root.is_dir():
        return 0
    cutoff = time.time() - max_age_days * 24 * 60 * 60
    removed = 0
    for path in root.glob("*.env"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def declared_session_lineage(
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Return the lineage declared in the *current* session record, or ``None``.

    The stable source once declared. ``None`` means either no session id
    resolves or the session has not declared — callers then fall through to
    explicit/env/suite.
    """
    session_id = harness_session_id(env)
    if not session_id:
        return None
    return read_session_record(session_id).get(MODEL_LINEAGE_ENV) or None


def resolve_model_lineage(
    *,
    explicit: str | None = None,
    session_id: str | None = None,
    env: Mapping[str, str] | None = None,
    suite: Mapping[str, str] | None = None,
) -> tuple[str | None, str | None]:
    """Resolve the model lineage through the session-scoped precedence chain.

    Precedence (WI-067, revised per cross-lineage review):

        session record (once declared) > explicit --model-lineage > process env
        > per-user suite.env > system suite.env > ``None``

    The session record is the stable source once declared: a declared lineage
    wins over an explicit flag so the same session cannot manufacture
    cross-lineage independence mid-session. When the explicit flag contradicts
    a declared record, :class:`SessionIdentityConflictError` is raised (fail-closed)
    rather than silently relabeling the session.

    ``session_id`` may be supplied explicitly (``session status
    --session-id``) for harnesses that cannot export their session id to tool
    subprocesses; when omitted, the id is read from the environment.

    Returns ``(lineage, source)`` where ``source`` is one of
    ``"session_record"``, ``"explicit"``, ``"env"``, ``"suite_env"`` or
    ``None`` when nothing resolved. An empty-string value is treated as unset.
    """
    source = dict(os.environ) if env is None else dict(env)
    resolved_session_id = session_id or harness_session_id(source)
    if resolved_session_id:
        record = read_session_record(resolved_session_id)
        from_record = record.get(MODEL_LINEAGE_ENV)
        if from_record:
            if explicit and explicit != from_record:
                raise SessionIdentityConflictError(
                    f"session {resolved_session_id} has already declared lineage "
                    f"{from_record!r}; explicit --model-lineage {explicit!r} "
                    "cannot change a session's identity mid-session (a "
                    "declared session record is the stable source for the "
                    "cross-lineage review gate)."
                )
            return from_record, "session_record"
    if explicit:
        return explicit, "explicit"
    from_env = source.get(MODEL_LINEAGE_ENV)
    if from_env:
        return from_env, "env"
    if suite is None:
        suite = load_suite_env()
    from_suite = suite.get(MODEL_LINEAGE_ENV)
    if from_suite:
        return from_suite, "suite_env"
    return None, None


def _is_canonical_lineage(lineage: str) -> bool:
    """Whether *lineage* is a canonical family per regista's closed registry.

    A declared value that is not a canonical family is *unresolvable*, not a
    passing declaration: the closed vocabulary is the guarantee the probe
    measures (the same rule regista's own probe enforces). A missing regista
    package degrades to ``False`` (fail-closed) rather than claiming
    resolvability we cannot verify.
    """
    try:
        from regista import MODEL_LINEAGE_FAMILIES
    except ImportError:
        return False
    return lineage in MODEL_LINEAGE_FAMILIES


def canonical_lineage_families() -> tuple[str, ...] | None:
    """Return the sorted canonical lineage families, or ``None`` if regista is
    missing. ``None`` (not empty) lets callers distinguish "regista absent"
    from "registry empty" and emit a clean contract error."""
    try:
        from regista import MODEL_LINEAGE_FAMILIES
    except ImportError:
        return None
    return tuple(sorted(MODEL_LINEAGE_FAMILIES))


def session_identity_probe() -> dict[str, object]:
    """Measure whether a session-scoped identity resolves (WI-067 / genesis).

    Emits the ``agent_notes.session_identity_resolvable`` check: ``pass`` only
    when a lineage resolves through the precedence chain AND is a canonical
    family; ``fail`` (fail-closed) when nothing resolves or the declared value
    is not a canonical family. The detail names the source so an auditor can
    see whether identity came from a session record (correct), the process env
    (launcher-declared) or suite.env (host-wide, single-model-hosts only).
    """
    session_id = harness_session_id()
    session_source = harness_session_source()
    lineage, source = resolve_model_lineage()
    resolvable = lineage is not None and _is_canonical_lineage(lineage)
    if lineage is None:
        detail = (
            "no model lineage resolves: declare one for this session with "
            "'agent-notes session declare --model-lineage <family>' (or set "
            f"{MODEL_LINEAGE_ENV} in the process env / suite.env)"
        )
    elif not _is_canonical_lineage(lineage):
        detail = (
            f"declared lineage {lineage!r} is not a canonical family "
            f"(source: {source}); unresolved values are not resolvable identity"
        )
    else:
        session_note = f"session {session_id}" if session_id else "no harness session id"
        fallback_note = (
            " (AGENT_NOTES_SESSION fallback only)"
            if session_source == _FALLBACK_SESSION_ENV
            else ""
        )
        detail = f"lineage resolved from {source} ({session_note}{fallback_note})"
    return {
        "id": "agent_notes.session_identity_resolvable",
        "status": "pass" if resolvable else "fail",
        "detail": detail,
        "session_id": session_id,
        "session_source": session_source,
        "lineage": lineage,
        "source": source,
    }
