"""Shared hermetic runner for CLI-subprocess tests (WI-030).

``test_cli.py`` and ``test_memory_provider_cli.py`` each grew their own ``_run``
that copied ``os.environ`` into the child. That leaks the operator's environment
into the CLI subprocess and — more subtly — lets engine/config selection read the
operator's real ``suite.env`` via platformdirs discovery (``$XDG_CONFIG_HOME`` /
``$HOME`` -> ``~/.config/agent-suite/suite.env``). On a bootstrapped host that can
flip the resolved memory engine or route writes at the production spine.

This module is the single implementation both modules use. It strips caller-named
keys, then applies discovery pins *last* so a caller-supplied env dict (even a
full ``os.environ`` copy) cannot undo the hermeticity. The pins mirror the
WI-029 fix first landed inline in ``test_memory_provider_cli.py`` (PR #17).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path

_CLI = [sys.executable, "-m", "agent_notes.cli"]

# Lazily-created empty home shared by every CLI subprocess in the test session.
# Discovery pins point here so platformdirs-based lookups find nothing.
_HERMETIC_HOME: Path | None = None


def hermetic_home() -> Path:
    """Return the session-wide empty home used to pin CLI subprocess discovery."""
    global _HERMETIC_HOME
    if _HERMETIC_HOME is None:
        _HERMETIC_HOME = Path(tempfile.mkdtemp(prefix="agent-notes-cli-home-"))
    return _HERMETIC_HOME


def discovery_pins() -> dict[str, str]:
    """Env pins that mask host suite.env / config-file discovery.

    The override vars point at nonexistent files (the only way to mask
    ``/etc/agent-suite/suite.env``, which a HOME redirect cannot cover); the
    platformdirs discovery root (``XDG_CONFIG_HOME``) points at an empty temp
    dir so code paths that skip the override vars find nothing either.

    ``HOME`` itself is deliberately *not* pinned: CPython resolves user
    site-packages (``~/.local/lib/pythonX.Y/site-packages``) relative to HOME,
    so overriding it would hide packages installed there — notably ``regista``
    on hosts that install per-user — and break every CLI subprocess at import.
    Config discovery does not need HOME: the override vars above take precedence
    on every platform, and ``platformdirs.user_config_dir`` honours
    ``XDG_CONFIG_HOME`` for the no-override fallback.
    """
    home = hermetic_home()
    missing = home / "nonexistent"
    return {
        "XDG_CONFIG_HOME": str(home / ".config"),
        "AGENT_SUITE_CONFIG": str(missing / "suite.env"),
        "AGENT_SUITE_SYSTEM_CONFIG": str(missing / "suite.env"),
        "AGENT_NOTES_CONFIG": str(missing / "config.json"),
    }


def build_cli_env(
    env: Mapping[str, str] | None = None,
    strip_keys: Iterable[str] = (),
) -> dict[str, str]:
    """Assemble the hermetic environment for a CLI subprocess.

    Inherited environment is copied minus ``strip_keys`` (module-specific live
    values, e.g. the hindsight engine vars); ``env`` is layered on top; the
    discovery pins are applied last so they always win — even over a caller that
    passes a full ``os.environ`` copy.
    """
    stripped = set(strip_keys)
    merged = {k: v for k, v in os.environ.items() if k not in stripped}
    merged.update(env or {})
    merged.update(discovery_pins())
    return merged


def run_cli(
    *args: str,
    env: Mapping[str, str] | None = None,
    check: bool = True,
    strip_keys: Iterable[str] = (),
) -> subprocess.CompletedProcess[str]:
    """Run the CLI as a hermetic subprocess and return the CompletedProcess."""
    return subprocess.run(
        _CLI + list(args),
        capture_output=True,
        text=True,
        env=build_cli_env(env, strip_keys),
        check=check,
    )
