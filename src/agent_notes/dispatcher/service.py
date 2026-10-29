"""The dispatcher loop: LISTEN, sweep, wake, and run jobs (Plan 025 §6).

One instance per VM, enforced with a session-level Postgres advisory lock on
the dedicated LISTEN connection. Notifications only schedule work; every ~30 s
a sweep re-reads the tables, so a lost notification (or a dispatcher restart)
delays delivery by at most one sweep.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass
from typing import Callable, Mapping

import psycopg
from psycopg.rows import dict_row

from agent_notes.core import messaging

from .jobs import JOB_INTERRUPTED, JobContext, JobHandler, failure_reply, post_reply, run_request
from .wake import DEFERRED, L3Waker

DEFAULT_SWEEP_INTERVAL = 30.0
DEFAULT_WAKE_DELAY = 1.0
DEFAULT_DEFER_WINDOW = 600.0
DEFAULT_MAX_JOBS = 2
_MAX_BACKOFF = 60.0
_LOCK_PREFIX = "agent-notes-dispatch:"

_log = logging.getLogger("agent_notes.dispatcher")


class AlreadyRunningError(RuntimeError):
    """Another dispatcher holds this VM's lock."""


@dataclass
class _Pending:
    session_key: str
    first_seen: float
    due: float


class Dispatcher:
    def __init__(
        self,
        *,
        vm: str,
        waker: L3Waker | None,
        handlers: Mapping[str, JobHandler] | None,
        dsn: str | None = None,
        max_jobs: int = DEFAULT_MAX_JOBS,
        sweep_interval: float = DEFAULT_SWEEP_INTERVAL,
        wake_delay: float = DEFAULT_WAKE_DELAY,
        defer_window: float = DEFAULT_DEFER_WINDOW,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.vm = vm
        self.waker = waker
        self.handlers = dict(handlers) if handlers is not None else None
        self.dsn = dsn
        self.max_jobs = max(1, max_jobs)
        self.sweep_interval = sweep_interval
        self.wake_delay = wake_delay
        self.defer_window = defer_window
        self.clock = clock
        self.claimer = f"{vm}/dispatcher"
        self.ctx = JobContext(vm=vm)
        self._pending: dict[int, _Pending] = {}
        self._settled: dict[int, float] = {}
        self._jobs: set[Future] = set()
        self._jobs_lock = threading.Lock()
        self._requests_dirty = True
        self._executor: ThreadPoolExecutor | None = None
        self._next_sweep = 0.0
        self.listening = threading.Event()

    # ------------------------------------------------------------------
    # Scheduling (no I/O; unit-testable)
    # ------------------------------------------------------------------

    def on_notify(self, payload: str) -> None:
        try:
            data = json.loads(payload)
        except (TypeError, ValueError):
            self._next_sweep = 0.0
            return
        if not isinstance(data, dict):
            return
        if data.get("kind") == "request":
            if data.get("to_vm") == self.vm:
                self._requests_dirty = True
            return
        to_session = data.get("to_session")
        if (
            self.waker is not None
            and data.get("wake")
            and isinstance(to_session, str)
            and to_session.startswith(f"{self.vm}/")
            and isinstance(data.get("id"), int)
        ):
            self.schedule_wake(data["id"], to_session)

    def schedule_wake(self, message_id: int, session_key: str) -> None:
        if message_id in self._pending or message_id in self._settled:
            return
        now = self.clock()
        self._pending[message_id] = _Pending(session_key, now, now + self.wake_delay)

    def next_deadline(self) -> float:
        due = [p.due for p in self._pending.values()]
        return min([self._next_sweep, *due])

    # ------------------------------------------------------------------
    # Work
    # ------------------------------------------------------------------

    def sweep(self) -> None:
        """Pick up what a lost notification would have missed."""
        now = self.clock()
        self._next_sweep = now + self.sweep_interval
        cutoff = now - 2 * self.defer_window
        self._settled = {mid: t for mid, t in self._settled.items() if t > cutoff}
        if self.waker is not None:
            for row in _pending_wakes(self.vm, self.defer_window):
                self.schedule_wake(row["id"], row["to_session"])
        self._requests_dirty = True

    def process_wakes(self, *, force: bool = False) -> list:
        """Run every due wake, grouped by session. Returns the results."""
        if self.waker is None:
            return []
        now = self.clock()
        due: dict[str, list[int]] = {}
        for mid, pending in self._pending.items():
            if force or pending.due <= now:
                due.setdefault(pending.session_key, []).append(mid)
        results = []
        for session_key, ids in due.items():
            try:
                result = self.waker.wake(session_key, ids)
            except psycopg.OperationalError:
                raise  # the run loop reconnects; the rows stay pending
            except Exception:  # noqa: BLE001 - one bad row must not stop the loop
                _log.exception("wake for %s failed", session_key)
                for mid in ids:
                    self._settle(mid)
                continue
            results.append(result)
            if result.outcome == DEFERRED:
                for mid in ids:
                    pending = self._pending[mid]
                    if now - pending.first_seen >= self.defer_window:
                        _log.info(
                            "wake #%s for %s: %s for %.0fs; leaving it for L1",
                            mid,
                            session_key,
                            result.reason,
                            now - pending.first_seen,
                        )
                        self._settle(mid)
                    else:
                        pending.due = now + self.sweep_interval
                continue
            _log.info(
                "wake %s -> %s (agent %s): %s%s",
                ",".join(f"#{m}" for m in ids),
                result.outcome,
                result.agent_id or "-",
                result.reason,
                "".join(f"; #{m} {why}" for m, why in result.refused.items()),
            )
            for mid in ids:
                self._settle(mid)
        return results

    def _settle(self, message_id: int) -> None:
        self._pending.pop(message_id, None)
        self._settled[message_id] = self.clock()

    def poll_requests(self) -> int:
        """Claim requests for free job slots and start them. Returns how many started."""
        if self.handlers is None or not self._requests_dirty:
            return 0
        with self._jobs_lock:
            free = self.max_jobs - len(self._jobs)
        if free <= 0:
            return 0
        self._requests_dirty = False
        rows = messaging.claim_requests(self.vm, limit=free, claimer=self.claimer)
        for row in rows:
            _log.info(
                "claimed request #%s (%s) from %s",
                row["id"],
                (row.get("payload") or {}).get("job"),
                messaging.sender_label(row),
            )
            self._start_job(row)
        if len(rows) == free:
            self._requests_dirty = True  # there may be more queued
        return len(rows)

    def _start_job(self, row: dict) -> None:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=self.max_jobs, thread_name_prefix="agent-notes-job"
            )
        future = self._executor.submit(run_request, row, self.handlers or {}, self.ctx)
        with self._jobs_lock:
            self._jobs.add(future)
        future.add_done_callback(self._job_done)

    def _job_done(self, future: Future) -> None:
        with self._jobs_lock:
            self._jobs.discard(future)
        self._requests_dirty = True

    def wait_for_jobs(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._jobs_lock:
                jobs = [job for job in self._jobs if not job.done()]
            if not jobs:
                return True
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            try:
                jobs[0].exception(timeout=remaining)
            except FuturesTimeout:
                return False

    def recover_interrupted(self) -> int:
        """Answer requests this VM's dispatcher claimed but never replied to.

        A dispatcher that died mid-job (crash, kill, reboot) leaves its claimed
        request with no reply. Only one dispatcher runs per VM, so at startup
        every such row is ours and is answered with ``JOB_INTERRUPTED``.
        """
        if self.handlers is None:
            return 0
        rows = _orphaned_requests(self.claimer)
        for row in rows:
            reply = failure_reply(
                row, JOB_INTERRUPTED, f"the dispatcher on {self.vm} stopped before finishing"
            )
            post_reply(row, reply, self.ctx)
        if rows:
            _log.info("answered %d interrupted request(s)", len(rows))
        return len(rows)

    def tick(self) -> None:
        if self.clock() >= self._next_sweep:
            self.sweep()
        self.process_wakes()
        self.poll_requests()

    def run_once(self) -> None:
        """One sweep, every wake now, and the queued jobs to completion."""
        self.recover_interrupted()
        self.sweep()
        self.process_wakes(force=True)
        while self.poll_requests():
            self.wait_for_jobs()
        self.wait_for_jobs()

    # ------------------------------------------------------------------
    # The LISTEN loop
    # ------------------------------------------------------------------

    def _connect(self) -> psycopg.Connection:
        from agent_notes.core.config import resolve_dsn

        conn = psycopg.connect(
            resolve_dsn(self.dsn), autocommit=True, row_factory=dict_row, connect_timeout=10
        )
        try:
            got = conn.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS ok",
                (_LOCK_PREFIX + self.vm,),
            ).fetchone()
            if not got or not got["ok"]:
                raise AlreadyRunningError(f"another dispatcher is already running for {self.vm}")
            conn.execute(f"LISTEN {messaging.CHANNEL}")
        except BaseException:
            conn.close()
            raise
        return conn

    def run(self, stop: threading.Event) -> None:
        backoff = 1.0
        recovered = False
        try:
            while not stop.is_set():
                try:
                    conn = self._connect()
                except AlreadyRunningError:
                    raise
                except psycopg.Error as exc:
                    _log.warning("database unavailable (%s); retrying in %.0fs", exc, backoff)
                    stop.wait(backoff)
                    backoff = min(_MAX_BACKOFF, backoff * 2)
                    continue
                _log.info("listening on %s for %s", messaging.CHANNEL, self.vm)
                self.listening.set()
                backoff = 1.0
                try:
                    if not recovered:
                        self.recover_interrupted()
                        recovered = True
                    self._next_sweep = 0.0
                    while not stop.is_set():
                        self.tick()
                        wait = max(0.05, min(1.0, self.next_deadline() - self.clock()))
                        for note in conn.notifies(timeout=wait):
                            self.on_notify(note.payload)
                except psycopg.Error as exc:
                    _log.warning("lost the database connection (%s); reconnecting", exc)
                    stop.wait(backoff)
                    backoff = min(_MAX_BACKOFF, backoff * 2)
                finally:
                    conn.close()
        finally:
            if self._executor is not None:
                self._executor.shutdown(wait=True)


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def _pending_wakes(vm: str, within_seconds: float) -> list[dict]:
    from datetime import timedelta

    from agent_notes.core.db import _conn

    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT m.id, m.to_session FROM agent_messages m
            JOIN agent_sessions s ON s.session_key = m.to_session
            WHERE m.to_session IS NOT NULL AND m.wake AND m.claimed_by IS NULL
              AND m.kind NOT IN ('request', 'notice') AND m.expires_at > now()
              AND m.created_at > now() - %s AND s.vm_id = %s
            ORDER BY m.id
            LIMIT 500
            """,
            (timedelta(seconds=within_seconds), vm),
        ).fetchall()
    return [dict(r) for r in rows]


def _orphaned_requests(claimer: str) -> list[dict]:
    from agent_notes.core.db import _conn

    with _conn() as conn:
        rows = conn.execute(
            f"""
            SELECT {messaging._MESSAGE_COLUMNS} FROM agent_messages m
            WHERE m.kind = 'request' AND m.claimed_by = %s AND m.expires_at > now()
              AND NOT EXISTS (
                  SELECT 1 FROM agent_messages r WHERE r.reply_to = m.id AND r.kind = 'reply'
              )
            ORDER BY m.id
            """,  # noqa: S608 - column list is a module constant
            (claimer,),
        ).fetchall()
    return [dict(r) for r in rows]
