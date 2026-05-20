"""Setup alias: runs migrate --all against an empty DB (decision 18).

`agent-notes-setup` is a thin alias for `agent-notes-migrate --all`. The
single source of DDL truth is schema/*.sql; this script adds no DDL of its
own.
"""

from __future__ import annotations

import sys


def main() -> None:
    sys.argv = [sys.argv[0], "--all"]
    from agent_notes.scripts.migrate import main as migrate_main

    migrate_main()


if __name__ == "__main__":
    main()
