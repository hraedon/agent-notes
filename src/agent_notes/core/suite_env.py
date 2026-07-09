"""Suite.env layered config reader (Plan 017 WI-4.2).

Reads the suite-wide config files (``suite.env``) that ``agent-suite bootstrap``
writes, providing the per-user overlay layer between process env and the
tool-specific config file. The precedence (blueprint §2.6 / bootstrap-contract
§2):

    process env  >  per-user suite.env  >  system suite.env  >  tool default

The per-user file is at ``~/.config/agent-suite/suite.env`` (Linux) or
``%APPDATA%/agent-suite/suite.env`` (Windows), overridable via
``AGENT_SUITE_CONFIG``. The system file is at ``/etc/agent-suite/suite.env``
(Linux) or ``%ProgramData%/agent-suite/suite.env`` (Windows).

This mirrors regista's ``_config.py`` resolver (Plan 025 WI-1.1) so precedence
is identical everywhere — the bootstrap contract says "resolves values through
regista's loader so precedence is identical everywhere." agent-notes reads the
same files but resolves its own vars (``REGISTA_PRINCIPAL_ID``,
``AGENT_NOTES_PROJECT``) in addition to the shared ``REGISTA_*`` facts.

The per-user overlay holds that human's ``principal_id``, default project, and
personal harness wiring; the system file holds shared facts (DSN host,
secret-backend pointers). The overlay does not touch the system file — it
layers on top of it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import platformdirs

# Env var that overrides the per-user suite.env path (same as regista's).
_SUITE_USER_CONFIG_ENV = "AGENT_SUITE_CONFIG"
# Test-only override for the system suite.env path (regista has no equivalent;
# agent-notes adds this so tests can isolate from a host /etc/agent-suite/).
_SUITE_SYSTEM_CONFIG_ENV = "AGENT_SUITE_SYSTEM_CONFIG"


def user_suite_env_path() -> Path:
    """Return the per-user suite.env path.

    ``AGENT_SUITE_CONFIG`` overrides; otherwise the platformdirs default
    (``~/.config/agent-suite/suite.env`` on Linux,
    ``%APPDATA%/agent-suite/suite.env`` on Windows).
    """
    override = os.environ.get(_SUITE_USER_CONFIG_ENV)
    if override:
        return Path(override).expanduser()
    return Path(platformdirs.user_config_dir("agent-suite")) / "suite.env"


def system_suite_env_path() -> Path:
    """Return the system suite.env path.

    ``AGENT_SUITE_SYSTEM_CONFIG`` overrides (test only); otherwise
    ``/etc/agent-suite/suite.env`` on Linux,
    ``%ProgramData%/agent-suite/suite.env`` on Windows.
    """
    override = os.environ.get(_SUITE_SYSTEM_CONFIG_ENV)
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("ProgramData", r"C:\ProgramData")
        return Path(base) / "agent-suite" / "suite.env"
    return Path("/etc/agent-suite/suite.env")


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a ``KEY=VALUE`` env file into a dict.

    Mirrors regista's ``_config._parse_env_file``: skips blank/comment lines,
    strips ``export `` prefix, and unwraps matching quotes. A missing file
    returns ``{}``.
    """
    result: dict[str, str] = {}
    if not path.is_file():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
            value = value[1:-1]
        result[key] = value
    return result


def load_suite_env(
    user_path: Path | None = None,
    system_path: Path | None = None,
) -> dict[str, str]:
    """Return the merged suite.env dict (per-user > system).

    The per-user overlay takes precedence over the system file: system values
    are loaded first, then per-user values overwrite them. Process env is NOT
    included here — callers check ``os.environ`` separately (and with higher
    precedence) so the layering is:

        process env  >  per-user suite.env  >  system suite.env  >  tool default

    A missing file is silently skipped (returns ``{}`` from that layer). This
    is the durable default beneath the env var, not a hard requirement.
    """
    if user_path is None:
        user_path = user_suite_env_path()
    if system_path is None:
        system_path = system_suite_env_path()
    merged: dict[str, str] = {}
    merged.update(_parse_env_file(system_path))
    merged.update(_parse_env_file(user_path))
    return merged
