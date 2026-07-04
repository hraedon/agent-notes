"""Secret-backend resolution for the suite (Plan 017 WI-4.1).

Wraps regista's secret-backend resolver (regista Plan 025 WI-1.2) so agent-notes'
suite-shared secrets — the regista DSN and the signing key-set — may live in
Vault / Azure Key Vault / env / file rather than a plaintext config value. The
blueprint (§2.5) makes this the custody contract for a regulated multi-user
deployment; agent-notes is a *consumer* — regista defines the ref syntax and
the providers, this module routes agent-notes' configured values through it.

Two resolution kinds:

1. ``resolve_dsn(value)`` — for a *string* secret (a full DSN). A literal DSN
   (``postgresql://user:pw@host/db``) has no registered provider prefix, so it
   is returned unchanged — zero regression for today's plaintext-DSN installs,
   and regista is not even imported for the common case. A backend ref
   (``env:VAR``, ``vault:mount/path/key``, ``azure:name``, ``file:/path``)
   resolves to the DSN string at use time via ``regista.secrets.resolve_str``.

2. ``materialize_key_manifest(value)`` — for the signing *key-set*. regista's
   ``KeySet`` reads a JSON manifest from a path and already resolves per-key
   ``secret_ref`` from the backend (regista ``_keys.py``), so the manifest
   itself need not contain secrets. A bare filesystem path (today's default)
   is returned unchanged so regista reads + mtime-polls it directly — no temp
   file, no regression. A remote backend ref (``env:``/``vault:``/``azure:``)
   resolves to bytes and is written to a private (0600) temp file owned by
   this process; regista reads that path. The temp file is scrubbed at
   interpreter exit (``atexit``), so no persisted material survives a clean
   shutdown — strictly better than a permanent plaintext key in
   ``~/.config/``. mtime-poll hot-reload is a no-op on this path (the temp
   file is static); rotating a backend-sourced manifest means restarting the
   face, documented in the plan log.

Security note: resolution-failure messages surface only the exception *type*
(``RuntimeError("...: VaultError")``), never ``str(exc)`` — a backend error
may echo the ref or partial material, and doctor/JSON output may land in
aggregators. The ref itself (the configured value) is not secret, but a Vault
path could be sensitive in some deployments, so we keep messages type-only and
suppress the ``__cause__`` chain (``from None``). That is a deliberate
security-over-debuggability trade: a logged traceback will not show the
original regista exception (which can echo refs/paths), at the cost of needing
to reproduce locally with the raw resolver to see full backend diagnostics.
"""

from __future__ import annotations

import atexit
import os
import threading
from pathlib import Path
from typing import Callable, Optional

# Providers whose prefix marks a value as a *remote* backend ref requiring
# materialization to a temp file (the manifest cannot live on the local FS in
# a form regista's KeySet reads directly). ``file:`` and bare paths are NOT in
# this set — regista reads those directly.
#
# This set is ALSO the gate for the silent-literal-fallback guard in
# ``resolve_dsn``: only these providers' registration is conditional on an
# optional SDK (hvac / azure-identity), so only they can be silently
# reclassified as ``literal`` by regista when the SDK is absent. ``env`` and
# ``file`` are always registered and either resolve or raise — they can never
# silently fall back, so they must NOT be in this set (a self-referential
# ``env:VAR`` whose value is the string ``env:VAR`` would otherwise trip a
# bogus "install regista[env]" error).
_REMOTE_PROVIDERS = frozenset({"vault", "azure"})

# Every provider prefix regista's ``_detect_prefix`` recognises. Used to decide
# whether a value warrants *any* resolution at all (vs literal passthrough).
# NOTE: ``akv`` is intentionally absent — regista registers no ``akv`` provider
# (only file/env/literal/vault/azure); the blueprint's ``akv:`` syntax awaits a
# regista-side provider. Use ``azure:`` here. See Plan 017 WI-4.1 log.
_ALL_PROVIDERS = frozenset({"env", "file", "literal", "vault", "azure"})

CleanupFn = Callable[[], None]

# Process-wide registry of materialized temp files, scrubbed at interpreter
# exit so a caller that forgets an explicit cleanup does not leak key material
# past the process. Owned by ``_temp_lock``.
_temp_files: list[Path] = []
_temp_lock = threading.Lock()
_atexit_registered = False


# ---------------------------------------------------------------------------
# predicates
# ---------------------------------------------------------------------------


