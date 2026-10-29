"""`agent-notes who`: registered agent sessions (Plan 025 Phase 3, §5)."""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta, timezone

from agent_notes.cli.common import EXIT_SUCCESS
from agent_notes.cli.msg import (
    _guarded,
    _print_json,
    add_as_argument,
    parse_duration,
    resolve_caller,
)
from agent_notes.core import messaging


def _ago(ts: datetime) -> str:
    seconds = max(0, int((datetime.now(timezone.utc) - ts).total_seconds()))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _why_no_session(vm: str) -> str:
    env_names = [messaging.SESSION_ENV, *(var for var, _ in messaging.HARNESS_ENV_VARS)]
    return (
        f"No session resolved on {vm}: --as was not given, none of {', '.join(env_names)} "
        f"is set, and no session on {vm} registered at or above {os.getcwd()} was seen in "
        "the last 10 minutes. This shell is anonymous: it can send and read inboxes but "
        "can't receive direct messages or replies."
    )


def _cmd_who_me(args: argparse.Namespace) -> int:
    caller = resolve_caller(args, register=False)
    if caller.session is None:
        reason = _why_no_session(caller.vm)
        if args.json:
            _print_json({"session": None, "vm": caller.vm, "reason": reason})
        else:
            print(reason)
        return EXIT_SUCCESS
    resolved = caller.session.to_dict()
    if args.json:
        _print_json(
            {
                "session": {**resolved, "registered": caller.session_row is not None},
                "registration": caller.session_row,
                "vm": caller.vm,
                "reason": None,
            }
        )
        return EXIT_SUCCESS
    print(f"{caller.session.session_key} (resolved from {caller.session.source})")
    row = caller.session_row
    if row is None:
        print("Not registered yet; it registers on the first msg command.")
    else:
        print(
            f"project={row['project'] or '-'} cwd={row['cwd'] or '-'} "
            f"wake_policy={row['wake_policy']} trust={row['trust']} "
            f"seen {_ago(row['last_seen_at'])}"
        )
    return EXIT_SUCCESS


@_guarded
def cmd_who(args: argparse.Namespace) -> int:
    if args.me:
        return _cmd_who_me(args)
    active: timedelta | None = args.active_within
    if active is not None and active.total_seconds() <= 0:
        active = None
    rows = messaging.list_sessions(project=args.project, vm=args.vm, active_within=active)
    if args.json:
        _print_json({"sessions": rows})
        return EXIT_SUCCESS
    if not rows:
        print("No active sessions.")
        return EXIT_SUCCESS
    print(f"{len(rows)} session(s):")
    for r in rows:
        extras = [f"wake={r['wake_policy']}"]
        if r["paseo_agent_id"]:
            extras.append("paseo")
        if r["trust"] != "normal":
            extras.append(r["trust"])
        print(
            f"- {r['session_key']}  project={r['project'] or '-'}  "
            f"seen {_ago(r['last_seen_at'])}  {' '.join(extras)}"
        )
        if r["cwd"]:
            print(f"    cwd {r['cwd']}")
    return EXIT_SUCCESS


def register_who_parser(sub: argparse._SubParsersAction) -> None:
    who = sub.add_parser("who", help="List agent sessions (Plan 025 presence)")
    who.add_argument("--project", default=None, help="Only sessions on this project slug")
    who.add_argument("--vm", default=None, help="Only sessions on this VM")
    who.add_argument(
        "--active-within",
        type=parse_duration,
        default=timedelta(minutes=30),
        metavar="DURATION",
        help="Seen within this long (default 30m; 0 for all)",
    )
    who.add_argument("--me", action="store_true", help="Show this caller's resolved session")
    who.add_argument("--json", action="store_true")
    add_as_argument(who)
    who.set_defaults(func=cmd_who)
