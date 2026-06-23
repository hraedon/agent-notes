"""CLI for outbox status and reconcile (Plan 009 §6.4-6.5).

    agent-notes outbox status   [--project <slug>] [--json]
    agent-notes outbox reconcile [--project <slug>] [--json]
"""

from __future__ import annotations

import argparse
import json
import sys

from agent_notes.cli.common import EXIT_GENERIC, EXIT_NOT_CONFIGURED, EXIT_SUCCESS
from agent_notes.core import outbox


def cmd_outbox_status(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    project = getattr(args, "project", None)

    if project:
        pending = outbox.count_ops(project)
        rejected = outbox.count_sidecar(project, "rejected.jsonl")
        conflicts = outbox.count_sidecar(project, "conflicts.jsonl")
        if use_json:
            print(
                json.dumps(
                    {
                        "project": project,
                        "pending": pending,
                        "rejected": rejected,
                        "conflicts": conflicts,
                    }
                )
            )
        else:
            print(f"Project: {project}")
            print(f"  Pending:   {pending}")
            print(f"  Rejected:  {rejected}")
            print(f"  Conflicts: {conflicts}")
        return EXIT_SUCCESS

    projects = outbox.list_projects()
    if not projects:
        if use_json:
            print(json.dumps({"projects": []}))
        else:
            print("No pending outbox ops.")
        return EXIT_SUCCESS

    results = []
    for p in projects:
        pending = outbox.count_ops(p)
        rejected = outbox.count_sidecar(p, "rejected.jsonl")
        conflicts = outbox.count_sidecar(p, "conflicts.jsonl")
        results.append(
            {
                "project": p,
                "pending": pending,
                "rejected": rejected,
                "conflicts": conflicts,
            }
        )
    if use_json:
        print(json.dumps({"projects": results}, indent=2))
    else:
        for r in results:
            print(
                f"{r['project']}: {r['pending']} pending"
                f" / {r['rejected']} rejected"
                f" / {r['conflicts']} conflicts"
            )
    return EXIT_SUCCESS


def cmd_outbox_reconcile(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)

    from agent_notes.core.config import regista_config

    cfg = regista_config()
    project = getattr(args, "project", None) or cfg.project

    from agent_notes.core.face_factory import get_face

    face = get_face()
    if face is None:
        msg = "regista writes not enabled; set AGENT_NOTES_REGISTA_WRITES=1"
        if use_json:
            print(json.dumps({"error": msg}))
        else:
            print(msg, file=sys.stderr)
        return EXIT_NOT_CONFIGURED

    if hasattr(face, "_base"):
        face = face._base

    signer = outbox.get_signer()
    from agent_notes.core.reconcile import reconcile

    report = reconcile(project, face=face, signer=signer)

    if use_json:
        print(json.dumps(report.to_dict(), indent=2, default=str))
    else:
        print(report.summary())
        if report.conflict_details:
            for d in report.conflict_details:
                print(
                    f"  CONFLICT: {d.get('op')} seq={d.get('client_seq')} "
                    f"expected={d.get('expected_state')} actual={d.get('actual_state')}"
                )
        if report.rejected_details:
            for d in report.rejected_details:
                print(f"  REJECTED: seq={d.get('client_seq')} reason={d.get('reason')}")

    if report.rejected or report.conflicts:
        return EXIT_GENERIC
    return EXIT_SUCCESS


def register_outbox_parsers(sub: argparse._SubParsersAction) -> None:
    outbox_p = sub.add_parser("outbox", help="Outbox status and reconcile")
    outbox_sub = outbox_p.add_subparsers(dest="outbox_cmd")

    status_p = outbox_sub.add_parser("status", help="Show pending/rejected/conflict counts")
    status_p.add_argument("--project", default=None, help="Regista project slug")
    status_p.add_argument("--json", action="store_true")
    status_p.set_defaults(func=cmd_outbox_status)

    reconcile_p = outbox_sub.add_parser("reconcile", help="Replay pending ops into regista")
    reconcile_p.add_argument("--project", default=None, help="Regista project slug")
    reconcile_p.add_argument("--json", action="store_true")
    reconcile_p.set_defaults(func=cmd_outbox_reconcile)

    outbox_p.set_defaults(func=lambda args: (_print_sub_help(outbox_p), EXIT_SUCCESS)[1])


def _print_sub_help(parser: argparse.ArgumentParser) -> None:
    parser.print_help()