def _provider_prefix(value: str) -> Optional[str]:
    """Return the lowercase provider prefix if ``value`` has a recognised one.

    ``postgresql://...`` → ``None`` (literal DSN, no resolution). ``env:FOO`` →
    ``"env"``. ``/etc/keys.json`` → ``None`` (bare path). Mirrors regista's
    ``_detect_prefix`` recognition set. Recognition is case-insensitive
    (``ENV:``/``Vault:`` are refs); ``_normalize_for_regista`` lowercases the
    prefix before the regista call because regista's provider names are
    lowercase and it does NOT lowercase itself.
    """
    if ":" not in value:
        return None
    prefix = value.split(":", 1)[0].lower()
    return prefix if prefix in _ALL_PROVIDERS else None


def _normalize_for_regista(value: str) -> str:
    """Lowercase the provider prefix so ``ENV:VAR`` resolves like ``env:VAR``.

    Only the prefix (before the first ``:``) is lowercased — the remainder
    (env var name, Vault path, AKV secret name) is case-sensitive and passed
    through unchanged. regista's provider registry is lowercase and its
    ``_detect_prefix`` does not lowercase, so without this an ``ENV:`` ref
    would be silently reclassified as ``literal``.
    """
    prefix, sep, rest = value.partition(":")
    return f"{prefix.lower()}{sep}{rest}"


def is_backend_ref(value: Optional[str]) -> bool:
    """True if ``value`` carries a recognised secret-backend provider prefix."""
    return bool(value) and _provider_prefix(value) is not None


# ---------------------------------------------------------------------------
# resolver import
# ---------------------------------------------------------------------------


def _resolver():
    """Import ``regista.secrets`` lazily (regista is a hard dep).

    Deferred so importing this module never costs a regista import for paths
    that turn out to be literal (the common case resolves without regista).
    """
    from regista import secrets as _secrets

    return _secrets


# ---------------------------------------------------------------------------
# DSN resolution
# ---------------------------------------------------------------------------


def resolve_dsn(value: Optional[str]) -> Optional[str]:
    """Resolve a DSN-or-ref to the DSN string.

    - ``None`` / empty → ``None`` (caller treats absence as "not configured").
    - Literal DSN (no provider prefix) → unchanged; regista is not imported.
    - Backend ref (``env:``/``vault:``/``azure:``/``file:``) → resolved via
      ``regista.secrets.resolve_str``.

    Resolution failures raise ``RuntimeError`` (type-only message) so a
    misconfigured backend is visible rather than silently degrading. The
    ``__cause__`` chain is suppressed (``from None``) so a logged traceback
    cannot echo the original regista error, which may include the ref or a
    partial Vault path.

    Silent-literal-fallback guard: when a remote provider's SDK is absent
    (e.g. ``hvac`` not installed), regista's ``_detect_prefix`` reclassifies
    the ref as ``literal`` and returns the ref *string* unchanged — which would
    then be handed to ``psycopg.connect`` as a bogus DSN. We detect that
    (resolved value equal to the input ref) and fail loudly instead.
    """
    if not value:
        return value
    prefix = _provider_prefix(value)
    if prefix is None:
        return value  # literal DSN — no resolution, no regista import
    normalized = _normalize_for_regista(value)
    try:
        result = _resolver().resolve_str(normalized)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to resolve DSN secret: {type(exc).__name__}"
        ) from None
    if prefix in _REMOTE_PROVIDERS and result == normalized:
        raise RuntimeError(
            f"DSN ref did not resolve — provider '{prefix}' may be missing its "
            f"SDK (install regista[{prefix}]) or the backend is unreachable"
        )
    return result


# ---------------------------------------------------------------------------
# key-set manifest resolution
# ---------------------------------------------------------------------------


