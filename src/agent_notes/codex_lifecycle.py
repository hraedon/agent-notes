"""Narrow Codex lifecycle adapter for agent-notes (Plan 019 Phase 2).

Codex command hooks receive JSON on stdin and require event-specific JSON on
stdout.  This module deliberately consumes only ``cwd``, ``hook_event_name``,
and ``stop_hook_active``.  Transcript paths, prompts, assistant messages,
model names, and credentials are neither read nor persisted.

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

SESSION_START_COMMAND = "agent-notes codex-hook session-start"
STOP_COMMAND = "agent-notes codex-hook stop"

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
            "Agent-notes project orientation and best-effort outbox reconciliation. "
            "Review and trust these commands with /hooks before use."
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
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": STOP_COMMAND,
                            "timeout": 30,
                            "statusMessage": "Reconciling the agent-notes outbox",
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

    work_items = payload.get("open_work_items")
    if isinstance(work_items, list):
        lines.append(f"Open work items: {len(work_items)}")
        for item in work_items[:10]:
            if not isinstance(item, dict):
                continue
            identifier = _redact(item.get("identifier", "?"))
            status = _redact(item.get("status", "?"))
            severity = _redact(item.get("severity", "?"))
            lines.append(f"- [{severity}] {identifier} ({status})")

    changes = payload.get("recent_changes")
    if isinstance(changes, list):
        lines.append(f"Recent changes: {len(changes)}")
        for change in changes[:10]:
            if not isinstance(change, dict):
                continue
            kind = _redact(change.get("kind", "?"))
            identifier = _redact(change.get("identifier", "?"))
            event = _redact(change.get("event", "?"))
            lines.append(f"- [{kind}] {identifier}: {event}")

    memories = payload.get("memories")
    if isinstance(memories, list):
        # Names can themselves contain sensitive user text.  The hook needs an
        # orientation signal, not the names; agents can opt into the skills to
        # inspect memories deliberately.
        lines.append(f"Stored project memories: {len(memories)}")

    resolved = payload.get("resolved_in_git")
    if isinstance(resolved, list) and resolved:
        identifiers = [
            _redact(item.get("identifier", "?"))
            for item in resolved[:10]
            if isinstance(item, dict)
        ]
        lines.append("Resolved in git but still open: " + ", ".join(identifiers))

    sync = payload.get("regista_sync")
    if isinstance(sync, dict) and sync.get("enabled"):
        pending = sum(
            int(sync.get(key, 0))
            for key in (
                "outbox_pending",
                "outbox_conflicts",
                "outbox_rejected",
                "pending_sync_rows",
            )
            if isinstance(sync.get(key, 0), int)
        )
        lines.append(f"Pending synchronization findings: {pending}")

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
        limit=10,
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
    return _format_orientation(parsed)


def _reconcile_cwd(cwd: Path) -> None:
    """Best-effort outbox reconciliation for the project containing *cwd*."""
    from agent_notes.cli.common import _resolve
    from agent_notes.core import outbox
    from agent_notes.core.face_factory import (
        get_face,
        regista_project_name,
    )
    from agent_notes.core.reconcile import reconcile

    _ws_id, _proj_id, _workspace, project_slug = _resolve(None, None, str(cwd))
    project = regista_project_name(project_slug)
    face = get_face()
    if face is None:
        return
    if hasattr(face, "_base"):
        face = face._base
    reconcile(project, face=face, signer=outbox.get_signer())


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


def run_session_start(payload: dict[str, Any]) -> dict[str, object]:
    cwd = _cwd_for(payload, "SessionStart")
    if cwd is None:
        return _success_output()
    try:
        context = _orientation_for_cwd(cwd)
    except (Exception, SystemExit):
        return _success_output()
    if not context:
        return _success_output()
    return {
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        },
    }


def run_stop(payload: dict[str, Any]) -> dict[str, object]:
    cwd = _cwd_for(payload, "Stop")
    # Codex marks a repeated Stop pass explicitly.  This adapter never requests
    # continuation, but skipping the repeated pass also prevents duplicate I/O
    # if another concurrently-running hook does request one.
    if cwd is None or payload.get("stop_hook_active") is True:
        return _success_output()
    try:
        _reconcile_cwd(cwd)
    except (Exception, SystemExit):
        pass
    return _success_output()


def cmd_codex_hook(args: argparse.Namespace) -> int:
    payload = _read_payload(sys.stdin)
    if payload is None:
        result: dict[str, object] = _success_output()
    elif args.codex_hook_event == "session-start":
        result = run_session_start(payload)
    else:
        result = run_stop(payload)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


def register_codex_hook_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("codex-hook", help=argparse.SUPPRESS)
    events = parser.add_subparsers(dest="codex_hook_event", required=True)
    for event in ("session-start", "stop"):
        event_parser = events.add_parser(event, help=argparse.SUPPRESS)
        event_parser.set_defaults(func=cmd_codex_hook)
