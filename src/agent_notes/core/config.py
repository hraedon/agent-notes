"""DSN resolution (decision: harness-independent config).

The Postgres DSN is resolved through a precedence chain so the CLI works under
any launcher. The ``AGENT_NOTES_DSN`` environment variable stays the highest
priority — Claude Code injects it via settings.json, CI passes it as env, and
the import scripts swap it per-invocation. But the env var is *not* reliably
propagated everywhere (e.g. a non-interactive shell skips ~/.bashrc, so an
opencode session launched that way never sees the export). A config file under
the platform config dir (``~/.config/agent-notes/`` on Linux,
``%APPDATA%/agent-notes/`` on Windows) is the durable default beneath the env
var, mirroring agent-wake's ``~/.config/agent-wake/config.json`` convention.
The config file *path* itself resolves via ``AGENT_NOTES_CONFIG`` (explicit
override) or the platformdirs default (honoring ``XDG_CONFIG_HOME`` on Linux,
``%APPDATA%`` on Windows); no other suite env var affects the path.

Precedence: explicit argument > AGENT_NOTES_DSN env > suite.env
(per-user > system; bootstrap-contract §2, WI-051) > config file ``dsn`` key.

Regista write-through is retired (Plan 025 D7) and deleted (Plan 026 D1). The
only regista setting still read is the writes gate (``AGENT_NOTES_REGISTA_WRITES``
or ``regista.writes_enabled`` in the config file), and only so that a process
which still asks for it fails with :class:`RetiredSettingError` instead of
silently writing natively. ``REGISTA_DSN``, ``REGISTA_KEY_PATH`` and
``REGISTA_REQUIRE_SSL`` are no longer read; the native agent-notes DSN
(``AGENT_NOTES_DSN``) never fell back to ``REGISTA_DSN``.
"""

from __future__ import annotations

import json
import os
import re
import socket
from pathlib import Path

import platformdirs

from agent_notes.core.suite_env import load_suite_env

_DSN_ENV = "AGENT_NOTES_DSN"
_CONFIG_ENV = "AGENT_NOTES_CONFIG"  # override the config file path

# The retired regista writes gate (Plan 026 D1): read only to refuse it.
_REGISTA_WRITES_ENV = "AGENT_NOTES_REGISTA_WRITES"

# VM identity for memory tiers (Plan 025 WI-2.2).
_VM_ENV = "AGENT_NOTES_VM"


def _env_or_suite(canonical_env: str, suite: dict[str, str]) -> str | None:
    """Resolve one canonical value through process env and suite.env."""
    return os.environ.get(canonical_env) or suite.get(canonical_env) or None


def config_path(home: Path | None = None) -> Path:
    """Return the config file path.

    ``AGENT_NOTES_CONFIG`` overrides, else the platformdirs default
    (``~/.config/agent-notes/config.json`` on Linux,
    ``%APPDATA%/agent-notes/config.json`` on Windows). When ``home`` is
    given, the platformdirs default is constructed under it (test
    redirection); the override is skipped so the path is deterministic.
    """
    override = os.environ.get(_CONFIG_ENV)
    if override and home is None:
        return Path(override).expanduser()
    real = Path(platformdirs.user_config_dir("agent-notes"))
    if home is not None:
        try:
            real = home / real.relative_to(Path.home())
        except ValueError:
            pass
    return real / "config.json"


# Secret-backend refs (Plan 017 WI-4.1: ``env:``/``file:``/``literal:``/
# ``vault:``/``azure:``) were resolved by ``regista.secrets``. No host used one,
# so Plan 026 D3 dropped them with the regista dependency. A DSN carrying one of
# these prefixes is refused by name instead of being handed to psycopg.
_RETIRED_SECRET_REF_PREFIXES = frozenset({"env", "file", "literal", "vault", "azure"})


def _resolve_secret_value(value: str) -> str:
    """Return a literal DSN unchanged; refuse a retired secret-backend ref.

    The message names only the prefix, never the configured value.
    """
    prefix = value.split(":", 1)[0].lower() if ":" in value else None
    if prefix in _RETIRED_SECRET_REF_PREFIXES:
        raise RetiredSettingError(
            f"the configured Postgres DSN is a {prefix}: secret-backend ref, and "
            "secret-backend refs were removed with the regista dependency "
            "(Plan 026 D3). Set a literal postgresql:// DSN in "
            f"{_DSN_ENV}, suite.env or {config_path()}."
        )
    return value


def resolve_dsn(explicit: str | None = None) -> str:
    """Return the Postgres DSN or raise RuntimeError with actionable guidance.

    Precedence: ``explicit`` arg > ``AGENT_NOTES_DSN`` env > suite.env
    (per-user > system, WI-051) > config file ``dsn`` key.

    The suite.env layer is the bootstrap-contract §2 chain (process env >
    ``~/.config/agent-suite/suite.env`` > ``/etc/agent-suite/suite.env``): the
    bootstrap writes ``AGENT_NOTES_DSN`` into suite.env and every suite tool is
    expected to resolve it from there without the operator exporting anything.
    The tool-specific config file stays as the final fallback beneath the
    suite-wide facts.

    Every source must hold a literal DSN. A secret-backend ref (``env:VAR``,
    ``vault:...``, ``file:/path``) in any of them raises
    :class:`RetiredSettingError` (Plan 026 D3).
    """
    if explicit:
        return _resolve_secret_value(explicit)

    env = os.environ.get(_DSN_ENV)
    if env:
        return _resolve_secret_value(env)

    suite = load_suite_env()
    suite_dsn = suite.get(_DSN_ENV)
    if suite_dsn:
        return _resolve_secret_value(suite_dsn)

    path = config_path()
    if path.is_file():
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"agent-notes config at {path} could not be read: {exc}") from exc
        dsn = data.get("dsn")
        if dsn:
            return _resolve_secret_value(dsn)

    from agent_notes.core.suite_env import system_suite_env_path, user_suite_env_path

    raise RuntimeError(
        "No Postgres DSN found. Set AGENT_NOTES_DSN in the environment, or in "
        f"a suite.env file (searched {user_suite_env_path()}, then "
        f"{system_suite_env_path()}), or create {config_path()} containing "
        '{"dsn": "postgresql://user:pass@host/agent_notes"}.'
    )


