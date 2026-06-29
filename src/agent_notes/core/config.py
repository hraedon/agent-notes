"""DSN resolution (decision: harness-independent config).

The Postgres DSN is resolved through a precedence chain so the CLI works under
any launcher. The ``AGENT_NOTES_DSN`` environment variable stays the highest
priority — Claude Code injects it via settings.json, CI passes it as env, and
the import scripts swap it per-invocation. But the env var is *not* reliably
propagated everywhere (e.g. a non-interactive shell skips ~/.bashrc, so an
opencode session launched that way never sees the export). A config file under
``~/.config/agent-notes/`` is the durable default beneath the env var, mirroring
agent-wake's ``~/.config/agent-wake/config.json`` convention.

Precedence: explicit argument > AGENT_NOTES_DSN env > config file ``dsn`` key.

The regista face config (``RegistaConfig``) follows the same harness-independent
principle: each field resolves env var > ``regista`` block in the config file.
The file fallback exists for the same reason as the native DSN — a non-interactive
launcher (e.g. opencode) skips ``~/.bashrc`` so env exports never arrive; the
config file is readable under any launcher. regista is an optional face, so a
missing/malformed ``regista`` block degrades silently (never raises), unlike the
mandatory native DSN.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

_DSN_ENV = "AGENT_NOTES_DSN"
_CONFIG_ENV = "AGENT_NOTES_CONFIG"  # override the config file path

# regista face config (Plan 009). When REGISTA_DSN is unset, regista writes are
# disabled and the legacy op_log path is used unchanged (feature gate).
_REGISTA_DSN_ENV = "AGENT_NOTES_REGISTA_DSN"
_REGISTA_PROJECT_ENV = "AGENT_NOTES_REGISTA_PROJECT"
_REGISTA_KEY_ENV = "AGENT_NOTES_REGISTA_HMAC_KEY_PATH"
_REGISTA_WRITES_ENV = "AGENT_NOTES_REGISTA_WRITES"
_REGISTA_SSL_ENV = "AGENT_NOTES_REGISTA_REQUIRE_SSL"
_REGISTA_PROJECT_DEFAULT = "agent_notes"


def config_path() -> Path:
    """Return the config file path (``AGENT_NOTES_CONFIG`` overrides, else XDG)."""
    override = os.environ.get(_CONFIG_ENV)
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "agent-notes" / "config.json"


def resolve_dsn(explicit: str | None = None) -> str:
    """Return the Postgres DSN or raise RuntimeError with actionable guidance.

    Precedence: ``explicit`` arg > ``AGENT_NOTES_DSN`` env > config file.
    """
    if explicit:
        return explicit

    env = os.environ.get(_DSN_ENV)
    if env:
        return env

    path = config_path()
    if path.is_file():
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"agent-notes config at {path} could not be read: {exc}") from exc
        dsn = data.get("dsn")
        if dsn:
            return dsn

    raise RuntimeError(
        "No Postgres DSN found. Set AGENT_NOTES_DSN, or create "
        f'{config_path()} containing {{"dsn": "postgresql://user:pass@host/agent_notes"}}.'
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
    resolves env var > ``regista`` block in the config file (Plan 012 WI-1).
    """

    def __init__(self) -> None:
        file_cfg = regista_config_from_file()

        env_dsn = os.environ.get(_REGISTA_DSN_ENV)
        file_dsn = file_cfg.get("dsn")
        self.dsn: str | None = (
            env_dsn if env_dsn else (file_dsn if isinstance(file_dsn, str) else None)
        )
        self.project: str = os.environ.get(_REGISTA_PROJECT_ENV) or _REGISTA_PROJECT_DEFAULT
        env_key = os.environ.get(_REGISTA_KEY_ENV)
        file_key = file_cfg.get("hmac_key_path")
        self.hmac_key_path: str | None = (
            env_key if env_key else (file_key if isinstance(file_key, str) else None)
        )

        self.require_ssl: bool = _parse_bool(
            os.environ.get(_REGISTA_SSL_ENV), file_cfg.get("require_ssl", False)
        )
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
