from __future__ import annotations

import argparse
import json

from agent_notes.cli.common import EXIT_SUCCESS


def cmd_ws_list(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    from agent_notes.core.db import list_projects, list_workspaces

    workspaces = list_workspaces()
    rows = []
    for w in workspaces:
        projects = list_projects(workspace_id=w.id)
        rows.append(
            {
                "id": w.id,
                "slug": w.slug,
                "name": w.name,
                "project_count": len(projects),
            }
        )

    if use_json:
        print(json.dumps({"workspaces": rows}, indent=2, default=str))
    else:
        if not rows:
            print("No workspaces.")
        else:
            print(f"{len(rows)} workspace(s):")
            for r in rows:
                print(f"- {r['slug']} (id={r['id']}, projects={r['project_count']}) — {r['name']}")
    return EXIT_SUCCESS


def register_workspace_parsers(sub: argparse._SubParsersAction) -> None:
    ws = sub.add_parser("workspace", help="Workspace operations")
    ws_sub = ws.add_subparsers(dest="ws_cmd")

    ws_list = ws_sub.add_parser("list", help="List workspaces")
    ws_list.add_argument("--json", action="store_true")
    ws_list.set_defaults(func=cmd_ws_list)

    ws.set_defaults(func=lambda args: (ws.print_help(), EXIT_SUCCESS)[1])
