"""LISTEN/NOTIFY helpers (decision 7).

`listen` is a context-manager generator that yields parsed payloads from the
`agent_notes_changes` channel. Used by future dashboards or `/start` reactors.

Kept intentionally simple: psycopg's synchronous LISTEN + polling loop.
Production hardening (reconnect, backpressure) is deferred until a consumer
with real uptime requirements is built.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from typing import Generator


@contextmanager
def listen(
    channel: str = "agent_notes_changes",
    dsn: str | None = None,
    poll_timeout: float = 5.0,
) -> Generator[Generator[dict, None, None], None, None]:
    """Context manager that yields a generator of parsed NOTIFY payloads.

    Usage::

        with notify.listen() as events:
            for payload in events:
                print(payload)  # {'kind': ..., 'event': ..., ...}

    The generator blocks for at most `poll_timeout` seconds per iteration.
    Break out of the inner loop to close cleanly.
    """
    import psycopg

    effective_dsn = dsn or os.environ.get("AGENT_NOTES_DSN", "")
    if not effective_dsn:
        raise RuntimeError("AGENT_NOTES_DSN not set")

    conn = psycopg.connect(effective_dsn, autocommit=True)
    try:
        from psycopg import sql

        conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(channel)))

        def _gen() -> Generator[dict, None, None]:
            for notify in conn.notifies(timeout=poll_timeout):
                try:
                    payload = json.loads(notify.payload)
                except (json.JSONDecodeError, TypeError):
                    payload = {"raw": notify.payload}
                yield payload

        yield _gen()
    finally:
        conn.close()
