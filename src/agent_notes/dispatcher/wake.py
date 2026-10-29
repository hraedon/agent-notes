"""L3 idle wake: hand a direct ``wake=true`` message to the local Paseo daemon.

One call to :meth:`L3Waker.wake` handles every pending wake message for one
session. It re-reads the rows (an ``await`` or a Stop hook may have claimed
them during the grace delay), applies the D6 trust rule, resolves the Paseo
agent, applies the session's ``wake_policy`` and the ``auto_continues`` cap,
and sends only when Paseo reports the agent idle. A running turn is never
interrupted: the message is left for L1/L2 and the caller rechecks later.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from agent_notes.core import messaging

from .paseo import PaseoClient, PaseoError, find_agent_for_session

DEFAULT_MAX_AUTO_CONTINUES = 3
MAX_AUTO_CONTINUES_ENV = "AGENT_NOTES_MAX_AUTO_CONTINUES"

# Outcomes. Only DEFERRED is retried; every other one settles the messages.
SENT = "sent"
DEFERRED = "deferred"
REFUSED = "refused"
NOT_HOSTED = "not_hosted"
SKIPPED = "skipped"
FAILED = "failed"

_log = logging.getLogger("agent_notes.dispatcher")


@dataclass
class WakeResult:
    outcome: str
    reason: str
    session_key: str
    message_ids: tuple[int, ...] = ()
    agent_id: str | None = None
    refused: dict[int, str] = field(default_factory=dict)

    @property
    def settled(self) -> bool:
        return self.outcome != DEFERRED


def policy_allows(session: dict, msg: dict) -> bool:
    """D6: ``direct`` wakes on any direct message; ``any-direct-from-project``
    only when the sender is on the recipient's project."""
    policy = session.get("wake_policy")
    if policy == "direct":
        return True
    if policy == "any-direct-from-project":
        project = session.get("project")
        return project is not None and msg.get("from_project") == project
    return False


class L3Waker:
    def __init__(
        self,
        *,
        vm: str,
        paseo: PaseoClient | None,
        paseo_home: Path,
        max_auto_continues: int = DEFAULT_MAX_AUTO_CONTINUES,
        claimer: str | None = None,
    ) -> None:
        self.vm = vm
        self.paseo = paseo
        self.paseo_home = paseo_home
        self.max_auto_continues = max_auto_continues
        self.claimer = claimer or f"{vm}/dispatcher"

    # -- helpers ----------------------------------------------------------

    def _deliverable(self, session_key: str, ids: Iterable[int]) -> list[dict]:
        rows = []
        for mid in sorted(set(ids)):
            row = messaging.get_message(mid)
            if (
                row is not None
                and row["claimed_by"] is None
                and row["wake"]
                and row["kind"] not in ("request", "notice")
                and row["to_session"] == session_key
                and not _expired(row)
            ):
                rows.append(row)
        return rows

    def _sender_untrusted(self, msg: dict, cache: dict[str, Any]) -> bool:
        sender = msg.get("from_session")
        if not sender:
            return False
        if sender not in cache:
            cache[sender] = messaging.get_session(sender)
        row = cache[sender]
        return row is not None and row.get("trust") == "untrusted-input"

    def resolve_agent(self, session: dict) -> tuple[str | None, dict]:
        """(a) the registered ``paseo_agent_id``; (b) Paseo's local records,
        stored back on the session. Returns the agent ID and the session row."""
        if session.get("paseo_agent_id"):
            return session["paseo_agent_id"], session
        agent_id = find_agent_for_session(session["session_id"], home=self.paseo_home)
        if agent_id is None:
            return None, session
        _log.info("mapped %s to Paseo agent %s from local state", session["session_key"], agent_id)
        updated = messaging.set_paseo_agent_id(session["session_key"], agent_id)
        return agent_id, updated or session

    # -- the wake ---------------------------------------------------------

    def wake(self, session_key: str, message_ids: Iterable[int]) -> WakeResult:
        rows = self._deliverable(session_key, message_ids)
        if not rows:
            return WakeResult(SKIPPED, "already claimed or expired", session_key)
        ids = tuple(r["id"] for r in rows)

        session = messaging.get_session(session_key)
        if session is None or session["vm_id"] != self.vm:
            return WakeResult(NOT_HOSTED, "session is not registered on this VM", session_key, ids)

        refused: dict[int, str] = {}
        trusted_cache: dict[str, Any] = {}
        candidates = []
        for row in rows:
            if self._sender_untrusted(row, trusted_cache):
                refused[row["id"]] = "sender is untrusted-input (D6)"
            else:
                candidates.append(row)
        if not candidates:
            return WakeResult(
                REFUSED, "every sender is untrusted-input", session_key, ids, None, refused
            )

        agent_id, session = self.resolve_agent(session)
        if agent_id is None:
            return WakeResult(
                NOT_HOSTED, "no Paseo agent hosts this session", session_key, ids, None, refused
            )

        eligible = []
        for row in candidates:
            if policy_allows(session, row):
                eligible.append(row)
            else:
                refused[row["id"]] = f"wake_policy {session['wake_policy']} does not allow it"
        if not eligible:
            return WakeResult(
                REFUSED, "wake_policy does not allow it", session_key, ids, agent_id, refused
            )
        if session["auto_continues"] >= self.max_auto_continues:
            return WakeResult(
                REFUSED,
                f"auto_continues {session['auto_continues']} is at the cap "
                f"{self.max_auto_continues}",
                session_key,
                ids,
                agent_id,
                refused,
            )
        if self.paseo is None:
            return WakeResult(FAILED, "no paseo binary", session_key, ids, agent_id, refused)

        try:
            state = self.paseo.inspect(agent_id)
        except PaseoError as exc:
            return WakeResult(DEFERRED, str(exc), session_key, ids, agent_id, refused)
        if not state.idle:
            outcome = DEFERRED if state.busy else NOT_HOSTED
            reason = f"Paseo agent is {'archived' if state.archived else state.status}"
            return WakeResult(outcome, reason, session_key, ids, agent_id, refused)

        claimed = [
            row
            for row in (messaging.claim_message(r["id"], self.claimer) for r in eligible)
            if row is not None
        ]
        if not claimed:
            return WakeResult(SKIPPED, "claimed by someone else", session_key, ids, agent_id)
        text = "\n\n".join(messaging.format_peer_message(row) for row in claimed)
        try:
            self.paseo.send(agent_id, text)
        except PaseoError as exc:
            for row in claimed:
                messaging.release_claim(row["id"], self.claimer)
            return WakeResult(FAILED, f"{exc}; left for L1", session_key, ids, agent_id, refused)
        count = messaging.bump_auto_continue(session_key)
        sent_ids = tuple(row["id"] for row in claimed)
        return WakeResult(
            SENT,
            f"sent {len(sent_ids)} message(s); auto_continues={count}",
            session_key,
            sent_ids,
            agent_id,
            refused,
        )


def _expired(row: dict) -> bool:
    from datetime import datetime, timezone

    expires = row.get("expires_at")
    return expires is not None and expires <= datetime.now(timezone.utc)
