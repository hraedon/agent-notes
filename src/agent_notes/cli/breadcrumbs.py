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
    _bc_format,
    _resolve,
)


def cmd_bc_file(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED  # type: ignore[arg-type]

    from agent_notes.core.breadcrumbs_model import BreadcrumbModel
    from agent_notes.core.embed import embed

    vec = embed(args.title + " " + (args.body or ""), task="document").tolist()
    try:
        bc = BreadcrumbModel.file_breadcrumb(
            project_id=proj_id,
            identifier=args.identifier,
            title=args.title,
            body=args.body or "",
            kind=args.kind,
            status=args.status,
            severity=args.severity or "medium",
            external_refs=args.external_refs,
            diagnostic_keys=args.diagnostic_keys,
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
        return exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED  # type: ignore[arg-type]

    from agent_notes.core.breadcrumbs_model import BreadcrumbModel
    from agent_notes.core.embed import embed

    fields: dict[str, Any] = {}
    if args.title is not None:
        fields["title"] = args.title
    if args.body is not None:
        fields["body"] = args.body
    if args.kind is not None:
        fields["kind"] = args.kind
    if args.status is not None:
        fields["status"] = args.status
    if args.severity is not None:
        fields["severity"] = args.severity

    if "body" in fields or "title" in fields:
        old = BreadcrumbModel.get_breadcrumb(proj_id, args.identifier)
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
        return exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED  # type: ignore[arg-type]

    from agent_notes.core.breadcrumbs_model import BreadcrumbModel

    bc = BreadcrumbModel.get_breadcrumb(proj_id, args.identifier)
    if bc is None:
        if use_json:
            print(json.dumps({"error": "not found"}, indent=2))
        else:
            print(f"Breadcrumb '{args.identifier}' not found.")
        return EXIT_NOT_FOUND

    if use_json:
        print(json.dumps({"breadcrumb": bc}, indent=2, default=str))
    else:
        print(_bc_format(bc))
    return EXIT_SUCCESS


def cmd_bc_find(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED  # type: ignore[arg-type]

    from agent_notes.core.breadcrumbs_model import BreadcrumbModel
    from agent_notes.core.embed import embed

    if args.text:
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
        return exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED  # type: ignore[arg-type]

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


def register_breadcrumb_parsers(sub: argparse._SubParsersAction) -> None:
    bc = sub.add_parser("breadcrumb", help="Breadcrumb operations")
    bc_sub = bc.add_subparsers(dest="bc_cmd")

    bc_file = bc_sub.add_parser("file", help="File a breadcrumb")
    bc_file.add_argument("--title", required=True)
    bc_file.add_argument("--body", default="")
    bc_file.add_argument("--identifier", default=None)
    bc_file.add_argument("--kind", default="todo")
    bc_file.add_argument("--status", default="new")
    bc_file.add_argument("--severity", default="medium")
    bc_file.add_argument("--external-refs", default=None)
    bc_file.add_argument("--diagnostic-keys", default=None)
    _add_common(bc_file)
    bc_file.set_defaults(func=cmd_bc_file)

    bc_update = bc_sub.add_parser("update", help="Update a breadcrumb")
    bc_update.add_argument("identifier")
    bc_update.add_argument("--title", default=None)
    bc_update.add_argument("--body", default=None)
    bc_update.add_argument("--kind", default=None)
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
    _add_common(bc_find)
    bc_find.set_defaults(func=cmd_bc_find)

    bc_query = bc_sub.add_parser("query", help="Query breadcrumbs")
    bc_query.add_argument("filter", nargs="?", default=None)
    bc_query.add_argument("--limit", type=int, default=50)
    _add_common(bc_query)
    bc_query.set_defaults(func=cmd_bc_query)

    bc.set_defaults(func=lambda args: (_print_sub_help(bc), EXIT_SUCCESS)[1])


def _print_sub_help(parser: argparse.ArgumentParser) -> None:
    parser.print_help()
