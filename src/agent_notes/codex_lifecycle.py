"""Narrow Codex lifecycle adapter for agent-notes (Plan 019 Phase 2, Plan 025 §6).

Codex command hooks receive JSON on stdin and require event-specific JSON on
stdout.  The orientation path consumes only ``cwd`` and ``hook_event_name``.
The mailbox hooks (:mod:`agent_notes.hooks.codex`) also read ``session_id``
and, for the loop guard, whether the prompt starts with the peer-message
marker.  Transcript paths, assistant messages, model names, and credentials
are neither read nor persisted.

The canonical hook definitions below are used by the direct installer and are
also regression-checked against the component plugin's ``hooks/hooks.json``.
Keeping one command surface for both paths prevents the plugin and fallback
installer from developing different lifecycle behavior.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, TextIO

MAX_HOOK_INPUT_CHARS = 1_000_000
MAX_ORIENTATION_CHARS = 7_000
_MAX_FIELD_CHARS = 400

SESSION_START_COMMAND = "agent-notes-hook codex session-start"
USER_PROMPT_SUBMIT_COMMAND = "agent-notes-hook codex user-prompt-submit"
STOP_COMMAND = "agent-notes-hook codex stop"
# Commands earlier versions installed; the installer replaces owned groups
# that still run them.
LEGACY_COMMANDS: dict[str, tuple[str, ...]] = {
    "SessionStart": ("agent-notes codex-hook session-start",),
    "Stop": ("agent-notes codex-hook stop",),
}

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|authorization|password|passwd|secret)"
        r"\b\s*[:=]\s*([^\s,;]+)"
    ),
    re.compile(r"(?i)\b(sk-[a-z0-9_-]{12,}|AKIA[A-Z0-9]{16})\b"),
    re.compile(r"(?i)([a-z][a-z0-9+.-]*://[^\s:/]+:)[^\s@/]+(@)"),
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
)


def codex_hook_document() -> dict[str, object]:
    """Return the canonical Codex hook document for plugin and direct wiring."""
    return {
        "description": (
            "Agent-notes project orientation and agent messages (next prompt and "
            "turn extension). Review and trust these commands with /hooks before use."
        ),
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup|resume|clear|compact",
                    "hooks": [
                        {
                            "type": "command",
                            "command": SESSION_START_COMMAND,
                            "timeout": 20,
                            "statusMessage": "Loading agent-notes orientation",
                        }
                    ],
                }
            ],
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": USER_PROMPT_SUBMIT_COMMAND,
                            "timeout": 10,
                        }
                    ]
                }
            ],
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": STOP_COMMAND,
                            "timeout": 10,
                            "statusMessage": "Checking agent-notes for wake messages",
                        }
                    ]
                }
            ],
        },
    }


def canonical_hook_groups() -> dict[str, dict[str, object]]:
    """Return one owned matcher group for each event."""
    hooks = codex_hook_document()["hooks"]
    assert isinstance(hooks, dict)
    return {event: groups[0] for event, groups in hooks.items()}


def _redact(value: object) -> str:
    """Bound one metadata field and redact common inline secret forms."""
    text = str(value).replace("\x00", "").replace("\r", " ").replace("\n", " ")
    for index, pattern in enumerate(_SECRET_PATTERNS):
        if index == 0:
            text = pattern.sub(r"\1=[REDACTED]", text)
        elif index == 2:
            text = pattern.sub(r"\1[REDACTED]\2", text)
        else:
            text = pattern.sub("[REDACTED]", text)
    if len(text) > _MAX_FIELD_CHARS:
        return text[: _MAX_FIELD_CHARS - 1] + "…"
    return text


def _format_orientation(payload: dict[str, Any]) -> str:
    """Render a bounded, metadata-only developer context block.

    The normal ``orient`` JSON contains no note bodies or secret configuration.
    This renderer narrows it further: memory names, work-item titles, git
    subjects, and learned recall are omitted. Only generated identifiers,
    controlled state fields, and counts enter automatic context.
    """
    project = _redact(payload.get("project", "unknown"))
    workspace = _redact(payload.get("workspace", "unknown"))
    lines = [
        "Agent-notes project orientation (bounded stored metadata; may be stale):",
        f"Project: {project} (workspace {workspace})",
    ]
    if payload.get("generated_at"):
        lines.append(f"Snapshot: {_redact(payload['generated_at'])}; ages do not verify accuracy")
    more = payload.get("has_more", {})
    if not isinstance(more, dict):
        more = {}

    work_items = payload.get("open_work_items")
    if isinstance(work_items, list):
        suffix = "; more available" if more.get("open_work_items") or len(work_items) > 5 else ""
        lines.append(f"Open work items: {min(len(work_items), 5)} shown{suffix}")
        for item in work_items[:5]:
            if not isinstance(item, dict):
                continue
            identifier = _redact(item.get("identifier", "?"))
            status = _redact(item.get("status", "?"))
            severity = _redact(item.get("severity", "?"))
            freshness = _redact(item.get("freshness", "unknown"))
            age = item.get("age_days")
            age_label = f"{age}d; " if isinstance(age, int) and age >= 0 else ""
            lines.append(
                f"- [{severity}] {identifier} ({status}; recorded update: {age_label}{freshness})"
            )

    notices = payload.get("notices")
    if isinstance(notices, list) and notices:
        # Subjects are peer content; the count and ids are enough to prompt a read.
        ids = [
            f"#{n['id']}" for n in notices if isinstance(n, dict) and isinstance(n.get("id"), int)
        ]
        suffix = "; more available" if more.get("notices") else ""
        lines.append(f"Live notices: {', '.join(ids)}{suffix} (read with `agent-notes msg inbox`)")

    changes = payload.get("recent_changes")
    if isinstance(changes, list):
        suffix = "; more available" if more.get("recent_changes") else ""
        lines.append(f"Recent changes: {len(changes)} sampled{suffix}")

    memories = payload.get("memories")
    if isinstance(memories, list):
        # Names can themselves contain sensitive user text.  The hook needs an
        # orientation signal, not the names; agents can opt into the skills to
        # inspect memories deliberately.
        suffix = "; more available" if more.get("memories") else ""
        lines.append(f"Stored project memories: {len(memories)} sampled{suffix}")

    reflection = payload.get("latest_reflection")
    if isinstance(reflection, dict):
        updated = _redact(reflection.get("updated_at") or "unknown")
        freshness = _redact(reflection.get("freshness", "unknown"))
        lines.append(f"Latest handoff: recorded update {updated} ({freshness})")

    resolved = payload.get("resolved_in_git")
    if isinstance(resolved, list) and resolved:
        identifiers = [
            _redact(item.get("identifier", "?")) for item in resolved[:10] if isinstance(item, dict)
        ]
        lines.append("Resolved in git but still open: " + ", ".join(identifiers))

    context = "\n".join(lines)
    if len(context) > MAX_ORIENTATION_CHARS:
        context = context[: MAX_ORIENTATION_CHARS - 24] + "\n[orientation truncated]"
    return context


def _orientation_for_cwd(cwd: Path) -> str | None:
    """Run the existing orientation query and return safe developer context."""
    from agent_notes.cli.orient import cmd_orient

    args = argparse.Namespace(
        workspace=None,
        project=None,
        path=str(cwd),
        days=7,
        limit=5,
        reconcile=True,
        recall=False,
        json=True,
    )
    stdout = io.StringIO()
    # Resolution and store failures must not corrupt the hook JSON channel.
    # Discard diagnostics here; the next interactive doctor run owns details.
    with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
        code = cmd_orient(args)
    if code != 0:
        return None
    parsed = json.loads(stdout.getvalue())
    if not isinstance(parsed, dict) or "error" in parsed:
        return None
    if parsed.get("project") is None:
        # Unregistered cwd: orient reports only the VM/global tiers; this hook
        # injects project orientation only.
        return None
    return _format_orientation(parsed)


def _read_payload(stdin: TextIO) -> dict[str, Any] | None:
    raw = stdin.read(MAX_HOOK_INPUT_CHARS + 1)
    if len(raw) > MAX_HOOK_INPUT_CHARS:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _cwd_for(payload: dict[str, Any], event: str) -> Path | None:
    if payload.get("hook_event_name") != event:
        return None
    raw = payload.get("cwd")
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        return None
    path = Path(raw)
    if not path.is_absolute() or not path.is_dir():
        return None
    return path


def _success_output() -> dict[str, bool]:
    # For Stop this is an explicit non-continuation response: it neither blocks
    # nor asks Codex to generate another turn.  It is also valid shared output
    # for SessionStart when orientation is unavailable.
    return {"continue": True}


def _run_event(event: str, payload: dict[str, Any]) -> dict[str, object]:
    import os

    from agent_notes.hooks import codex

    try:
        return codex.handle(event, payload, os.environ)
    except BaseException as exc:  # noqa: BLE001 - hooks fail open, always
        from agent_notes.hooks.common import log

        log("codex %s failed: %s: %s", event, type(exc).__name__, exc)
        return _success_output()


def run_session_start(payload: dict[str, Any]) -> dict[str, object]:
    return _run_event("session-start", payload)


def run_user_prompt_submit(payload: dict[str, Any]) -> dict[str, object]:
    return _run_event("user-prompt-submit", payload)


def run_stop(payload: dict[str, Any]) -> dict[str, object]:
    """L2 turn extension (Plan 025 §6); the regista reconcile is retired (D7)."""
    return _run_event("stop", payload)


_RUNNERS = {
    "session-start": run_session_start,
    "user-prompt-submit": run_user_prompt_submit,
    "stop": run_stop,
}


def cmd_codex_hook(args: argparse.Namespace) -> int:
    """``agent-notes codex-hook <event>``: the command older installs still trust."""
    from agent_notes.hooks.common import write_json

    payload = _read_payload(sys.stdin)
    if payload is None:
        result: dict[str, object] = _success_output()
    else:
        result = _RUNNERS[args.codex_hook_event](payload)
    write_json(result, sys.stdout)
    return 0


def register_codex_hook_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("codex-hook", help=argparse.SUPPRESS)
    events = parser.add_subparsers(dest="codex_hook_event", required=True)
    for event in _RUNNERS:
        event_parser = events.add_parser(event, help=argparse.SUPPRESS)
        event_parser.set_defaults(func=cmd_codex_hook)
