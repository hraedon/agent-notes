"""Shared HTTP transport for agent-wake POST (bridge + trigger-loop).

Both ``bridge.py`` and ``trigger_loop.py`` duplicate ``_sign`` and
``_post_with_retry``.  This module is the single copy.

Contract:
- ``sign(body, secret)`` → HMAC-SHA256 hex digest with ``sha256=`` prefix.
- ``post_with_retry(target, secret, source, event, ...)`` → bool (True on 2xx).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Iterable

_log = logging.getLogger("agent_notes.http_transport")

RETRY_DELAYS = (0.1, 1.0, 10.0)
DEFAULT_SOURCE = "agent-notes"


def sign(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def post_with_retry(
    target: str,
    secret: str,
    source: str,
    event: dict,
    *,
    delays: Iterable[float] = RETRY_DELAYS,
    timeout: float = 5.0,
) -> bool:
    body = json.dumps(event).encode("utf-8")
    sig = sign(body, secret)

    attempts = [0.0] + list(delays)
    last_error: str | None = None
    for delay in attempts:
        if delay:
            time.sleep(delay)
        req = urllib.request.Request(
            target,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-AgentWake-Source": source,
                "X-AgentWake-Signature": sig,
                "X-AgentWake-Event-Id": event["event_id"],
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if 200 <= resp.status < 300:
                    return True
                last_error = f"HTTP {resp.status}"
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = f"transport: {exc}"
    _log.warning("dropping event %s after retries: %s", event.get("event_id"), last_error)
    return False
