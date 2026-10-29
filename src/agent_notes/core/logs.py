"""UTC log timestamps.

Every log line agent-notes writes is stamped in ISO-8601 UTC with a ``Z``
suffix (``2026-10-29T19:47:58.184Z``), whatever the host's time zone. The
servers run UTC and the Windows workstation doesn't, and unlabelled local
times made logs from different hosts hard to line up. The database already
stores ``timestamptz``; human-facing views may still show local or relative
times.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable


class UTCFormatter(logging.Formatter):
    """A formatter whose ``%(asctime)s`` is ISO-8601 UTC with milliseconds and ``Z``."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: N802 - stdlib name
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
        return f"{stamp}.{int(record.msecs):03d}Z"


def basic_config(
    *,
    fmt: str,
    level: int = logging.INFO,
    handlers: Iterable[logging.Handler] | None = None,
) -> None:
    """``logging.basicConfig`` with :class:`UTCFormatter` on every handler."""
    chosen = list(handlers) if handlers else [logging.StreamHandler()]
    for handler in chosen:
        handler.setFormatter(UTCFormatter(fmt))
    logging.basicConfig(level=level, handlers=chosen, force=True)
