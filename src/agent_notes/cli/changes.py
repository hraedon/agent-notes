from __future__ import annotations

import argparse
import json
from datetime import datetime

from agent_notes.cli.common import EXIT_GENERIC, EXIT_SUCCESS, _print_sub_help, emit_error


def cmd_changes_since(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    from agent_notes.core.change_log import changes_since as cl_changes_since

    try:
        since = datetime.fromisoformat(args.since)
    except ValueError as exc:
        return emit_error(
            "INVALID_ARGUMENT",
            f"invalid timestamp: {exc}",
            use_json=use_json,
            exit_code=EXIT_GENERIC,
        )

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


def cmd_changes_archive(args: argparse.Namespace) -> int:
    """Archive change_log rows older than N days to a change_log_archive table.

    Creates the archive table if it doesn't exist. Idempotent: archived rows
    are removed from change_log and inserted into change_log_archive.
    """
    use_json = getattr(args, "json", False)
    from datetime import timedelta, timezone

    from agent_notes.core.db import _conn

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)

    with _conn() as conn:
        cur = conn.cursor()
        # Ensure archive table exists
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS change_log_archive (
                LIKE change_log INCLUDING ALL
            )
            """
        )
        # Copy old rows to archive
        cur.execute(
            """
            INSERT INTO change_log_archive
            SELECT * FROM change_log
            WHERE changed_at < %s
            ON CONFLICT DO NOTHING
            """,
            (cutoff,),
        )
        archived_count = cur.rowcount
        # Delete from change_log
        cur.execute(
            "DELETE FROM change_log WHERE changed_at < %s",
            (cutoff,),
        )
        deleted_count = cur.rowcount
        conn.commit()

    if use_json:
        print(
            json.dumps(
                {
                    "archived": archived_count,
                    "deleted": deleted_count,
                    "cutoff": cutoff.isoformat(),
                },
                indent=2,
            )
        )
    else:
        print(
            f"Archived {archived_count} change_log row(s) older than "
            f"{args.days} days (cutoff: {cutoff.isoformat()})."
        )
    return EXIT_SUCCESS


def register_changes_parsers(sub: argparse._SubParsersAction) -> None:
    changes = sub.add_parser("changes", help="Change log operations")
    changes_sub = changes.add_subparsers(dest="changes_cmd")

    changes_since = changes_sub.add_parser("since", help="List changes since a timestamp")
    changes_since.add_argument("since")
    changes_since.add_argument("--limit", type=int, default=50)
    changes_since.add_argument("--json", action="store_true")
    changes_since.set_defaults(func=cmd_changes_since)

    changes_archive = changes_sub.add_parser(
        "archive", help="Archive old change_log rows to change_log_archive"
    )
    changes_archive.add_argument(
        "--days", type=int, default=90, help="Archive rows older than N days (default: 90)"
    )
    changes_archive.add_argument("--json", action="store_true")
    changes_archive.set_defaults(func=cmd_changes_archive)

    changes.set_defaults(func=lambda args: _print_sub_help(changes))
