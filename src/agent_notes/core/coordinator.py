"""Degrade contract and coordination mode detection (Plan 008 P4 Tier A).

The coordinator-absent mode is the **default, first-class safe mode**.
When no coordinator is configured (the normal state for single-/few-writer
installations), the kernel uses the local ``work_item_leases`` table for
claim/heartbeat/release. Reads, progress on already-held items, and append/file
all work freely.

If a coordinator is configured (``AGENT_NOTES_COORDINATOR_URL``), the system
checks reachability before attempting distributed claims. If the coordinator is
down, the system falls back to local-lease mode with the constraint that **no
new claims** are issued (the only operation that races).

This module provides the configuration layer and the ``doctor`` check.
The actual coordinator integration (regista ``_claims_api.py``) is Tier B and
must not be attempted until Tier A is complete.
"""

from __future__ import annotations

import os

# Environment variable for coordinator URL. When absent, the system operates
# in local-lease mode (the default, safe mode).
_COORDINATOR_URL_ENV = "AGENT_NOTES_COORDINATOR_URL"


def get_coordinator_url() -> str | None:
    """Return the configured coordinator URL, or None if not configured."""
    return os.environ.get(_COORDINATOR_URL_ENV)


def get_coordination_mode() -> str:
    """Return the current coordination mode string.

    Returns one of:
    - ``coordinator-absent / local-lease`` (default, safe mode)
    - ``coordinator-present / local-lease`` (configured but unreachable,
      new claims blocked)
    - ``coordinator-present / distributed-lease`` (full multi-agent mode,
      Tier B, not yet implemented)
    """
    url = get_coordinator_url()
    if not url:
        return "coordinator-absent / local-lease"

    # Tier B: when coordinator is configured, check reachability.
    # For Tier A, we only report that it's configured; the actual
    # reachability check is a Tier B concern.
    return "coordinator-present / local-lease (Tier B pending)"


def is_distributed_claim_available() -> bool:
    """Return True if distributed claims via the coordinator are available.

    For Tier A, this always returns False because the coordinator integration
    is Tier B. This function is the guard: ``claim_work_item`` checks it
    and falls back to local lease when False.
    """
    return False


def check_coordinator_health() -> tuple[bool, str]:
    """Check if the coordinator is reachable.

    For Tier A, this always returns (False, "not configured") because the
    coordinator integration is Tier B. The degrade contract is that when
    the coordinator is absent, the system operates in local-lease mode.

    Returns (healthy, message).
    """
    url = get_coordinator_url()
    if not url:
        return False, "coordinator-absent — local-lease mode (default)"

    # Tier B: would probe the coordinator here. For Tier A, we report
    # that the coordinator is configured but not yet integrated.
    return False, f"coordinator configured at {url} but integration is Tier B (pending)"