def materialize_key_manifest(
    value: Optional[str],
) -> tuple[Optional[str], Optional[CleanupFn]]:
    """Resolve the signing key-set path, materializing a remote ref to a temp file.

    Returns ``(path_or_none, cleanup_or_none)``:

    - ``value`` empty/None → ``(None, None)``.
    - Bare path or ``file:`` ref → ``(expanded_path, None)``: regista reads +
      polls it directly; nothing to clean up (today's behavior, no regression).
      ``~`` is expanded here because regista's ``KeySet`` does not expand it.
    - Remote ref (``env:``/``vault:``/``azure:``) → resolves the manifest bytes,
      writes a 0600 temp file, returns ``(temp_path, cleanup)``. The temp file
      is also registered for scrub at interpreter exit. ``cleanup`` is idempotent.

    ``literal:`` is refused as a manifest source (a literal string is not a
    readable key-set path; surfacing this early is clearer than a JSON-parse
    failure from regista's KeySet later).
    """
    if not value:
        return None, None

    prefix = _provider_prefix(value)
    if prefix is None:
        # Bare filesystem path — regista reads it directly. Expand ~ since
        # regista's KeySet does Path(path).read_text() without expanduser.
        return str(_expand_path(value)), None

    if prefix == "file":
        return str(_expand_path(value.split(":", 1)[1])), None

    if prefix == "literal":
        raise RuntimeError(
            "literal: is not a valid key-set manifest source; use a file path "
            "or a backend ref (env:/vault:/azure:)"
        )

    # Remote backend ref — resolve bytes and materialize to a private temp file.
    normalized = _normalize_for_regista(value)
    try:
        data = _resolver().resolve(normalized)
        _validate_manifest_bytes(data)
    except RuntimeError:
        # Validation raises a clear, ref-safe message; let it through unchanged.
        raise
    except Exception as exc:
        raise RuntimeError(
            f"Failed to resolve key-set manifest: {type(exc).__name__}"
        ) from None
    path = _write_temp_manifest(data)
    return str(path), _make_cleanup(path)


def _expand_path(value: str) -> Path:
    """Expand ``~`` for a filesystem path (regista's KeySet does not)."""
    from pathlib import Path

    return Path(value).expanduser()


def _validate_manifest_bytes(data: bytes) -> None:
    """Confirm resolved bytes are a plausibly-shaped key-set before materializing.

    Catches the silent-literal-fallback case: when a backend provider's SDK is
    absent (e.g. ``hvac`` not installed), regista's ``_detect_prefix`` treats an
    unregistered prefix as ``literal`` and returns the ref *string* as the
    bytes. Without this check we would write ``b"vault:secret/..."`` to a temp
    file and let regista's KeySet fail later with a confusing JSON error. The
    message is generic (no echo of the ref); only the structural diagnosis.
    """
    import json

    try:
        parsed = json.loads(data)
    except ValueError:
        raise RuntimeError(
            "key-set manifest did not resolve to valid JSON — if using a "
            "backend ref (env:/vault:/azure:), confirm the provider SDK is "
            "installed, the prefix is lowercase, and the backend is reachable"
        ) from None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("keys"), list):
        raise RuntimeError(
            "key-set manifest must be a JSON object with a 'keys' array"
        )


# ---------------------------------------------------------------------------
# temp-file materialization + lifecycle
# ---------------------------------------------------------------------------


def _write_temp_manifest(data: bytes) -> Path:
    """Write ``data`` to a fresh owner-only temp file and register it for exit-scrub."""
    import tempfile

    # mkstemp creates the file mode 0600 (umask-modified) on POSIX; the explicit
    # chmod below is defensive. On Windows, mkstemp honours the inherited ACL
    # of %TEMP% (typically user-scoped) and chmod is a no-op (POSIX-only guard).
    fd, name = tempfile.mkstemp(prefix="an-keys-", suffix=".json")
    path = Path(name)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    _chmod_owner_only(path)
    with _temp_lock:
        _temp_files.append(path)
        _register_atexit()
    return path


def _chmod_owner_only(path: Path) -> None:
    """Restrict a path to owner-only on POSIX; no-op on Windows (ACLs inherited)."""
    if os.name == "posix":
        try:
            path.chmod(0o600)
        except OSError:
            # Best-effort; mkstemp already created it 0600 on POSIX.
            pass


def _make_cleanup(path: Path) -> CleanupFn:
    """Return an idempotent cleanup that scrubs ``path`` once."""
    done = threading.Event()

    def _cleanup() -> None:
        if done.is_set():
            return
        done.set()
        _scrub(path)

    return _cleanup


def _scrub(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        # If we cannot delete (permissions, open handle), leave it — atexit will
        # try again, and the file is owner-only 0600 regardless.
        pass
    with _temp_lock:
        try:
            _temp_files.remove(path)
        except ValueError:
            pass


def _register_atexit() -> None:
    global _atexit_registered
    if _atexit_registered:
        return
    _atexit_registered = True
    atexit.register(_scrub_all)


def _scrub_all() -> None:
    """Scrub every materialized temp file (interpreter-exit safety net)."""
    with _temp_lock:
        paths = list(_temp_files)
        _temp_files.clear()
    for p in paths:
        _scrub(p)


def materialized_temp_count() -> int:
    """Diagnostic: how many temp manifests are currently registered (tests)."""
    with _temp_lock:
        return len(_temp_files)
