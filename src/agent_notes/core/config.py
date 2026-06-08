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
"""

from __future__ import annotations

import json
import os
from pathlib import Path

_DSN_ENV = "AGENT_NOTES_DSN"
_CONFIG_ENV = "AGENT_NOTES_CONFIG"  # override the config file path


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
