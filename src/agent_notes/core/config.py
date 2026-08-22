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

The regista face config (``RegistaConfig``) follows the same harness-independent
principle: each field resolves env var > ``regista`` block in the config file.
The file fallback exists for the same reason as the native DSN — a non-interactive
launcher (e.g. opencode) skips ``~/.bashrc`` so env exports never arrive; the
config file is readable under any launcher. regista is an optional face, so a
missing/malformed ``regista`` block degrades silently (never raises), unlike the
mandatory native DSN.

Suite config adoption (Plan 017 WI-1.1): the three shared facts that every
suite consumer reads — the regista DSN, the signing-key path, and the SSL flag —
are sourced from the canonical suite env vars (``REGISTA_DSN``,
``REGISTA_KEY_PATH``, ``REGISTA_REQUIRE_SSL``), the names regista/dossier/cairn
also use. The per-tool slug is ``AGENT_NOTES_PROJECT`` and the writes gate
(``AGENT_NOTES_REGISTA_WRITES``) remains tool-specific. There are no deprecated
configuration aliases: an unrecognised name is ignored.

Note: ``REGISTA_DSN`` is used only by the optional regista face; the native
agent-notes DSN (``AGENT_NOTES_DSN``) does not fall back to it. The deployment
model uses separate regista and agent-notes databases, so a missing
``AGENT_NOTES_DSN`` is an actionable failure, not a silent connection to the
regista database.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import platformdirs

from agent_notes.core.suite_env import load_suite_env

_DSN_ENV = "AGENT_NOTES_DSN"
_CONFIG_ENV = "AGENT_NOTES_CONFIG"  # override the config file path

# Canonical suite env vars (Plan 017 WI-1.1). These are the shared facts every
# suite consumer reads, sourced from one suite.env.
_SUITE_REGISTA_DSN_ENV = "REGISTA_DSN"
_SUITE_REGISTA_KEY_ENV = "REGISTA_KEY_PATH"
_SUITE_REGISTA_SSL_ENV = "REGISTA_REQUIRE_SSL"

# Canonical suite var for the per-consumer project slug (blueprint §2.6 /
# bootstrap-contract §2: "per-consumer <TOOL>_PROJECT"). The per-user suite.env
# overlay sets this (multi-user-onboarding §3).
_SUITE_PROJECT_ENV = "AGENT_NOTES_PROJECT"

# Tool-specific (not a shared fact) — keeps its AGENT_NOTES_* name.
_REGISTA_WRITES_ENV = "AGENT_NOTES_REGISTA_WRITES"
_REGISTA_PROJECT_DEFAULT = "agent_notes"


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


def _resolve_secret_value(value: str) -> str:
    """Route a configured value through the suite secret resolver (Plan 017 WI-4.1).

    A literal DSN (no provider prefix) is returned unchanged — zero regression
    for plaintext deployments, and regista is not even imported for that case.
    A backend ref (``env:``/``vault:``/``azure:``/``file:``) resolves to the
    real DSN via ``regista.secrets.resolve_str``. Import is local so the common
    literal path keeps this module import-light.
    """
    from agent_notes.core import secrets as suite_secrets

    return suite_secrets.resolve_dsn(value)


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

    A backend ref (``env:VAR`` / ``vault:...`` / ``file:/path``) is resolved
    through ``regista.secrets`` (Plan 017 WI-4.1) so the DSN password need not
    live in plaintext config; a literal DSN passes through unchanged. This
    applies uniformly to the explicit arg, the env var, the suite.env value,
    and the file value — any of them may carry a backend ref.
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
# regista face config (Plan 009)
# ---------------------------------------------------------------------------


def regista_config_from_file() -> dict:
    """Return the ``regista`` block from the config file, or ``{}``.

    Reads the same path as :func:`config_path` (honors ``AGENT_NOTES_CONFIG`` +
    XDG). regista is an optional face, so a missing or malformed file (or no
    ``regista`` key) degrades to ``{}`` rather than raising — contrast
    :func:`resolve_dsn`, where the native DSN is mandatory and a bad file is
    fatal.
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


class RegistaConfig:
    """Resolved regista face configuration (Plan 009 D1/D2).

    ``enabled`` is True only when a regista DSN is configured AND the writes gate
    is on. When False, the legacy op_log path is used unchanged. Each field
    resolves through the suite precedence (Plan 017 WI-1.1 + WI-4.2):

        process env > per-user suite.env > system suite.env
        > tool config file    (``regista`` block in config.json; ``key_path``
        for the signing key-set manifest)

    The suite.env layer is the per-user overlay (blueprint §2.6): each human's
    ``principal_id`` and default project live there, layered on the system-wide
    shared facts (DSN host, secret-backend pointers).
    """

    def __init__(self) -> None:
        file_cfg = regista_config_from_file()
        suite = load_suite_env()

        # DSN / key / SSL resolve: process env > suite.env > config file
        # (Plan 017 WI-1.1 + WI-4.2). An empty env var falls through.
        env_dsn = _env_or_suite(_SUITE_REGISTA_DSN_ENV, suite)
        file_dsn = file_cfg.get("dsn")
        self.dsn: str | None = env_dsn or (file_dsn if isinstance(file_dsn, str) else None)

        # Project slug: canonical AGENT_NOTES_PROJECT process env > suite.env
        # > default. The per-user overlay sets AGENT_NOTES_PROJECT
        # (multi-user-onboarding §3).
        self.project: str = (
            os.environ.get(_SUITE_PROJECT_ENV)
            or suite.get(_SUITE_PROJECT_ENV)
            or _REGISTA_PROJECT_DEFAULT
        )

        env_key = _env_or_suite(_SUITE_REGISTA_KEY_ENV, suite)
        file_key = file_cfg.get("key_path")
        self.key_path: str | None = env_key or (
            file_key if isinstance(file_key, str) else None
        )

        ssl_env = _env_or_suite(_SUITE_REGISTA_SSL_ENV, suite)
        self.require_ssl: bool = _parse_bool(ssl_env, file_cfg.get("require_ssl", False))
        gate = _parse_bool(
            os.environ.get(_REGISTA_WRITES_ENV), file_cfg.get("writes_enabled", False)
        )
        self.writes_enabled: bool = gate and self.dsn is not None

    @property
    def enabled(self) -> bool:
        return self.writes_enabled


def regista_config() -> RegistaConfig:
    """Return resolved regista face config (read fresh from env each call)."""
    return RegistaConfig()