# ---------------------------------------------------------------------------
# Retired regista write-through (Plan 026 D1)
# ---------------------------------------------------------------------------

_REGISTA_CUTOVER_DOC = "docs/spikes/025-regista-cutover.md"


class RetiredSettingError(RuntimeError):
    """A configuration setting names a feature that has been removed."""

    code = "RETIRED_SETTING"

    def __init__(self, message: str) -> None:
        super().__init__(f"{self.code}: {message}")


def regista_config_from_file() -> dict:
    """Return the ``regista`` block from the config file, or ``{}``.

    Reads the same path as :func:`config_path` (honors ``AGENT_NOTES_CONFIG`` +
    XDG). A missing or malformed file (or no ``regista`` key) degrades to
    ``{}`` rather than raising. The only key still read is ``writes_enabled``,
    so that a config that asks for the retired write-through is refused.
    """
    path = config_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    block = data.get("regista")
    return block if isinstance(block, dict) else {}


def _parse_bool(env_value: str | None, file_value: object) -> bool:
    """Resolve a boolean flag with env > file precedence.

    Strings use the canonical truthy set ``{1,true,yes}`` (so a JSON ``"false"``
    string stays False, unlike ``bool("false")``). An empty env var is treated
    as unset and falls back to the file value, mirroring :func:`resolve_dsn`.
    """
    raw = env_value if env_value else file_value
    if isinstance(raw, str):
        return raw.lower() in {"1", "true", "yes"}
    return bool(raw)


def regista_writes_requested() -> bool:
    """True when ``AGENT_NOTES_REGISTA_WRITES`` or ``regista.writes_enabled`` is on."""
    return _parse_bool(
        os.environ.get(_REGISTA_WRITES_ENV),
        regista_config_from_file().get("writes_enabled", False),
    )


def reject_regista_writes() -> None:
    """Refuse a process that still asks for regista write-through.

    Plan 025 D7 turned the write-through off and Plan 026 D1 deleted it: the
    native op-log is the only write path. Writing natively while the operator
    asked for regista would be a silent fallback, so the retired setting is an
    error that names the fix.
    """
    if regista_writes_requested():
        raise RetiredSettingError(
            f"{_REGISTA_WRITES_ENV} (or regista.writes_enabled in {config_path()}) is on, "
            "but regista write-through has been removed (Plan 026 D1); the native "
            f"op-log is the only write path. Unset {_REGISTA_WRITES_ENV} and remove "
            f"regista.writes_enabled from the config file. See {_REGISTA_CUTOVER_DOC}."
        )


# ---------------------------------------------------------------------------
# VM identity (Plan 025 WI-2.2)
# ---------------------------------------------------------------------------

_VM_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,61}[a-z0-9])?$")
# Kubernetes name generators draw from this alphabet (no vowels, no 0/1/3).
_K8S_RAND = "[bcdfghjklmnpqrstvwxz2456789]"
_GENERATED_HOSTNAME_RES = (
    # Docker/Podman container ID (short or full).
    re.compile(r"^(?:[0-9a-f]{12}|[0-9a-f]{64})$"),
    # Deployment/ReplicaSet pod: <name>-<template-hash>-<5 chars>; CronJob pod:
    # <name>-<epoch minutes>-<5 chars>.
    re.compile(rf"^.+-(?:{_K8S_RAND}{{8,10}}|\d{{6,10}})-{_K8S_RAND}{{5}}$"),
    # Job / DaemonSet pod: <name>-<5 chars>.
    re.compile(rf"^.+-{_K8S_RAND}{{5}}$"),
)
_UNSTABLE_HOSTNAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})


class VmIdError(ValueError):
    """The VM ID could not be resolved to something stable."""


def _looks_generated(hostname: str) -> bool:
    if hostname in _UNSTABLE_HOSTNAMES:
        return True
    return any(rx.match(hostname) for rx in _GENERATED_HOSTNAME_RES)


def resolve_vm_id(explicit: str | None = None) -> str:
    """Return the VM ID that names this machine's memory scope.

    Precedence: ``explicit`` (``--vm``) > ``AGENT_NOTES_VM`` (process env, then
    suite.env) > short lowercase hostname. A hostname that looks generated
    (container ID, k8s pod name, ``localhost``) is refused, because VM memories
    filed under a random name scatter and are never read again. Raises
    :class:`VmIdError` with an actionable message.
    """
    configured = explicit or _env_or_suite(_VM_ENV, load_suite_env())
    if configured:
        vm_id = configured.strip().lower()
        if not _VM_ID_RE.match(vm_id):
            raise VmIdError(
                f"invalid VM ID {configured!r}: use lowercase letters, digits, '.', '_' or '-' "
                "(max 63 characters)"
            )
        return vm_id

    hostname = socket.gethostname().split(".")[0].strip().lower()
    if not hostname or not _VM_ID_RE.match(hostname) or _looks_generated(hostname):
        raise VmIdError(
            f"cannot derive a stable VM ID from hostname {hostname!r}: it looks generated "
            "or unstable (container, pod, or localhost). Set AGENT_NOTES_VM (environment "
            "or suite.env) or pass --vm."
        )
    return hostname
