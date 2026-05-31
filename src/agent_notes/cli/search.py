from __future__ import annotations

import argparse
import json

from agent_notes.cli.common import (
    EXIT_NOT_CONFIGURED,
    EXIT_SUCCESS,
    _add_common,
    _resolve,
    report_resolution_failure,
)


def cmd_search_all(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return code

    from agent_notes.core.embed import embed
    from agent_notes.core.search import search_all_notes

    vec = embed(args.query, task="query").tolist()
    rows = search_all_notes(
        query_vec=vec,
        kinds=getattr(args, "kinds", None),
        workspace_ids=[ws_id] if ws_id else None,
        project_ids=[proj_id] if proj_id else None,
        limit=min(args.limit or 20, 100),
    )
    if use_json:
        print(json.dumps({"results": rows}, indent=2, default=str))
    else:
        if not rows:
            print("No matching notes found.")
        else:
            print(f"{len(rows)} note(s) matched:")
            for r in rows:
                print(
                    f"- [{r['kind']}] **{r['identifier']}** — {r['title']} (score={r['score']:.3f})"
                )
    return EXIT_SUCCESS


def register_search_parsers(sub: argparse._SubParsersAction) -> None:
    srch = sub.add_parser("search", help="Cross-kind search")
    srch_sub = srch.add_subparsers(dest="srch_cmd")

    srch_all = srch_sub.add_parser("all", help="Search across all kinds")
    srch_all.add_argument("query")
    srch_all.add_argument("--limit", type=int, default=20)
    _add_common(srch_all)
    srch_all.set_defaults(func=cmd_search_all)

    srch_bc = srch_sub.add_parser("breadcrumb", help="Search breadcrumbs only")
    srch_bc.add_argument("query")
    srch_bc.add_argument("--limit", type=int, default=20)
    _add_common(srch_bc)
    srch_bc.set_defaults(func=cmd_search_all, kinds=["breadcrumb"])

    srch_mem = srch_sub.add_parser("memory", help="Search memories only")
    srch_mem.add_argument("query")
    srch_mem.add_argument("--limit", type=int, default=20)
    _add_common(srch_mem)
    srch_mem.set_defaults(func=cmd_search_all, kinds=["memory"])

    srch.set_defaults(func=lambda args: (_print_sub_help(srch), EXIT_SUCCESS)[1])


def _print_sub_help(parser: argparse.ArgumentParser) -> None:
    parser.print_help()
