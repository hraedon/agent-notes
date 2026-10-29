"""UTC log timestamps (agent_notes.core.logs)."""

from __future__ import annotations

import logging

from agent_notes.core.logs import UTCFormatter, basic_config


def _record(created: float) -> logging.LogRecord:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "hello", None, None)
    record.created = created
    record.msecs = (created - int(created)) * 1000
    return record


def test_asctime_is_iso_utc_with_z():
    # 2026-10-29T19:47:58.184Z, independent of the host time zone.
    line = UTCFormatter("%(asctime)s %(message)s").format(_record(1793303278.184))
    assert line == "2026-10-29T19:47:58.184Z hello"


def test_basic_config_applies_the_utc_formatter():
    handler = logging.StreamHandler()
    try:
        basic_config(fmt="%(asctime)s %(message)s", handlers=[handler])
        assert isinstance(handler.formatter, UTCFormatter)
    finally:
        logging.getLogger().removeHandler(handler)
