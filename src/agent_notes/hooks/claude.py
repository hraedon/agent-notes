"""Claude Code hooks: L1 at session start and per prompt, L2 at Stop (Plan 025 §6).

Installed user-level by ``agent-notes install-harness claude``:

* ``SessionStart`` -> ``additionalContext``: the ``orient`` summary, this
  session's key, then claimed peer messages. The summary is skipped when a
  settings file already runs ``agent-notes orient`` itself (the per-repo hook
  older ``agent-notes init`` runs wrote), so it never appears twice.
* ``UserPromptSubmit`` -> ``additionalContext``: claimed peer messages. A
  prompt without the peer marker resets the loop guard.
* ``Stop`` -> ``{"decision": "block", "reason": ...}`` for one direct
  ``wake=true`` message, while the loop guard allows it. ``stop_hook_active``
  is ignored: on re-entry our own cap decides.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from agent_notes.hooks import common

HARNESS = "claude"
_COMMAND = "agent-notes-hook claude"
SESSION_START_COMMAND = f"{_COMMAND} session-start"
USER_PROMPT_SUBMIT_COMMAND = f"{_COMMAND} user-prompt-submit"
STOP_COMMAND = f"{_COMMAND} stop"
ORIENT_COMMAND_PREFIX = "agent-notes orient"


def claude_hook_groups() -> dict[str, dict[str, object]]:
    """The one owned hook group per event in ``~/.claude/settings.json``."""
    return {
        "SessionStart": {
            "hooks": [{"type": "command", "command": SESSION_START_COMMAND, "timeout": 30}]
        },
        "UserPromptSubmit": {
            "hooks": [{"type": "command", "command": USER_PROMPT_SUBMIT_COMMAND, "timeout": 10}]
        },
        "Stop": {"hooks": [{"type": "command", "command": STOP_COMMAND, "timeout": 10}]},
    }


def fallback(event: str) -> dict[str, Any]:
    return {}


def _input(payload: Mapping[str, Any], env: Mapping[str, str]) -> common.HookInput:
    sid = payload.get("session_id") or env.get("CLAUDE_CODE_SESSION_ID")
    cwd = payload.get("cwd") or env.get("CLAUDE_PROJECT_DIR")
    return common.HookInput(
        HARNESS,
        str(sid) if sid else None,
        str(cwd) if isinstance(cwd, str) and cwd else None,
        env.get("PASEO_AGENT_ID") or None,
        common.model_name(payload.get("model")),
    )


def settings_run_orient(path: Path) -> bool:
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    groups = hooks.get("SessionStart") if isinstance(hooks, dict) else None
    if not isinstance(groups, list):
        return False
    for group in groups:
        handlers = group.get("hooks") if isinstance(group, dict) else None
        for handler in handlers if isinstance(handlers, list) else []:
            command = handler.get("command") if isinstance(handler, dict) else None
            if isinstance(command, str) and command.strip().startswith(ORIENT_COMMAND_PREFIX):
                return True
    return False


def orient_already_wired(cwd: str | None, env: Mapping[str, str], home: Path | None = None) -> bool:
    """True when a user or project settings file already runs ``agent-notes orient``."""
    home = home or Path.home()
    candidates = [home / ".claude" / "settings.json"]
    for root in {env.get("CLAUDE_PROJECT_DIR"), cwd}:
        if root:
            base = Path(root) / ".claude"
            candidates += [base / "settings.json", base / "settings.local.json"]
    return any(settings_run_orient(path) for path in candidates)


def _context(event_name: str, text: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": common.bounded(text, common.CONTEXT_CAP, "context"),
        }
    }


def session_start(payload: Mapping[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    inp = _input(payload, env)
    with common.connect() as conn:
        row = common.ensure_session(conn, inp, full=True)
        parts: list[str] = []
        if not orient_already_wired(inp.cwd, env):
            summary = common.orient_text(inp.cwd)
            if summary:
                parts.append(summary)
        if row is not None:
            parts.append(common.session_key_line(row["session_key"]))
            room = common.CONTEXT_CAP - sum(len(p) + 2 for p in parts)
            budget = min(common.MESSAGE_BUDGET, room)
            if budget > 600:
                messages = common.deliver(conn, row["session_key"], budget)
                if messages:
                    parts.append(messages)
    return _context("SessionStart", "\n\n".join(parts)) if parts else {}


def user_prompt_submit(payload: Mapping[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    inp = _input(payload, env)
    if inp.session_id is None:
        return {}
    with common.connect() as conn:
        row = common.ensure_session(conn, inp)
        if row is None:
            return {}
        if common.is_human_prompt(payload.get("prompt")):
            common.note_human_prompt(conn, row)
        messages = common.deliver(conn, row["session_key"])
    return _context("UserPromptSubmit", messages) if messages else {}


def stop(payload: Mapping[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    inp = _input(payload, env)
    if inp.session_id is None or not common.stop_wake_enabled(env):
        return {}
    with common.connect() as conn:
        row = common.ensure_session(conn, inp)
        reason = common.claim_wake(conn, row, env)
    if reason is None:
        return {}
    return {"decision": "block", "reason": reason}


_HANDLERS = {
    "session-start": session_start,
    "user-prompt-submit": user_prompt_submit,
    "stop": stop,
}


def handle(event: str, payload: Mapping[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    handler = _HANDLERS.get(event)
    if handler is None:
        common.log("claude: unsupported event %r", event)
        return fallback(event)
    return handler(payload, env)
