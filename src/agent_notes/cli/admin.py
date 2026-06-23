from __future__ import annotations

import argparse
import sys

from agent_notes.cli.common import EXIT_GENERIC, EXIT_NOT_CONFIGURED


def cmd_migrate_to_regista(args: argparse.Namespace) -> int:
    from agent_notes.scripts.migrate_to_regista import main as migrate_main

    argv = []
    if args.project:
        argv.extend(["--project", args.project])
    if args.apply:
        argv.append("--apply")
    try:
        return migrate_main(argv)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_NOT_CONFIGURED
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_GENERIC


def register_admin_parsers(sub: argparse._SubParsersAction) -> None:
    migrate_p = sub.add_parser(
        "migrate-to-regista",
        help="Migrate local work_items into regista breadcrumbs (dry-run by default)",
    )
    migrate_p.add_argument("--project", default=None, help="Project slug (default: all projects)")
    migrate_p.add_argument(
        "--apply",
        action="store_true",
        help="Apply the migration; without this flag, only report what would happen",
    )
    migrate_p.set_defaults(func=cmd_migrate_to_regista)
