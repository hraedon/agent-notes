#!/usr/bin/env python3
"""Check the local sibling regista checkout against SUITE.lock (Plan 017 WI-4.1).

SUITE.lock pins the regista git SHA this agent-notes release was tested against.
In the dev layout regista is an editable sibling at ``../regista``; this script
compares that checkout's HEAD to the pinned SHA and reports drift. CI is N/A
(CI installs regista from ``@main``); this is a local-dev guard that catches a
stale or moved sibling checkout before you debug a phantom incompatibility.

Exit code is always 0 — the lock is a known-good pin, not a hard requirement
(being ahead of the pin is often fine). Use ``--strict`` to exit non-zero on
drift, e.g. for a pre-release sanity gate.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

_LOCK = Path(__file__).resolve().parent.parent / "SUITE.lock"
_REGISTA_DIR = Path(__file__).resolve().parent.parent.parent / "regista"


def _read_pinned_sha() -> str | None:
    if not _LOCK.is_file():
        return None
    text = _LOCK.read_text(encoding="utf-8")
    m = re.search(r"^sha\s*=\s*\"([0-9a-f]+)\"", text, re.MULTILINE)
    return m.group(1) if m else None


def _head_sha() -> str | None:
    if not (_REGISTA_DIR / ".git").exists():
        return None
    try:
        out = subprocess.run(
            ["git", "-C", str(_REGISTA_DIR), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero when the sibling checkout drifts from the pin.",
    )
    args = parser.parse_args()

    pinned = _read_pinned_sha()
    if pinned is None:
        print(f"SUITE.lock: no sha pin found in {_LOCK}")
        return 0

    head = _head_sha()
    if head is None:
        print(f"SUITE.lock pins regista @ {pinned[:12]}")
        print(f"  (no sibling checkout at {_REGISTA_DIR} to compare; skipping)")
        return 0

    if head == pinned:
        print(f"SUITE.lock OK: regista @ {pinned[:12]} matches pinned SHA")
        return 0

    short_pinned = pinned[:12]
    short_head = head[:12]
    print("SUITE.lock DRIFT:")
    print(f"  pinned: {short_pinned}")
    print(f"  local:  {short_head}")
    print(
        "  The sibling regista checkout does not match the SUITE.lock pin. The\n"
        "  pin records the SHA this agent-notes release was tested against; a\n"
        "  drift may be fine (newer regista) or may surface an untested\n"
        "  incompatibility. To match the pin:\n"
        f"    git -C {_REGISTA_DIR} checkout {pinned}"
    )
    return 1 if args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
