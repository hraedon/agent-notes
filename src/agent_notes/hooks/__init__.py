"""Harness hook entry point: ``agent-notes-hook <harness> <event>`` (Plan 025 Phase 4).

Kept import-light on purpose. The per-prompt events run on every prompt, so
this package must not import ``agent_notes.cli``, regista, the embedding model
or the connection pool on that path (§6 latency budget).
"""

from __future__ import annotations


def main(argv: list[str] | None = None) -> int:
    from agent_notes.hooks.common import run

    return run(argv)
