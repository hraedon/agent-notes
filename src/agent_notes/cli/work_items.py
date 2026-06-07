from __future__ import annotations

import argparse
import json
from typing import Any

from agent_notes.cli.common import (
    EXIT_CONFLICT,
    EXIT_NOT_CONFIGURED,
    EXIT_NOT_FOUND,
    EXIT_SUCCESS,
    _add_common,
    _print_sub_help,
    _resolve,
    report_resolution_failure,
)


def _wi_format(wi: dict) -> str:
    lines = [
        f"**{wi['identifier']}** — {wi['title']}",
        f"Kind: {wi['kind']} | Status: {wi['status']} | Severity: {wi['severity']}",
        f"Created: {wi['created_at']}",
        f"Updated: {wi['updated_at']}",
        "",
        "(Body stored as content-addressed blob; use `get --with-body` to view)",
    ]
    return "\n".join(lines)


def cmd_wi_file(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return code

    from agent_notes.core.embed import embed
    from agent_notes.core.work_item_model import WorkItemModel

    vec = embed(args.title + " " + (args.body or ""), task="document").tolist()
    external_refs = json.loads(args.external_refs) if args.external_refs else None
    diagnostic_keys = json.loads(args.diagnostic_keys) if args.diagnostic_keys else None
    try:
        wi = WorkItemModel.file_work_item(
            project_id=proj_id,
            identifier=args.identifier,
            title=args.title,
            body=args.body or "",
            kind=args.type,
            status=args.status,
            severity=args.severity or "medium",
            external_refs=external_refs,
            diagnostic_keys=diagnostic_keys,
            embedding=vec,
        )
    except ValueError as exc:
        if use_json:
            print(json.dumps({"error": str(exc)}, indent=2))
        else:
            print(f"Error: {exc}")
        return EXIT_CONFLICT

    if use_json:
        print(json.dumps({"work_item": wi}, indent=2, default=str))
    else:
        ident = wi["identifier"]
        kind = wi["kind"]
        status = wi["status"]
        print(f"Work item filed: **{ident}** ({kind} / {status}) in project {proj_slug}")
    return EXIT_SUCCESS


def cmd_wi_update(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return code

    from agent_notes.core.embed import embed
    from agent_notes.core.work_item_model import WorkItemModel

    fields: dict[str, Any] = {}
    if args.title is not None:
        fields["title"] = args.title
    if args.body is not None and args.append_body is not None:
        if use_json:
            msg = "--body and --append-body are mutually exclusive"
            print(json.dumps({"error": msg}, indent=2))
        else:
            print("Error: --body and --append-body are mutually exclusive")
        return EXIT_CONFLICT
    if args.body is not None:
        fields["body"] = args.body
    if args.append_body is not None:
        old = WorkItemModel.get_work_item(proj_id, args.identifier)
        if old is None:
            if use_json:
                print(json.dumps({"error": "not found"}, indent=2))
            else:
                print(f"Work item '{args.identifier}' not found.")
            return EXIT_NOT_FOUND
        existing_body = WorkItemModel.get_work_item_body(proj_id, args.identifier) or ""
        existing = existing_body.rstrip()
        sep = "\n\n" if existing else ""
        fields["body"] = f"{existing}{sep}{args.append_body}"
    if args.type is not None:
        fields["kind"] = args.type
    if args.status is not None:
        fields["status"] = args.status
    if args.severity is not None:
        fields["severity"] = args.severity

    if "body" in fields or "title" in fields:
        old = WorkItemModel.get_work_item(proj_id, args.identifier)
        if old is None:
            if use_json:
                print(json.dumps({"error": "not found"}, indent=2))
            else:
                print(f"Work item '{args.identifier}' not found.")
            return EXIT_NOT_FOUND
        old_body = WorkItemModel.get_work_item_body(proj_id, args.identifier) or ""
        text = fields.get("title", old.get("title", "")) + " " + fields.get("body", old_body)
        fields["embedding"] = embed(text, task="document").tolist()

    try:
        wi = WorkItemModel.update_work_item(
            project_id=proj_id,
            identifier=args.identifier,
            **fields,
        )
    except ValueError as exc:
        if use_json:
            print(json.dumps({"error": str(exc)}, indent=2))
        else:
            print(f"Error: {exc}")
        return EXIT_NOT_FOUND

    if use_json:
        print(json.dumps({"work_item": wi}, indent=2, default=str))
    else:
        print(f"Work item updated: **{wi['identifier']}** ({wi['kind']} / {wi['status']})")
    return EXIT_SUCCESS


def cmd_wi_get(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return code

    from agent_notes.core.work_item_model import WorkItemModel

    wi = WorkItemModel.get_work_item(proj_id, args.identifier)
    if wi is None:
        if use_json:
            print(json.dumps({"error": "not found"}, indent=2))
        else:
            print(f"Work item '{args.identifier}' not found.")
        return EXIT_NOT_FOUND

    if use_json:
        wi.pop("embedding", None)
        if getattr(args, "with_body", False):
            body = WorkItemModel.get_work_item_body(proj_id, args.identifier)
            wi["body"] = body or ""
        print(json.dumps({"work_item": wi}, indent=2, default=str))
    else:
        if getattr(args, "with_body", False):
            body = WorkItemModel.get_work_item_body(proj_id, args.identifier) or ""
            wi = dict(wi)
            wi["body"] = body
        print(_wi_format(wi))
    return EXIT_SUCCESS


def cmd_wi_find(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    scope = getattr(args, "scope", None) or "project"

    from agent_notes.core.db import list_workspaces
    from agent_notes.core.embed import embed
    from agent_notes.core.work_item_model import WorkItemModel

    proj_id: int | None = None
    ws_id: int | None = None

    if scope == "global":
        pass
    elif scope == "workspace":
        if args.workspace:
            ws = next((w for w in list_workspaces() if w.slug == args.workspace), None)
            if ws is None:
                available = [w.slug for w in list_workspaces()]
                hint = f" Available: {', '.join(available)}" if available else ""
                suggestion = " Use --path for auto-resolution or run 'agent-notes workspace list'."
                if use_json:
                    msg = f"workspace '{args.workspace}' not found"
                    print(json.dumps({"error": msg, "available": available}, indent=2))
                else:
                    print(f"Workspace '{args.workspace}' not found.{hint}{suggestion}")
                return EXIT_NOT_FOUND
            ws_id = ws.id
        else:
            try:
                ws_id, _proj_id, _ws_slug, _proj_slug = _resolve(
                    args.workspace, args.project, args.path
                )
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
                report_resolution_failure(args, code)
                return code
    else:
        try:
            ws_id, proj_id, _ws_slug, _proj_slug = _resolve(args.workspace, args.project, args.path)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
            report_resolution_failure(args, code)
            return code

    if args.text:
        text = args.text.strip()
        if text.isdigit() or (text.startswith("WI-") and text[3:].isdigit()):
            rows = WorkItemModel.query_work_items(
                project_id=proj_id,
                workspace_id=ws_id,
                identifier=text,
                limit=1,
            )
            if not rows:
                rows = WorkItemModel.query_work_items(
                    project_id=proj_id,
                    workspace_id=ws_id,
                    limit=min(args.limit or 50, 200),
                )
                rows = [r for r in rows if text.lower() in r["identifier"].lower()]
        else:
            vec = embed(args.text, task="query").tolist()
            rows = WorkItemModel.find_work_items(
                query_vec=vec,
                project_id=proj_id,
                workspace_id=ws_id,
                limit=min(args.limit or 10, 50),
            )
    else:
        rows = WorkItemModel.query_work_items(
            project_id=proj_id,
            workspace_id=ws_id,
            status=args.status,
            kind=args.type,
            limit=min(args.limit or 50, 200),
        )

    if use_json:
        for r in rows:
            r.pop("embedding", None)
        print(json.dumps({"work_items": rows}, indent=2, default=str))
    else:
        if not rows:
            print("No work items found.")
        else:
            print(f"{len(rows)} work item(s) found:")
            for r in rows:
                print(f"- **{r['identifier']}** ({r['kind']} / {r['status']}) — {r['title']}")
    return EXIT_SUCCESS


def cmd_wi_query(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return code

    from agent_notes.core.work_item_model import WorkItemModel

    rows = WorkItemModel.query_work_items(
        project_id=proj_id,
        workspace_id=ws_id,
        limit=min(args.limit or 50, 200),
    )
    if use_json:
        print(json.dumps({"work_items": rows}, indent=2, default=str))
    else:
        if not rows:
            print("No work items found.")
        else:
            for r in rows:
                print(f"- {r['identifier']} | {r['title']} | {r['kind']} | {r['status']}")
    return EXIT_SUCCESS


def cmd_wi_ready(args: argparse.Namespace) -> int:
    """Show work items that are ready (not blocked)."""
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return code

    from agent_notes.core.work_item_model import WorkItemModel

    rows = WorkItemModel.ready_work_items(
        project_id=proj_id,
        workspace_id=ws_id,
        limit=min(args.limit or 50, 200),
    )
    if use_json:
        for r in rows:
            r.pop("embedding", None)
        print(json.dumps({"ready_work_items": rows}, indent=2, default=str))
    else:
        if not rows:
            print("No work items are ready.")
        else:
            print(f"{len(rows)} work item(s) ready:")
            for r in rows:
                print(f"- **{r['identifier']}** ({r['kind']}) — {r['title']}")
    return EXIT_SUCCESS


def cmd_wi_claimable(args: argparse.Namespace) -> int:
    """Show work items that are claimable (ready + not leased). P0: same as ready."""
    return cmd_wi_ready(args)


def cmd_wi_delete(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return code

    from agent_notes.core.work_item_model import WorkItemModel

    deleted = WorkItemModel.delete_work_item(proj_id, args.identifier)
    if not deleted:
        if use_json:
            print(json.dumps({"error": "not found"}, indent=2))
        else:
            print(f"Work item '{args.identifier}' not found.")
        return EXIT_NOT_FOUND

    if use_json:
        print(json.dumps({"deleted": args.identifier}, indent=2))
    else:
        print(f"Work item '{args.identifier}' deleted.")
    return EXIT_SUCCESS


def cmd_wi_close(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return code

    from agent_notes.core.work_item_model import WorkItemModel

    try:
        wi = WorkItemModel.close_work_item(proj_id, args.identifier)
    except ValueError as exc:
        if use_json:
            print(json.dumps({"error": str(exc)}, indent=2))
        else:
            print(f"Error: {exc}")
        return EXIT_NOT_FOUND

    if use_json:
        print(json.dumps({"work_item": wi}, indent=2, default=str))
    else:
        print(f"Work item closed: **{wi['identifier']}**")
    return EXIT_SUCCESS


def cmd_wi_diagnose(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return code

    from agent_notes.core.work_item_model import WorkItemModel

    try:
        result = WorkItemModel.diagnose(proj_id, args.identifier)
    except ValueError as exc:
        if use_json:
            print(json.dumps({"error": str(exc)}, indent=2))
        else:
            print(f"Error: {exc}")
        return EXIT_NOT_FOUND

    if use_json:
        print(json.dumps(result, indent=2, default=str))
    else:
        wi = result["work_item"]
        print(f"**{wi['identifier']}** — {wi['title']}")
        print(f"Status: {wi['status']} | Kind: {wi['kind']}")
        print(f"\nOps ({len(result['ops'])}):")
        for op in result["ops"]:
            print(f"  {op['op_type']} (lamport={op['lamport']})")
        print(f"\nRecent changes ({len(result['recent_changes'])}):")
        for ch in result["recent_changes"]:
            print(f"  {ch['event']} @ {ch['changed_at']}")
    return EXIT_SUCCESS


def register_work_item_parsers(sub: argparse._SubParsersAction) -> None:
    wi = sub.add_parser("work-item", help="Work item operations (Plan 008 kernel)")
    wi_sub = wi.add_subparsers(dest="wi_cmd")

    wi_file = wi_sub.add_parser("file", help="File a work item")
    wi_file.add_argument("--title", required=True)
    wi_file.add_argument("--body", default="")
    wi_file.add_argument("--identifier", default=None)
    wi_file.add_argument("--type", default="todo", dest="type")
    wi_file.add_argument("--status", default="open")
    wi_file.add_argument("--severity", default="medium")
    wi_file.add_argument("--external-refs", default=None)
    wi_file.add_argument("--diagnostic-keys", default=None)
    _add_common(wi_file)
    wi_file.set_defaults(func=cmd_wi_file)

    wi_update = wi_sub.add_parser("update", help="Update a work item")
    wi_update.add_argument("identifier")
    wi_update.add_argument("--title", default=None)
    wi_update.add_argument("--body", default=None, help="Replace body entirely")
    wi_update.add_argument(
        "--append-body",
        default=None,
        dest="append_body",
        help="Append text to the existing body (separated by a blank line)",
    )
    wi_update.add_argument("--type", default=None, dest="type")
    wi_update.add_argument("--status", default=None)
    wi_update.add_argument("--severity", default=None)
    _add_common(wi_update)
    wi_update.set_defaults(func=cmd_wi_update)

    wi_get = wi_sub.add_parser("get", help="Get a work item")
    wi_get.add_argument("identifier")
    wi_get.add_argument("--with-body", action="store_true", help="Include the body text")
    _add_common(wi_get)
    wi_get.set_defaults(func=cmd_wi_get)

    wi_find = wi_sub.add_parser("find", help="Find work items by text or filters")
    wi_find.add_argument("--status", default=None)
    wi_find.add_argument("--type", default=None, dest="type")
    wi_find.add_argument("--text", default=None)
    wi_find.add_argument("--limit", type=int, default=None)
    wi_find.add_argument(
        "--scope",
        choices=["project", "workspace", "global"],
        default="project",
        help=(
            "Search scope. 'project' (default) uses --path/--workspace+--project. "
            "'workspace' broadens to the workspace. 'global' ignores both."
        ),
    )
    _add_common(wi_find)
    wi_find.set_defaults(func=cmd_wi_find)

    wi_query = wi_sub.add_parser("query", help="Query work items")
    wi_query.add_argument("filter", nargs="?", default=None)
    wi_query.add_argument("--limit", type=int, default=50)
    _add_common(wi_query)
    wi_query.set_defaults(func=cmd_wi_query)

    wi_ready = wi_sub.add_parser("ready", help="Show work items that are ready (not blocked)")
    wi_ready.add_argument("--limit", type=int, default=50)
    _add_common(wi_ready)
    wi_ready.set_defaults(func=cmd_wi_ready)

    wi_claimable = wi_sub.add_parser(
        "claimable", help="Show work items that are claimable (ready + not leased)"
    )
    wi_claimable.add_argument("--limit", type=int, default=50)
    _add_common(wi_claimable)
    wi_claimable.set_defaults(func=cmd_wi_claimable)

    wi_close = wi_sub.add_parser("close", help="Close a work item")
    wi_close.add_argument("identifier")
    _add_common(wi_close)
    wi_close.set_defaults(func=cmd_wi_close)

    wi_delete = wi_sub.add_parser("delete", help="Delete a work item")
    wi_delete.add_argument("identifier")
    _add_common(wi_delete)
    wi_delete.set_defaults(func=cmd_wi_delete)

    wi_diagnose = wi_sub.add_parser("diagnose", help="Diagnose a work item (ops + history)")
    wi_diagnose.add_argument("identifier")
    _add_common(wi_diagnose)
    wi_diagnose.set_defaults(func=cmd_wi_diagnose)

    wi.set_defaults(func=lambda args: (_print_sub_help(wi), EXIT_SUCCESS)[1])
