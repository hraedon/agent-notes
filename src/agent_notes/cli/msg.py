"""`agent-notes msg ...` and `agent-notes session policy` (Plan 025 Phase 3, §5).

Every command works outside a registered repo (project = None). The caller's
session resolves per D5; ``--as`` overrides it.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable

from agent_notes.cli.common import (
    EXIT_CONFLICT,
    EXIT_GENERIC,
    EXIT_NOT_CONFIGURED,
    EXIT_NOT_FOUND,
    EXIT_SUCCESS,
    emit_error,
)
from agent_notes.core import messaging

EXIT_TIMEOUT = 2

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$", re.IGNORECASE)
_UNIT_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> timedelta:
    """Parse ``90``, ``45s``, ``30m``, ``2h`` or ``1d``."""
    match = _DURATION_RE.match(text)
    if not match:
        raise argparse.ArgumentTypeError(f"not a duration (e.g. 30m, 2h, 90s): {text!r}")
    return timedelta(seconds=float(match.group(1)) * _UNIT_SECONDS[match.group(2).lower()])


@dataclass
class Caller:
    """Where a CLI call comes from: VM, workspace, project and session (if any)."""

    vm: str
    workspace_id: int
    project_id: int | None
    project: str | None
    session: messaging.ResolvedSession | None
    session_row: dict | None = None

    @property
    def key(self) -> str | None:
        return self.session.session_key if self.session else None


def resolve_caller(args: argparse.Namespace, *, register: bool = True) -> Caller:
    """Resolve the caller and, with ``register``, heartbeat its local session."""
    from agent_notes.core.project_discovery import discover_project

    cwd = os.getcwd()
    vm = messaging.resolve_vm_id()
    workspace_id, project_id, project = None, None, None
    found = discover_project(cwd)
    if found is not None:
        located = messaging.lookup_project(found.workspace, found.project)
        if located is not None:
            workspace_id, project_id = located
            project = found.project
    if workspace_id is None:
        workspace_id = messaging.default_workspace_id()
    session = messaging.resolve_session(
        getattr(args, "as_session", None), cwd=cwd, env=os.environ, vm=vm
    )
    caller = Caller(vm, workspace_id, project_id, project, session)
    if session is not None:
        if register and session.vm_id == vm:
            caller.session_row = messaging.register_session(
                session.vm_id,
                session.harness,
                session.session_id,
                workspace_id=workspace_id,
                project_id=project_id,
                cwd=cwd,
                paseo_agent_id=session.paseo_agent_id,
                replace_location=False,
            )
        else:
            caller.session_row = messaging.get_session(session.session_key)
        if caller.session_row and caller.session_row.get("project"):
            caller.project = caller.session_row["project"]
            caller.workspace_id = caller.session_row["workspace_id"]
    return caller


def _guarded(func: Callable[[argparse.Namespace], int]) -> Callable[[argparse.Namespace], int]:
    """Map mailbox and store failures to the common error envelope."""

    @functools.wraps(func)
    def wrapper(args: argparse.Namespace) -> int:
        use_json = bool(getattr(args, "json", False))
        try:
            return func(args)
        except messaging.AmbiguousSessionError as exc:
            return emit_error(
                "AMBIGUOUS_SESSION",
                str(exc),
                use_json=use_json,
                detail={"candidates": exc.candidates},
                exit_code=EXIT_CONFLICT,
            )
        except messaging.SessionNotFoundError as exc:
            return emit_error(
                "SESSION_NOT_FOUND", str(exc), use_json=use_json, exit_code=EXIT_NOT_FOUND
            )
        except messaging.MessagingError as exc:
            return emit_error(
                "VALIDATION_FAILED", str(exc), use_json=use_json, exit_code=EXIT_CONFLICT
            )
        except LookupError as exc:
            return emit_error("NOT_FOUND", str(exc), use_json=use_json, exit_code=EXIT_NOT_FOUND)
        except RuntimeError as exc:
            return emit_error(
                "STORE_NOT_CONFIGURED", str(exc), use_json=use_json, exit_code=EXIT_NOT_CONFIGURED
            )
        except Exception as exc:
            import psycopg

            if not isinstance(exc, psycopg.Error):
                raise
            return emit_error(
                "STORE_UNAVAILABLE",
                f"database error: {exc}",
                use_json=use_json,
                retryable=isinstance(exc, psycopg.OperationalError),
                exit_code=EXIT_GENERIC,
            )

    return wrapper


def _print_json(data: dict[str, Any]) -> None:
    print(json.dumps(data, indent=2, default=str, ensure_ascii=False))


def _message_out(msg: dict) -> dict:
    return {k: v for k, v in msg.items() if k not in ("recipient_registered", "routed")}


def _read_body(body: str) -> str:
    return sys.stdin.read() if body == "-" else body


def _no_session_hint(caller: Caller) -> str:
    return (
        f"no session resolved on {caller.vm}: pass --as <vm>/<harness>/<session_id>, "
        "set AGENT_NOTES_SESSION, or run inside a harness that sets its session ID"
    )


# ---------------------------------------------------------------------------
# msg send / reply
# ---------------------------------------------------------------------------


@_guarded
def cmd_msg_send(args: argparse.Namespace) -> int:
    use_json = args.json
    if args.to and (args.to_project or args.to_vm):
        raise messaging.MessagingError("use --to, or --to-project/--to-vm, not both")
    caller = resolve_caller(args)
    msg = messaging.send_message(
        workspace_id=caller.workspace_id,
        from_session=caller.key,
        from_vm=caller.vm,
        from_project=caller.project,
        to_session=args.to,
        to_project=args.to_project,
        to_vm=args.to_vm,
        kind=args.kind,
        subject=args.subject,
        body=_read_body(args.body),
        wake=args.wake,
        expires_in=args.expires_in,
    )
    warnings: list[str] = []
    if not msg["recipient_registered"]:
        warnings.append(
            f"no session {args.to!r} is registered yet; the message waits until it appears"
        )
    for w in warnings:
        print(f"Warning: {w}", file=sys.stderr)
    if use_json:
        _print_json({"message": _message_out(msg), "warnings": warnings})
    else:
        if args.to:
            target = args.to
        else:
            parts = [f"project {args.to_project}"] if args.to_project else []
            parts += [f"vm {args.to_vm}"] if args.to_vm else []
            target = " + ".join(parts) + " inbox"
        print(f"Sent #{msg['id']} {msg['kind']} to {target}.")
    return EXIT_SUCCESS


@_guarded
def cmd_msg_reply(args: argparse.Namespace) -> int:
    caller = resolve_caller(args)
    msg = messaging.reply(
        args.id,
        from_session=caller.key,
        from_vm=caller.vm,
        from_project=caller.project,
        subject=args.subject,
        body=_read_body(args.body),
        wake=args.wake,
    )
    routed = msg["routed"]
    if args.json:
        _print_json({"message": _message_out(msg), "routed": routed})
    else:
        to = msg["to_session"] or msg["to_project"] or msg["to_vm"]
        via = "" if routed == "direct" else f" ({routed.replace('_', ' ')}: sender had no session)"
        print(f"Replied #{msg['id']} to {to}{via}.")
    return EXIT_SUCCESS


# ---------------------------------------------------------------------------
# msg inbox / await
# ---------------------------------------------------------------------------


@_guarded
def cmd_msg_inbox(args: argparse.Namespace) -> int:
    caller = resolve_caller(args)
    rows = messaging.inbox(
        caller.key,
        claim=args.claim,
        include_claimed=args.all,
        vm=caller.vm,
        project=caller.project,
        workspace_id=caller.workspace_id,
        limit=args.limit,
    )
    if args.json:
        _print_json(
            {
                "session": caller.key,
                "project": caller.project,
                "vm": caller.vm,
                "claimed": bool(args.claim),
                "messages": rows,
            }
        )
        return EXIT_SUCCESS
    who = caller.key or f"{caller.vm} (anonymous)"
    if not rows:
        print(f"No messages for {who}.")
        return EXIT_SUCCESS
    print(f"{len(rows)} message(s) for {who}:")
    for row in rows:
        print()
        print(messaging.format_peer_message(row))
    return EXIT_SUCCESS


@_guarded
def cmd_msg_await(args: argparse.Namespace) -> int:
    caller = resolve_caller(args)
    if caller.key is None and args.reply_to is None and args.from_session is None:
        print(f"Note: {_no_session_hint(caller)}; awaiting inbox messages only.", file=sys.stderr)
    msg = messaging.await_message(
        caller.key,
        reply_to=args.reply_to,
        from_session=args.from_session,
        timeout=args.timeout,
        vm=caller.vm,
        project=caller.project,
        workspace_id=caller.workspace_id,
    )
    if msg is None:
        return emit_error(
            "AWAIT_TIMEOUT",
            f"no matching message within {args.timeout:g}s; run msg await again to keep waiting",
            use_json=args.json,
            retryable=True,
            exit_code=EXIT_TIMEOUT,
        )
    if args.json:
        _print_json({"message": msg})
    else:
        print(messaging.format_peer_message(msg))
    return EXIT_SUCCESS


@_guarded
def cmd_msg_prune(args: argparse.Namespace) -> int:
    counts = messaging.prune()
    if args.json:
        _print_json({"pruned": counts})
    else:
        print(
            f"Pruned {counts['messages']} expired message(s) and "
            f"{counts['sessions']} stale session(s)."
        )
    return EXIT_SUCCESS


# ---------------------------------------------------------------------------
# session policy
# ---------------------------------------------------------------------------


@_guarded
def cmd_session_policy(args: argparse.Namespace) -> int:
    caller = resolve_caller(args)
    if caller.key is None:
        return emit_error(
            "NO_SESSION", _no_session_hint(caller), use_json=args.json, exit_code=EXIT_NOT_FOUND
        )
    row = caller.session_row
    if args.wake_policy is not None or args.trust is not None:
        row = messaging.set_session_policy(
            caller.key, wake_policy=args.wake_policy, trust=args.trust
        )
    if row is None:
        raise messaging.SessionNotFoundError(f"session {caller.key!r} is not registered")
    if args.json:
        _print_json({"session": row})
    else:
        explicit = " (set explicitly)" if row["wake_policy_explicit"] else " (default)"
        print(
            f"{row['session_key']}: wake_policy={row['wake_policy']}{explicit}, "
            f"trust={row['trust']}"
        )
    return EXIT_SUCCESS


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def add_as_argument(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--as",
        dest="as_session",
        default=None,
        metavar="SESSION",
        help="Act as this session (full key '<vm>/<harness>/<id>' or a bare ID)",
    )


def register_msg_parsers(sub: argparse._SubParsersAction) -> None:
    msg = sub.add_parser("msg", help="Agent-to-agent messages (Plan 025 mailbox)")
    msg_sub = msg.add_subparsers(dest="msg_cmd")

    send = msg_sub.add_parser("send", help="Send a message to a session or an inbox")
    send.add_argument("--to", default=None, metavar="SESSION_KEY", help="Direct recipient")
    send.add_argument("--to-project", default=None, help="Project inbox (slug)")
    send.add_argument("--to-vm", default=None, help="VM inbox (alone, or with --to-project)")
    send.add_argument("--body", required=True, help="Message body ('-' reads stdin)")
    send.add_argument(
        "--kind",
        default="message",
        choices=("message", "question", "handoff", "notice"),
    )
    send.add_argument("--subject", default=None)
    send.add_argument(
        "--wake", action="store_true", help="Ask to wake the recipient (direct messages only)"
    )
    send.add_argument("--expires-in", type=parse_duration, default=None, help="Expiry (default 7d)")
    send.add_argument("--json", action="store_true")
    add_as_argument(send)
    send.set_defaults(func=cmd_msg_send)

    rep = msg_sub.add_parser("reply", help="Reply to a message, direct to its sender")
    rep.add_argument("id", type=int)
    rep.add_argument("--body", required=True, help="Reply body ('-' reads stdin)")
    rep.add_argument("--subject", default=None)
    rep.add_argument("--wake", action="store_true")
    rep.add_argument("--json", action="store_true")
    add_as_argument(rep)
    rep.set_defaults(func=cmd_msg_reply)

    inbox = msg_sub.add_parser("inbox", help="List messages waiting for this session")
    inbox.add_argument("--claim", action="store_true", help="Claim what is listed")
    inbox.add_argument("--all", action="store_true", help="Include already-claimed messages")
    inbox.add_argument("--limit", type=int, default=50)
    inbox.add_argument("--json", action="store_true")
    add_as_argument(inbox)
    inbox.set_defaults(func=cmd_msg_inbox)

    wait = msg_sub.add_parser("await", help="Block until a message arrives, then claim it")
    wait.add_argument("--reply-to", type=int, default=None, metavar="ID")
    wait.add_argument("--from", dest="from_session", default=None, metavar="SESSION_KEY")
    wait.add_argument(
        "--timeout",
        type=float,
        default=messaging.DEFAULT_AWAIT_TIMEOUT,
        help="Seconds to wait (default 120); exits 2 on timeout",
    )
    wait.add_argument("--json", action="store_true")
    add_as_argument(wait)
    wait.set_defaults(func=cmd_msg_await)

    prune = msg_sub.add_parser("prune", help="Delete expired messages and stale sessions")
    prune.add_argument("--json", action="store_true")
    prune.set_defaults(func=cmd_msg_prune)

    msg.set_defaults(func=lambda args: (msg.print_help(), EXIT_SUCCESS)[1])


def register_session_parsers(sub: argparse._SubParsersAction) -> None:
    session = sub.add_parser("session", help="This agent session's mailbox settings")
    session_sub = session.add_subparsers(dest="session_cmd")

    policy = session_sub.add_parser("policy", help="Show or set wake policy and trust")
    policy.add_argument("--wake-policy", default=None, choices=messaging.WAKE_POLICIES)
    policy.add_argument("--trust", default=None, choices=messaging.TRUST_LEVELS)
    policy.add_argument("--json", action="store_true")
    add_as_argument(policy)
    policy.set_defaults(func=cmd_session_policy)

    session.set_defaults(func=lambda args: (session.print_help(), EXIT_SUCCESS)[1])
