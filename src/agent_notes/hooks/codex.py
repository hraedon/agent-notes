"""Codex hooks: L1 at session start and per prompt, L2 at Stop (Plan 025 §6).

The hook document lives in :func:`agent_notes.codex_lifecycle.codex_hook_document`
(shared by the plugin bundle and ``install-harness codex``).

* ``SessionStart`` -> ``additionalContext``: the metadata-only orientation
  :mod:`agent_notes.codex_lifecycle` has always rendered, this session's
  key, then claimed peer messages.
* ``UserPromptSubmit`` -> ``additionalContext``: claimed peer messages. A
  prompt without the peer marker resets the loop guard.
* ``Stop`` -> ``{"decision": "block", "reason": ...}`` for one direct
  ``wake=true`` message; Codex runs the reason as the continuation prompt.
  On re-entry (``stop_hook_active``) our cap decides.

``agent-notes codex-hook session-start|stop`` (the commands older installs
trust) delegates here too.
"""

from __future__ import annotations

from typing import Any, Mapping

from agent_notes.hooks import common

HARNESS = "codex"


def fallback(event: str) -> dict[str, Any]:
    # A valid response for every Codex event, and an explicit "let the turn
    # end" for Stop.
    return {"continue": True}


def _input(payload: Mapping[str, Any], env: Mapping[str, str]) -> common.HookInput:
    sid = payload.get("session_id") or env.get("CODEX_THREAD_ID") or env.get("CODEX_SESSION_ID")
    cwd = payload.get("cwd")
    return common.HookInput(
        HARNESS,
        str(sid) if sid else None,
        cwd if isinstance(cwd, str) and cwd and "\x00" not in cwd else None,
        env.get("PASEO_AGENT_ID") or None,
    )


def _context(event_name: str, text: str) -> dict[str, Any]:
    return {
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": common.bounded(text, common.CONTEXT_CAP, "context"),
        },
    }


def session_start(payload: Mapping[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    from agent_notes import codex_lifecycle as lifecycle

    cwd = lifecycle._cwd_for(dict(payload), "SessionStart")
    if cwd is None:
        return fallback("session-start")
    parts: list[str] = []
    try:
        orientation = lifecycle._orientation_for_cwd(cwd)
    except (Exception, SystemExit) as exc:
        common.log("codex session-start orientation failed: %s", type(exc).__name__)
        orientation = None
    if orientation:
        parts.append(orientation)
    try:
        with common.connect() as conn:
            row = common.ensure_session(conn, _input(payload, env), full=True)
            if row is not None:
                parts.append(common.session_key_line(row["session_key"]))
                room = common.CONTEXT_CAP - sum(len(p) + 2 for p in parts)
                budget = min(common.MESSAGE_BUDGET, room)
                if budget > 600:
                    messages = common.deliver(conn, row["session_key"], budget)
                    if messages:
                        parts.append(messages)
    except Exception as exc:  # noqa: BLE001 - keep the orientation if the mailbox fails
        common.log("codex session-start mailbox failed: %s: %s", type(exc).__name__, exc)
    return _context("SessionStart", "\n\n".join(parts)) if parts else fallback("session-start")


def user_prompt_submit(payload: Mapping[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    inp = _input(payload, env)
    if inp.session_id is None:
        return fallback("user-prompt-submit")
    with common.connect() as conn:
        row = common.ensure_session(conn, inp)
        if row is None:
            return fallback("user-prompt-submit")
        if common.is_human_prompt(payload.get("prompt")):
            common.note_human_prompt(conn, row)
        messages = common.deliver(conn, row["session_key"])
    return _context("UserPromptSubmit", messages) if messages else fallback("user-prompt-submit")


def stop(payload: Mapping[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    inp = _input(payload, env)
    if inp.session_id is None or not common.stop_wake_enabled(env):
        return fallback("stop")
    with common.connect() as conn:
        row = common.ensure_session(conn, inp)
        reason = common.claim_wake(conn, row, env)
    if reason is None:
        return fallback("stop")
    return {"decision": "block", "reason": reason}


_HANDLERS = {
    "session-start": session_start,
    "user-prompt-submit": user_prompt_submit,
    "stop": stop,
}


def handle(event: str, payload: Mapping[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    handler = _HANDLERS.get(event)
    if handler is None:
        common.log("codex: unsupported event %r", event)
        return fallback(event)
    return handler(payload, env)
