from __future__ import annotations

import argparse
import json

from agent_notes.cli.common import (
    EXIT_GENERIC,
    EXIT_NOT_FOUND,
    EXIT_SUCCESS,
    _print_sub_help,
    emit_error,
)


def _emit_ref_error(exc: Exception, use_json: bool) -> int:
    """Report a malformed/unresolvable link ref as a clean structured error
    instead of letting the ValueError surface as an uncaught traceback."""
    return emit_error(
        "OPERATION_FAILED",
        str(exc),
        use_json=use_json,
        exit_code=EXIT_GENERIC,
    )


def _parse_link_ref(ref: str) -> tuple[str, str, str, str]:
    parts = ref.split(":", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid link ref: {ref!r}; expected kind:workspace/project/identifier")
    kind = parts[0]
    path = parts[1]
    path_parts = path.split("/")
    if len(path_parts) != 3:
        raise ValueError(f"Invalid link path: {path!r}; expected workspace/project/identifier")
    return kind, path_parts[0], path_parts[1], path_parts[2]


def _resolve_link_ref(
    kind: str, workspace_slug: str, project_slug: str, identifier: str
) -> tuple[int, int, str]:
    from agent_notes.core.db import list_projects, list_workspaces

    ws = next((w for w in list_workspaces() if w.slug == workspace_slug), None)
    if ws is None:
        available = [w.slug for w in list_workspaces()]
        hint = f" Available: {', '.join(available)}" if available else ""
        suggestion = " Use --path for auto-resolution or run 'agent-notes workspace list'."
        raise ValueError(f"workspace '{workspace_slug}' not found.{hint}{suggestion}")
    proj = next((p for p in list_projects(workspace_id=ws.id) if p.slug == project_slug), None)
    if proj is None:
        raise ValueError(f"project '{project_slug}' not found")
    return ws.id, proj.id, identifier


def cmd_link_add(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    try:
        fk, fwslug, fproj, fid = _parse_link_ref(args.from_)
        tk, twslug, tproj, tid = _parse_link_ref(args.to)
        from_ws, from_proj, from_id = _resolve_link_ref(fk, fwslug, fproj, fid)
        to_ws, to_proj, to_id = _resolve_link_ref(tk, twslug, tproj, tid)
    except ValueError as exc:
        return _emit_ref_error(exc, use_json)

    from agent_notes.core.links import add_link

    add_link(
        from_kind=fk,
        from_workspace=from_ws,
        from_project=from_proj,
        from_identifier=from_id,
        to_kind=tk,
        to_workspace=to_ws,
        to_project=to_proj,
        to_identifier=to_id,
        relationship=args.rel_type,
    )
    print(f"Link added: {fk}/{from_id} --{args.rel_type}--> {tk}/{to_id}")
    return EXIT_SUCCESS


def cmd_link_remove(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    try:
        fk, fwslug, fproj, fid = _parse_link_ref(args.from_)
        tk, twslug, tproj, tid = _parse_link_ref(args.to)
        from_ws, from_proj, from_id = _resolve_link_ref(fk, fwslug, fproj, fid)
        to_ws, to_proj, to_id = _resolve_link_ref(tk, twslug, tproj, tid)
    except ValueError as exc:
        return _emit_ref_error(exc, use_json)

    from agent_notes.core.links import remove_link

    removed = remove_link(
        from_kind=fk,
        from_workspace=from_ws,
        from_project=from_proj,
        from_identifier=from_id,
        to_kind=tk,
        to_workspace=to_ws,
        to_project=to_proj,
        to_identifier=to_id,
        relationship=args.rel_type,
    )
    if removed:
        print(f"Link removed: {fk}/{from_id} --{args.rel_type}--> {tk}/{to_id}")
    else:
        print("No such link found.")
        return EXIT_NOT_FOUND
    return EXIT_SUCCESS


def cmd_link_trace(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    try:
        kind, ws_slug, proj_slug, identifier = _parse_link_ref(args.start)
        ws, proj, _ = _resolve_link_ref(kind, ws_slug, proj_slug, identifier)
    except ValueError as exc:
        return _emit_ref_error(exc, use_json)

    if args.all:
        from agent_notes.core.search import trace_graph_all

        nodes = trace_graph_all(
            kind=kind,
            workspace=ws,
            project=proj,
            identifier=identifier,
            direction=args.direction,
            max_depth=min(args.depth or 3, 10),
        )
    else:
        from agent_notes.core.links import trace_graph as core_local_trace_graph

        nodes = core_local_trace_graph(
            kind=kind,
            workspace=ws,
            project=proj,
            identifier=identifier,
            direction=args.direction,
            max_depth=min(args.depth or 3, 10),
        )

    if use_json:
        print(
            json.dumps(
                {
                    "nodes": [
                        {
                            "kind": n.kind,
                            "identifier": n.identifier,
                            "relationship": n.relationship,
                            "depth": n.depth,
                            "title": n.title,
                            "status": n.status,
                        }
                        for n in nodes
                    ]
                },
                indent=2,
                default=str,
            )
        )
    else:
        if not nodes:
            print(f"No linked notes found for {kind}/{identifier}.")
        else:
            print(f"{len(nodes)} linked note(s):")
            for n in nodes:
                print(
                    f"- [{n.kind}] {n.identifier} (relationship={n.relationship}, depth={n.depth})"
                )
    return EXIT_SUCCESS


def register_link_parsers(sub: argparse._SubParsersAction) -> None:
    lnk = sub.add_parser("link", help="Link operations")
    lnk_sub = lnk.add_subparsers(dest="lnk_cmd")

    lnk_add = lnk_sub.add_parser("add", help="Add a typed link")
    lnk_add.add_argument("--from", required=True, dest="from_")
    lnk_add.add_argument("--to", required=True)
    lnk_add.add_argument("--type", required=True, dest="rel_type")
    lnk_add.set_defaults(func=cmd_link_add)

    lnk_remove = lnk_sub.add_parser("remove", help="Remove a typed link")
    lnk_remove.add_argument("--from", required=True, dest="from_")
    lnk_remove.add_argument("--to", required=True)
    lnk_remove.add_argument("--type", required=True, dest="rel_type")
    lnk_remove.set_defaults(func=cmd_link_remove)

    lnk_trace = lnk_sub.add_parser("trace", help="Trace links from a node")
    lnk_trace.add_argument("start")
    lnk_trace.add_argument("--all", action="store_true", help="Cross-kind traversal")
    lnk_trace.add_argument("--direction", default="dependencies")
    lnk_trace.add_argument("--depth", type=int, default=3)
    lnk_trace.add_argument("--json", action="store_true")
    lnk_trace.set_defaults(func=cmd_link_trace)

    lnk.set_defaults(func=lambda args: _print_sub_help(lnk))
