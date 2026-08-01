from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from agent_notes.cli.common import (
    EXIT_NOT_CONFIGURED,
    EXIT_SUCCESS,
    _add_common,
    _print_sub_help,
    _resolve,
    report_resolution_failure,
)


def _run_exact_search(
    args: argparse.Namespace,
    ws_id: int,
    proj_id: int,
) -> list[dict[str, Any]]:
    """Run the existing pgvector search and return rows."""
    from agent_notes.core.embed import embed
    from agent_notes.core.search import search_all_notes

    vec = embed(args.query, task="query").tolist()
    return search_all_notes(
        query_vec=vec,
        kinds=getattr(args, "kinds", None),
        workspace_ids=[ws_id] if ws_id else None,
        project_ids=[proj_id] if proj_id else None,
        limit=min(args.limit or 20, 100),
    )


def _run_learned_recall(
    query: str,
    proj_slug: str,
    ws_slug: str,
) -> tuple[list[dict[str, Any]], str | None, list[str]]:
    """Query the configured memory engine for learned results.

    Returns (learned_results, engine_name, warnings).
    Honest degradation: never raises — on any failure returns ([], None, [msg]).
    """
    learned: list[dict[str, Any]] = []
    warnings: list[str] = []

    from agent_notes.core.memory_engine import (
        MemoryScope,
        RecallQuery,
        get_engine,
    )

    try:
        engine = get_engine()
    except ValueError:
        warnings.append("learned: skipped (unknown engine configuration)")
        return learned, None, warnings
    except Exception as exc:
        warnings.append(f"learned: failed — {type(exc).__name__}")
        return learned, None, warnings

    try:
        rq = RecallQuery(
            query=query,
            scope=MemoryScope(
                project_slug=proj_slug,
                workspace_slug=ws_slug,
            ),
            budget="mid",
            max_tokens=4096,
        )
        response = engine.recall(rq)
    except Exception as exc:
        warnings.append(f"learned: failed — {type(exc).__name__}")
        return learned, engine.engine_name, warnings

    if response.usage.get("error"):
        warnings.append(f"learned: degraded — {response.usage['error']}")
        return learned, response.engine, warnings

    if not response.results:
        warnings.append("learned: no learned context found")
        return learned, response.engine, warnings

    for r in response.results:
        learned.append(
            {
                "source": "learned",
                "text": r.text,
                "origin": r.origin.value,
                "score": r.score,
                "source_ref": r.source_ref,
                "memory_type": r.memory_type,
                "engine": response.engine,
            }
        )
    return learned, response.engine, warnings


def cmd_search_all(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return code

    rows = _run_exact_search(args, ws_id, proj_id)

    federated = getattr(args, "federated", False)

    if not federated:
        if use_json:
            print(json.dumps({"results": rows}, indent=2, default=str))
        else:
            if not rows:
                print("No matching notes found.")
            else:
                print(f"{len(rows)} note(s) matched:")
                for r in rows:
                    score = r['score']
                    print(
                        f"- [{r['kind']}] **{r['identifier']}** — {r['title']}"
                        f" (score={score:.3f})"
                    )
        return EXIT_SUCCESS

    exact: list[dict[str, Any]] = [{**r, "source": "exact"} for r in rows]

    learned, engine_name, warnings = _run_learned_recall(
        args.query, proj_slug, ws_slug
    )

    for w in warnings:
        print(w, file=sys.stderr)

    if use_json:
        payload: dict[str, Any] = {
            "results": exact + learned,
            "exact_count": len(exact),
            "learned_count": len(learned),
        }
        if engine_name:
            payload["engine"] = engine_name
        print(json.dumps(payload, indent=2, default=str))
    else:
        if not exact and not learned:
            print("No matching notes found.")
        else:
            print(f"{len(exact)} exact + {len(learned)} learned results:")
            for r in exact:
                score = r["score"]
                print(
                    f"- [exact] [{r['kind']}] **{r['identifier']}**"
                    f" — {r['title']} (score={score:.3f})"
                )
            for r in learned:
                print(
                    f"- [learned:{r['origin']}] {r['text']} (score={r['score']:.3f})"
                )
    return EXIT_SUCCESS


def register_search_parsers(sub: argparse._SubParsersAction) -> None:
    srch = sub.add_parser("search", help="Cross-kind search")
    srch_sub = srch.add_subparsers(dest="srch_cmd")

    srch_all = srch_sub.add_parser("all", help="Search across all kinds")
    srch_all.add_argument("query")
    srch_all.add_argument("--limit", type=int, default=20)
    srch_all.add_argument(
        "--federated",
        action="store_true",
        help="Also query the configured memory engine for learned results (Plan 020 WI-1.2). "
        "Off by default.",
    )
    _add_common(srch_all)
    srch_all.set_defaults(func=cmd_search_all)

    srch_bc = srch_sub.add_parser("breadcrumb", help="Search breadcrumbs only")
    srch_bc.add_argument("query")
    srch_bc.add_argument("--limit", type=int, default=20)
    srch_bc.add_argument(
        "--federated",
        action="store_true",
        help="Also query the configured memory engine for learned results (Plan 020 WI-1.2). "
        "Off by default.",
    )
    _add_common(srch_bc)
    srch_bc.set_defaults(func=cmd_search_all, kinds=["breadcrumb"])

    srch_mem = srch_sub.add_parser("memory", help="Search memories only")
    srch_mem.add_argument("query")
    srch_mem.add_argument("--limit", type=int, default=20)
    srch_mem.add_argument(
        "--federated",
        action="store_true",
        help="Also query the configured memory engine for learned results (Plan 020 WI-1.2). "
        "Off by default.",
    )
    _add_common(srch_mem)
    srch_mem.set_defaults(func=cmd_search_all, kinds=["memory"])

    srch.set_defaults(func=lambda args: _print_sub_help(srch))
