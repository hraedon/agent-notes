from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_notes.cli.common import (
    EXIT_CONFLICT,
    EXIT_GENERIC,
    EXIT_NOT_CONFIGURED,
    EXIT_NOT_FOUND,
    EXIT_SUCCESS,
    _add_common,
    _bc_format,
    _print_sub_help,
    _resolve,
    report_resolution_failure,
)


def cmd_bc_file(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return code

    from agent_notes.core.breadcrumbs_model import BreadcrumbModel
    from agent_notes.core.embed import embed

    vec = embed(args.title + " " + (args.body or ""), task="document").tolist()
    external_refs = json.loads(args.external_refs) if args.external_refs else None
    diagnostic_keys = json.loads(args.diagnostic_keys) if args.diagnostic_keys else None
    try:
        bc = BreadcrumbModel.file_breadcrumb(
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
        print(json.dumps({"breadcrumb": bc}, indent=2, default=str))
    else:
        ident = bc["identifier"]
        kind = bc["kind"]
        status = bc["status"]
        print(f"Breadcrumb filed: **{ident}** ({kind} / {status}) in project {proj_slug}")
    return EXIT_SUCCESS


def cmd_bc_update(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return code

    from agent_notes.core.breadcrumbs_model import BreadcrumbModel
    from agent_notes.core.embed import embed

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
        old = BreadcrumbModel.get_breadcrumb(proj_id, args.identifier)
        if old is None:
            if use_json:
                print(json.dumps({"error": "not found"}, indent=2))
            else:
                print(f"Breadcrumb '{args.identifier}' not found.")
            return EXIT_NOT_FOUND
        existing = (old.get("body") or "").rstrip()
        sep = "\n\n" if existing else ""
        fields["body"] = f"{existing}{sep}{args.append_body}"
    if args.type is not None:
        fields["kind"] = args.type
    if args.status is not None:
        fields["status"] = args.status
    if args.severity is not None:
        fields["severity"] = args.severity

    if "body" in fields or "title" in fields:
        old = BreadcrumbModel.get_breadcrumb(proj_id, args.identifier)
        if old is None:
            if use_json:
                print(json.dumps({"error": "not found"}, indent=2))
            else:
                print(f"Breadcrumb '{args.identifier}' not found.")
            return EXIT_NOT_FOUND
        text = (
            fields.get("title", old.get("title", ""))
            + " "
            + fields.get("body", old.get("body", ""))
        )
        fields["embedding"] = embed(text, task="document").tolist()

    try:
        bc = BreadcrumbModel.update_breadcrumb(
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
        print(json.dumps({"breadcrumb": bc}, indent=2, default=str))
    else:
        print(f"Breadcrumb updated: **{bc['identifier']}** ({bc['kind']} / {bc['status']})")
    return EXIT_SUCCESS


def cmd_bc_get(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return code

    from agent_notes.core.breadcrumbs_model import BreadcrumbModel

    bc = BreadcrumbModel.get_breadcrumb(proj_id, args.identifier)
    if bc is None:
        if use_json:
            print(json.dumps({"error": "not found"}, indent=2))
        else:
            print(f"Breadcrumb '{args.identifier}' not found.")
        return EXIT_NOT_FOUND

    if use_json:
        bc.pop("embedding", None)
        print(json.dumps({"breadcrumb": bc}, indent=2, default=str))
    else:
        print(_bc_format(bc))
    return EXIT_SUCCESS


def cmd_bc_find(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    scope = getattr(args, "scope", None) or "project"

    from agent_notes.core.breadcrumbs_model import BreadcrumbModel
    from agent_notes.core.db import list_workspaces
    from agent_notes.core.embed import embed

    proj_id: int | None = None
    ws_id: int | None = None

    if scope == "global":
        pass
    elif scope == "workspace":
        # Resolve workspace from --workspace or --path; project is intentionally ignored.
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
    else:  # scope == "project" (default)
        try:
            ws_id, proj_id, _ws_slug, _proj_slug = _resolve(args.workspace, args.project, args.path)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
            report_resolution_failure(args, code)
            return code

    if args.text:
        # Quick-win: exact identifier lookup (e.g., "BC-001" or "001")
        # If the text is a short identifier-like string, do a direct lookup
        # to avoid the 270MB embedding model cold-load.
        text = args.text.strip()
        if text.isdigit() or (text.startswith("BC-") and text[3:].isdigit()):
            rows = BreadcrumbModel.query_breadcrumbs(
                project_id=proj_id,
                workspace_id=ws_id,
                identifier=text,
                limit=1,
            )
            if not rows:
                # Fall back to identifier partial match
                rows = BreadcrumbModel.query_breadcrumbs(
                    project_id=proj_id,
                    workspace_id=ws_id,
                    limit=min(args.limit or 50, 200),
                )
                rows = [r for r in rows if text.lower() in r["identifier"].lower()]
        else:
            vec = embed(args.text, task="query").tolist()
            rows = BreadcrumbModel.find_breadcrumbs(
                query_vec=vec,
                project_id=proj_id,
                workspace_id=ws_id,
                limit=min(args.limit or 10, 50),
            )
    else:
        rows = BreadcrumbModel.query_breadcrumbs(
            project_id=proj_id,
            workspace_id=ws_id,
            status=args.status,
            kind=args.type,
            limit=min(args.limit or 50, 200),
        )

    if use_json:
        for r in rows:
            r.pop("embedding", None)
        print(json.dumps({"breadcrumbs": rows}, indent=2, default=str))
    else:
        if not rows:
            print("No breadcrumbs found.")
        else:
            print(f"{len(rows)} breadcrumb(s) found:")
            for r in rows:
                print(f"- **{r['identifier']}** ({r['kind']} / {r['status']}) — {r['title']}")
    return EXIT_SUCCESS


def cmd_bc_query(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return code

    from agent_notes.core.breadcrumbs_model import BreadcrumbModel

    rows = BreadcrumbModel.query_breadcrumbs(
        project_id=proj_id,
        workspace_id=ws_id,
        limit=min(args.limit or 50, 200),
    )
    if use_json:
        print(json.dumps({"breadcrumbs": rows}, indent=2, default=str))
    else:
        if not rows:
            print("No breadcrumbs found.")
        else:
            for r in rows:
                print(f"- {r['identifier']} | {r['title']} | {r['kind']} | {r['status']}")
    return EXIT_SUCCESS


def cmd_bc_delete(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return code

    from agent_notes.core.breadcrumbs_model import BreadcrumbModel

    deleted = BreadcrumbModel.delete_breadcrumb(proj_id, args.identifier)
    if not deleted:
        if use_json:
            print(json.dumps({"error": "not found"}, indent=2))
        else:
            print(f"Breadcrumb '{args.identifier}' not found.")
        return EXIT_NOT_FOUND

    if use_json:
        print(json.dumps({"deleted": args.identifier}, indent=2))
    else:
        print(f"Breadcrumb '{args.identifier}' deleted.")
    return EXIT_SUCCESS


def cmd_bc_sync(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return code

    from agent_notes.core.bc_files import sync_breadcrumbs_from_dir
    from agent_notes.core.embed import embed

    directory = args.from_files or (Path(args.path) / "breadcrumbs" if args.path else None)
    if not directory or not Path(directory).is_dir():
        msg = f"breadcrumb directory not found: {directory!r} (pass --from-files)"
        if use_json:
            print(json.dumps({"error": msg}, indent=2))
        else:
            print(f"Error: {msg}", file=sys.stderr)
        return EXIT_GENERIC

    summary = sync_breadcrumbs_from_dir(
        proj_id,
        directory,
        lambda text: embed(text, task="document").tolist(),
        create_missing_vocab=args.create_missing_vocab,
        prune=args.prune,
    )
    if use_json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        print(
            f"Synced into {proj_slug}: {len(summary['imported'])} imported, "
            f"{len(summary['skipped'])} skipped, {len(summary['errors'])} errors, "
            f"{len(summary['pruned'])} pruned."
        )
        for ns, vals in summary["missing_vocab"].items():
            print(f"  missing {ns}: {', '.join(vals)} (re-run with --create-missing-vocab)")
        for err in summary["errors"]:
            print(f"  error {err['identifier']}: {err['error']}")
    return EXIT_CONFLICT if (summary["errors"] or summary["missing_vocab"]) else EXIT_SUCCESS


def cmd_bc_export_index(args: argparse.Namespace) -> int:
    """Write a plain-text fallback index of open breadcrumbs to the repo root.

    This is the offline fallback: if the agent-notes CLI/DB is broken, an agent
    can still read OPEN_BREADCRUMBS.txt to see what's open.
    """
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return code

    from agent_notes.core.breadcrumbs_model import BreadcrumbModel
    from agent_notes.core.db import list_projects

    # Determine output path
    if args.output:
        out_path = Path(args.output)
    else:
        # Find the project's repo_root
        proj = next((p for p in list_projects(workspace_id=ws_id) if p.id == proj_id), None)
        rr = proj.repo_root if proj else None
        out_path = Path(rr or ".") / "OPEN_BREADCRUMBS.txt"

    open_bcs = BreadcrumbModel.query_breadcrumbs(project_id=proj_id, is_open=True, limit=200)
    # Sort by severity then recency
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    open_bcs.sort(
        key=lambda b: (
            severity_order.get(b.get("severity", "medium"), 2),
            b.get("updated_at", ""),
        )
    )

    lines = [
        f"# Open Breadcrumbs for {proj_slug}",
        f"# Generated: {datetime.now().isoformat()}",
        f"# Total: {len(open_bcs)}",
        "#",
        "# This file is a plain-text fallback. If the agent-notes CLI is unavailable,",
        "# agents can read this file to see open breadcrumbs.",
        "# Do not edit by hand; regenerate with: agent-notes breadcrumb export-index",
        "",
    ]

    for bc in open_bcs:
        ident = bc.get("identifier", "?")
        kind = bc.get("kind", "?")
        status = bc.get("status", "?")
        severity = bc.get("severity", "medium")
        title = bc.get("title", "(no title)")
        lines.append(f"[{severity}] {ident} ({kind} / {status}) — {title}")

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if use_json:
        print(json.dumps({"path": str(out_path), "count": len(open_bcs)}, indent=2))
    else:
        print(f"Exported {len(open_bcs)} open breadcrumb(s) to {out_path}")
    return EXIT_SUCCESS


def register_breadcrumb_parsers(sub: argparse._SubParsersAction) -> None:
    bc = sub.add_parser("breadcrumb", help="Breadcrumb operations")
    bc_sub = bc.add_subparsers(dest="bc_cmd")

    bc_file = bc_sub.add_parser("file", help="File a breadcrumb")
    bc_file.add_argument("--title", required=True)
    bc_file.add_argument("--body", default="")
    bc_file.add_argument("--identifier", default=None)
    bc_file.add_argument("--type", default="todo", dest="type")
    bc_file.add_argument("--status", default="new")
    bc_file.add_argument("--severity", default="medium")
    bc_file.add_argument("--external-refs", default=None)
    bc_file.add_argument("--diagnostic-keys", default=None)
    _add_common(bc_file)
    bc_file.set_defaults(func=cmd_bc_file)

    bc_update = bc_sub.add_parser("update", help="Update a breadcrumb")
    bc_update.add_argument("identifier")
    bc_update.add_argument("--title", default=None)
    bc_update.add_argument("--body", default=None, help="Replace body entirely")
    bc_update.add_argument(
        "--append-body",
        default=None,
        dest="append_body",
        help="Append text to the existing body (separated by a blank line)",
    )
    bc_update.add_argument("--type", default=None, dest="type")
    bc_update.add_argument("--status", default=None)
    bc_update.add_argument("--severity", default=None)
    _add_common(bc_update)
    bc_update.set_defaults(func=cmd_bc_update)

    bc_get = bc_sub.add_parser("get", help="Get a breadcrumb")
    bc_get.add_argument("identifier")
    _add_common(bc_get)
    bc_get.set_defaults(func=cmd_bc_get)

    bc_find = bc_sub.add_parser("find", help="Find breadcrumbs by text or filters")
    bc_find.add_argument("--status", default=None)
    bc_find.add_argument("--type", default=None, dest="type")
    bc_find.add_argument("--text", default=None)
    bc_find.add_argument("--limit", type=int, default=None)
    bc_find.add_argument(
        "--scope",
        choices=["project", "workspace", "global"],
        default="project",
        help=(
            "Search scope. 'project' (default) uses --path/--workspace+--project. "
            "'workspace' broadens to the workspace. 'global' ignores both."
        ),
    )
    _add_common(bc_find)
    bc_find.set_defaults(func=cmd_bc_find)

    bc_query = bc_sub.add_parser("query", help="Query breadcrumbs")
    bc_query.add_argument("filter", nargs="?", default=None)
    bc_query.add_argument("--limit", type=int, default=50)
    _add_common(bc_query)
    bc_query.set_defaults(func=cmd_bc_query)

    bc_delete = bc_sub.add_parser("delete", help="Delete a breadcrumb")
    bc_delete.add_argument("identifier")
    _add_common(bc_delete)
    bc_delete.set_defaults(func=cmd_bc_delete)

    bc_sync = bc_sub.add_parser(
        "sync", help="Import breadcrumbs from on-disk markdown files into the DB"
    )
    bc_sync.add_argument(
        "--from-files",
        dest="from_files",
        default=None,
        help="Directory of breadcrumb .md files (defaults to <path>/breadcrumbs)",
    )
    bc_sync.add_argument(
        "--create-missing-vocab",
        dest="create_missing_vocab",
        action="store_true",
        help="Add kind/status/severity values found in files but absent from vocab",
    )
    bc_sync.add_argument(
        "--prune",
        action="store_true",
        help="Delete DB breadcrumbs not present in files (hard delete, destructive)",
    )
    _add_common(bc_sync)
    bc_sync.set_defaults(func=cmd_bc_sync)

    bc_export_index = bc_sub.add_parser(
        "export-index",
        help="Export open breadcrumbs to a plain-text fallback index in the repo root",
    )
    bc_export_index.add_argument(
        "--output",
        default=None,
        help="Output path (default: <repo-root>/OPEN_BREADCRUMBS.txt)",
    )
    _add_common(bc_export_index)
    bc_export_index.set_defaults(func=cmd_bc_export_index)

    bc.set_defaults(func=lambda args: (_print_sub_help(bc), EXIT_SUCCESS)[1])
