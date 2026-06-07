from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from agent_notes.cli.common import (
    EXIT_SUCCESS,
    _add_common,
    _print_sub_help,
)


def cmd_events_tail(args: argparse.Namespace) -> int:
    """Replayable event tail — the level delivery mode (Invariant W).

    Returns events newer than ``cursor`` (last-seen ``op_log_events.id``).
    Pass 0 to start from the beginning, or omit to get recent events.
    """
    use_json = getattr(args, "json", False)
    from agent_notes.core.kernel import events_since

    cursor = args.cursor or 0
    rows = events_since(
        cursor=cursor,
        event_type=args.event_type,
        limit=min(args.limit or 50, 200),
    )

    if use_json:
        print(json.dumps({"events": rows, "cursor": cursor}, indent=2, default=str))
    else:
        if not rows:
            print(f"No events newer than cursor={cursor}.")
        else:
            print(f"{len(rows)} event(s) newer than cursor={cursor}:")
            for r in rows:
                ts = r["created_at"]
                if isinstance(ts, datetime):
                    ts_str = ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                else:
                    ts_str = str(ts)
                print(f"  [{r['id']}] {r['event_type']} (op={r['op_id']}) @ {ts_str}")
                if r.get("payload"):
                    print(f"    payload: {json.dumps(r['payload'], indent=2, default=str)}")
    return EXIT_SUCCESS


def register_events_parsers(sub: argparse._SubParsersAction) -> None:
    ev = sub.add_parser("events", help="Event tail (op-log replay)")
    ev_sub = ev.add_subparsers(dest="ev_cmd")

    ev_tail = ev_sub.add_parser("tail", help="Replay events since a cursor")
    ev_tail.add_argument(
        "--cursor",
        type=int,
        default=0,
        help="Last-seen op_log_events.id (default: 0 = from beginning)",
    )
    ev_tail.add_argument(
        "--event-type",
        default=None,
        dest="event_type",
        help="Filter to a specific event type (e.g. item.created, item.status_changed)",
    )
    ev_tail.add_argument("--limit", type=int, default=50)
    _add_common(ev_tail)
    ev_tail.set_defaults(func=cmd_events_tail)

    ev.set_defaults(func=lambda args: (_print_sub_help(ev), EXIT_SUCCESS)[1])
