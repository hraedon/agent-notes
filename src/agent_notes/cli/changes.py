from __future__ import annotations

import argparse
import json
from datetime import datetime

from agent_notes.cli.common import EXIT_GENERIC, EXIT_SUCCESS, _print_sub_help


def cmd_changes_since(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    from agent_notes.core.change_log import changes_since as cl_changes_since

    try:
        since = datetime.fromisoformat(args.since)
    except ValueError as exc:
        if use_json:
            print(json.dumps({"error": f"invalid timestamp: {exc}"}, indent=2))
        else:
            print(f"Invalid timestamp: {exc}")
        return EXIT_GENERIC

    rows = cl_changes_since(since, limit=min(args.limit or 50, 200))
    if use_json:
        print(
            json.dumps(
                {
                    "changes": [
                        {
                            "kind": r.kind,
                            "identifier": r.identifier,
                            "event": r.event,
                            "changed_at": r.changed_at.isoformat(),
                        }
                        for r in rows
                    ]
                },
                indent=2,
                default=str,
            )
        )
    else:
        if not rows:
            print("No changes found.")
        else:
            print(f"{len(rows)} change(s) since {args.since}:")
            for r in rows:
                print(f"- [{r.kind}] {r.identifier} event={r.event} at {r.changed_at}")
    return EXIT_SUCCESS


def register_changes_parsers(sub: argparse._SubParsersAction) -> None:
    changes = sub.add_parser("changes", help="Change log operations")
    changes_sub = changes.add_subparsers(dest="changes_cmd")

    changes_since = changes_sub.add_parser("since", help="List changes since a timestamp")
    changes_since.add_argument("since")
    changes_since.add_argument("--limit", type=int, default=50)
    changes_since.add_argument("--json", action="store_true")
    changes_since.set_defaults(func=cmd_changes_since)

    changes.set_defaults(func=lambda args: (_print_sub_help(changes), EXIT_SUCCESS)[1])
