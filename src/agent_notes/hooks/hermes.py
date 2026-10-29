"""Hermes Agent hooks: L1 (Plan 025 §6, Phase 7).

Contract checked against hermes-agent 0.15.2 and ``main``
(``agent/shell_hooks.py``, ``agent/conversation_loop.py``). Hermes runs the
``hooks:`` block of ``~/.hermes/config.yaml`` as subprocesses: JSON on stdin,
JSON on stdout, any failure is ignored.

* ``pre_llm_call`` -> ``{"context": ...}``, appended to the turn's user
  message (never the system prompt). It fires once per user turn. stdin is
  ``{"hook_event_name", "session_id", "cwd", "profile"?, "extra": {"user_message",
  "is_first_turn", "platform", "model", "sender_id", ...}}``.

The first turn of a session carries the ``orient`` summary and this session's
key, then claimed peer messages. Later turns carry only claimed peer messages
(D6: framed, and only here). A session the hooks have not seen before (a
resumed one, or one whose id changed when Hermes compressed its history)
counts as a first turn.

There is no L2: ``pre_verify`` exists only on ``main`` and fires only on
turns that edited code, so it can't extend an arbitrary turn.
"""

from __future__ import annotations

from typing import Any, Mapping

from agent_notes.hooks import common

HARNESS = "hermes"
EVENT = "pre_llm_call"
HOOK_COMMAND = "agent-notes-hook hermes pre-llm-call"
HOOK_TIMEOUT = 15


def hermes_hook_entry() -> dict[str, object]:
    """The owned entry under ``hooks.pre_llm_call`` in ``~/.hermes/config.yaml``."""
    return {"command": HOOK_COMMAND, "timeout": HOOK_TIMEOUT}


def fallback(event: str) -> dict[str, Any]:
    return {}


def _extra(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    extra = payload.get("extra")
    return extra if isinstance(extra, Mapping) else {}


def _input(payload: Mapping[str, Any], env: Mapping[str, str]) -> common.HookInput:
    sid = payload.get("session_id") or env.get("HERMES_SESSION_ID")
    cwd = payload.get("cwd")
    return common.HookInput(
        HARNESS,
        str(sid) if sid else None,
        cwd if isinstance(cwd, str) and cwd and "\x00" not in cwd else None,
        env.get("PASEO_AGENT_ID") or None,
        common.model_name(_extra(payload).get("model")),
    )


def _prompt_text(message: object) -> object:
    """``user_message`` is a string, or a list of content parts on a multimodal turn."""
    if isinstance(message, list):
        return "".join(
            part["text"]
            for part in message
            if isinstance(part, Mapping) and isinstance(part.get("text"), str)
        )
    return message


def pre_llm_call(payload: Mapping[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    from agent_notes.core import messaging

    inp = _input(payload, env)
    if inp.session_id is None:
        return {}
    extra = _extra(payload)
    parts: list[str] = []
    with common.connect() as conn:
        key = common.session_key_for(inp)
        assert key is not None
        start = extra.get("is_first_turn") is True or messaging.get_session(key, conn=conn) is None
        row = common.ensure_session(conn, inp, full=start)
        if row is None:
            return {}
        if common.is_human_prompt(_prompt_text(extra.get("user_message"))):
            common.note_human_prompt(conn, row)
        if start:
            summary = common.orient_text(inp.cwd)
            if summary:
                parts.append(summary)
            parts.append(common.session_key_line(key))
        room = common.CONTEXT_CAP - sum(len(p) + 2 for p in parts)
        budget = min(common.MESSAGE_BUDGET, room)
        if budget > 600:
            messages = common.deliver(conn, key, budget)
            if messages:
                parts.append(messages)
    if not parts:
        return {}
    return {"context": common.bounded("\n\n".join(parts), common.CONTEXT_CAP, "context")}


_HANDLERS = {"pre-llm-call": pre_llm_call}


def handle(event: str, payload: Mapping[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    handler = _HANDLERS.get(event)
    if handler is None:
        common.log("hermes: unsupported event %r", event)
        return fallback(event)
    return handler(payload, env)
