"""``agent-notes-dispatch``: the per-VM mailbox dispatcher (Plan 025 §6, Phase 5).

It does two things for the VM it runs on:

- **L3 idle wake.** A direct ``wake=true`` message for a Paseo-hosted session on
  this VM is handed to the local Paseo daemon with ``paseo send --no-wait``,
  but only when the agent is idle and the session's wake policy, the D6 trust
  rule and the ``auto_continues`` cap all allow it.
- **Jobs.** ``request`` messages addressed to this VM run as bounded jobs (v1:
  ``review``, D8), at most two at a time, and always get a ``wake=true`` reply.

It is a separate process from ``agent-notes-bridge``, which only forwards
change notifications to agent-wake and is retired with it. See
``docs/dispatcher.md`` for configuration and operations.
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import signal
import sys
import threading

LOG_ENV = "AGENT_NOTES_DISPATCH_LOG"
MAX_JOBS_ENV = "AGENT_NOTES_DISPATCH_MAX_JOBS"
SWEEP_ENV = "AGENT_NOTES_DISPATCH_SWEEP_SECONDS"

_log = logging.getLogger("agent_notes.dispatcher")


def _setting(name: str) -> str | None:
    from agent_notes.core.config import _env_or_suite
    from agent_notes.core.suite_env import load_suite_env

    return _env_or_suite(name, load_suite_env())


def _int_setting(name: str, default: int, *, minimum: int) -> int:
    raw = _setting(name)
    try:
        return max(minimum, int(raw)) if raw else default
    except ValueError:
        _log.warning("%s=%r is not an integer; using %d", name, raw, default)
        return default


def _configure_logging(level: str, log_file: str | None) -> None:
    # With a log file it is the only sink (the Windows task has no console).
    handlers: list[logging.Handler] = [] if log_file else [logging.StreamHandler(sys.stderr)]
    if log_file:
        os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
            )
        )
    from agent_notes.core import logs

    logs.basic_config(
        level=getattr(logging, level.upper(), logging.INFO),
        fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


def build_dispatcher(args: argparse.Namespace):
    from agent_notes.core import messaging
    from agent_notes.core.config import resolve_vm_id

    from .paseo import PaseoClient, find_paseo, paseo_home
    from .review_job import run_review_job
    from .service import DEFAULT_SWEEP_INTERVAL, Dispatcher
    from .wake import DEFAULT_MAX_AUTO_CONTINUES, MAX_AUTO_CONTINUES_ENV, L3Waker

    vm = resolve_vm_id(args.vm)
    waker = None
    if not args.no_wake:
        binary = find_paseo()
        if binary is None:
            _log.warning(
                "no paseo binary (AGENT_NOTES_PASEO_BIN or PATH): L3 wake is off; "
                "wake messages wait for L1/L2"
            )
        else:
            waker = L3Waker(
                vm=vm,
                paseo=PaseoClient(binary),
                paseo_home=paseo_home(),
                max_auto_continues=_int_setting(
                    MAX_AUTO_CONTINUES_ENV, DEFAULT_MAX_AUTO_CONTINUES, minimum=0
                ),
                auto_continue_window=_int_setting(
                    messaging.AUTO_CONTINUE_WINDOW_ENV,
                    messaging.DEFAULT_AUTO_CONTINUE_WINDOW,
                    minimum=1,
                ),
            )
    handlers = None if args.no_jobs else {"review": run_review_job}
    sweep = _int_setting(SWEEP_ENV, int(DEFAULT_SWEEP_INTERVAL), minimum=5)
    return Dispatcher(
        vm=vm,
        waker=waker,
        handlers=handlers,
        max_jobs=args.max_jobs or _int_setting(MAX_JOBS_ENV, 2, minimum=1),
        sweep_interval=float(sweep),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent-notes-dispatch",
        description="Per-VM agent-notes dispatcher: L3 Paseo wake and review jobs.",
    )
    parser.add_argument("--vm", default=None, help="VM ID (default: AGENT_NOTES_VM or hostname)")
    parser.add_argument("--once", action="store_true", help="One sweep, run due work, then exit")
    parser.add_argument("--no-wake", action="store_true", help="Don't do L3 Paseo wakes")
    parser.add_argument("--no-jobs", action="store_true", help="Don't claim request jobs")
    parser.add_argument("--max-jobs", type=int, default=None, help="Concurrent jobs (default 2)")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--log-file", default=None, help=f"Also log here (or {LOG_ENV})")
    args = parser.parse_args(argv)

    _configure_logging(args.log_level, args.log_file or _setting(LOG_ENV))
    from agent_notes.core.config import VmIdError

    from .service import AlreadyRunningError

    try:
        dispatcher = build_dispatcher(args)
    except VmIdError as exc:
        _log.error("%s", exc)
        return 3

    stop = threading.Event()

    def _stop(signum, _frame) -> None:
        _log.info("signal %s: stopping after running jobs finish", signum)
        stop.set()

    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _stop)

    _log.info(
        "agent-notes-dispatch starting for %s (wake %s, jobs %s)",
        dispatcher.vm,
        "on" if dispatcher.waker is not None else "off",
        "on" if dispatcher.handlers is not None else "off",
    )
    try:
        if args.once:
            conn = dispatcher._connect()  # holds the per-VM lock for the run
            try:
                dispatcher.run_once()
            finally:
                conn.close()
        else:
            dispatcher.run(stop)
    except AlreadyRunningError as exc:
        _log.error("%s", exc)
        return 4
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
