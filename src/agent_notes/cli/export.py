"""Export/import CLI commands for agent-notes (Plan 002 / operational gap)."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

import psycopg

from agent_notes.cli.common import (
    EXIT_GENERIC,
    EXIT_NOT_CONFIGURED,
    EXIT_SUCCESS,
    _resolve,
    report_resolution_failure,
)


def cmd_export(args: argparse.Namespace) -> int:
    """Export all data for a project (or entire workspace) as JSON."""
    from agent_notes.core.db import list_projects, list_workspaces
    from agent_notes.core.memory_model import get_memory, list_memories
    from agent_notes.core.work_item_model import WorkItemModel

    if args.path or (args.workspace and args.project):
        try:
            ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
            report_resolution_failure(args, code, use_json=True)
            return code

        workspaces = [
            {
                "ws_id": ws_id,
                "ws_slug": ws_slug,
                "proj_id": proj_id,
                "proj_slug": proj_slug,
            }
        ]
    elif args.workspace:
        ws = next((w for w in list_workspaces() if w.slug == args.workspace), None)
        if ws is None:
            available = [w.slug for w in list_workspaces()]
            hint = f" Available: {', '.join(available)}" if available else ""
            suggestion = " Use --path for auto-resolution or run 'agent-notes workspace list'."
            print(
                json.dumps(
                    {
                        "error": f"workspace '{args.workspace}' not found",
                        "available": available,
                    }
                )
            )
            print(f"Workspace '{args.workspace}' not found.{hint}{suggestion}", file=sys.stderr)
            return EXIT_NOT_CONFIGURED
        projects = list_projects(workspace_id=ws.id)
        workspaces = [
            {
                "ws_id": ws.id,
                "ws_slug": ws.slug,
                "proj_id": p.id,
                "proj_slug": p.slug,
            }
            for p in projects
        ]
    else:
        all_ws = list_workspaces()
        workspaces = []
        for ws in all_ws:
            for p in list_projects(workspace_id=ws.id):
                workspaces.append(
                    {
                        "ws_id": ws.id,
                        "ws_slug": ws.slug,
                        "proj_id": p.id,
                        "proj_slug": p.slug,
                    }
                )

    result = {
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "projects": [],
    }

    for entry in workspaces:
        proj_data = {
            "workspace": entry["ws_slug"],
            "project": entry["proj_slug"],
            "work_items": [],
            "memories": [],
        }

        wi_rows = WorkItemModel.query_work_items(project_id=entry["proj_id"], limit=10000)
        for wi in wi_rows:
            body = WorkItemModel.get_work_item_body(entry["proj_id"], wi["identifier"]) or ""
            proj_data["work_items"].append(
                {
                    "identifier": wi["identifier"],
                    "title": wi["title"],
                    "body": body,
                    "kind": wi["kind"],
                    "status": wi["status"],
                    "severity": wi.get("severity", "medium"),
                    "external_refs": wi.get("external_refs", {}),
                    "diagnostic_keys": wi.get("diagnostic_keys", {}),
                    "created_at": (wi["created_at"].isoformat() if wi.get("created_at") else None),
                    "updated_at": (wi["updated_at"].isoformat() if wi.get("updated_at") else None),
                }
            )

        mem_summaries = list_memories(
            workspace_id=entry["ws_id"],
            project_id=entry["proj_id"],
            limit=10000,
        )
        for m in mem_summaries:
            full = get_memory(entry["ws_id"], entry["proj_id"], m["name"])
            if full:
                proj_data["memories"].append(
                    {
                        "name": full["name"],
                        "memory_type": full["memory_type"],
                        "body": full["body"],
                        "attributes": full.get("attributes", {}),
                        "created_at": (
                            full["created_at"].isoformat() if full.get("created_at") else None
                        ),
                        "updated_at": (
                            full["updated_at"].isoformat() if full.get("updated_at") else None
                        ),
                    }
                )

        result["projects"].append(proj_data)

    json.dump(result, sys.stdout, indent=2, default=str, ensure_ascii=False)
    print()  # trailing newline
    return EXIT_SUCCESS


def cmd_import(args: argparse.Namespace) -> int:
    """Import data from a JSON export file."""
    from agent_notes.core.db import get_or_create_project, get_or_create_workspace
    from agent_notes.core.embed import embed
    from agent_notes.core.memory_model import add_memory
    from agent_notes.core.work_item_model import WorkItemModel

    try:
        with open(args.file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"Error reading {args.file}: {exc}", file=sys.stderr)
        return EXIT_GENERIC

    projects = data.get("projects", [])
    if not projects:
        print("No projects found in export file.", file=sys.stderr)
        return EXIT_GENERIC

    total_wi = 0
    total_mem = 0

    for proj_data in projects:
        ws_slug = proj_data.get("workspace", "default")
        proj_slug = proj_data.get("project", "imported")

        ws = get_or_create_workspace(ws_slug, ws_slug.replace("-", " ").title())
        proj = get_or_create_project(ws.id, proj_slug, proj_slug)

        for wi in proj_data.get("work_items", []) + proj_data.get("breadcrumbs", []):
            text = wi.get("title", "") + " " + wi.get("body", "")
            vec = embed(text, task="document").tolist() if text.strip() else None
            try:
                WorkItemModel.file_work_item(
                    project_id=proj.id,
                    identifier=wi.get("identifier"),
                    title=wi.get("title", ""),
                    body=wi.get("body", ""),
                    kind=wi.get("kind", "todo"),
                    status=wi.get("status", "open"),
                    severity=wi.get("severity", "medium"),
                    external_refs=wi.get("external_refs"),
                    diagnostic_keys=wi.get("diagnostic_keys"),
                    embedding=vec,
                )
                total_wi += 1
            except ValueError as exc:
                print(
                    f"  Skipped work item {wi.get('identifier')}: {exc}",
                    file=sys.stderr,
                )
            except psycopg.errors.UniqueViolation:
                print(
                    f"  Skipped work item {wi.get('identifier')}: already exists",
                    file=sys.stderr,
                )

        for mem in proj_data.get("memories", []):
            body = mem.get("body", "")
            vec = embed(body, task="document").tolist() if body.strip() else None
            try:
                add_memory(
                    workspace_id=ws.id,
                    project_id=proj.id,
                    name=mem["name"],
                    memory_type=mem.get("memory_type", "note"),
                    body=body,
                    attributes=mem.get("attributes"),
                    embedding=vec,
                )
                total_mem += 1
            except (ValueError, KeyError) as exc:
                print(
                    f"  Skipped memory {mem.get('name')}: {exc}",
                    file=sys.stderr,
                )

    print(f"Imported {total_wi} work item(s) and {total_mem} memory(ies).")
    return EXIT_SUCCESS


def register_export_parsers(sub: argparse._SubParsersAction) -> None:
    export_p = sub.add_parser("export", help="Export data as JSON")
    export_p.add_argument("--path", default=None)
    export_p.add_argument("--workspace", default=None)
    export_p.add_argument("--project", default=None)
    export_p.set_defaults(func=cmd_export)

    import_p = sub.add_parser("import", help="Import data from JSON export")
    import_p.add_argument("file", help="Path to JSON export file")
    import_p.set_defaults(func=cmd_import)
