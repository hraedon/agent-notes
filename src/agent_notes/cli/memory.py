from __future__ import annotations

import argparse
import json
from typing import Any

from agent_notes.cli.common import (
    EXIT_CONFLICT,
    EXIT_GENERIC,
    EXIT_NOT_CONFIGURED,
    EXIT_NOT_FOUND,
    EXIT_SUCCESS,
    MemoryTarget,
    ScopeError,
    _add_common,
    _print_sub_help,
    _resolve,
    add_scope_args,
    emit_error,
    report_resolution_failure,
    resolve_composed_scopes,
    resolve_memory_target,
)
from agent_notes.core.note_model import NoteProjectionError

_RECOVERY_HINT = (
    "Run 'agent-notes memory rebuild-from-regista --project {proj_slug}' "
    "to re-derive the local projection from the regista authority."
)


def _target(args: argparse.Namespace, *, create: bool = False) -> MemoryTarget | int:
    """Resolve the memory scope, or report the failure and return its exit code."""
    use_json = getattr(args, "json", False)
    try:
        return resolve_memory_target(args, create=create)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return code
    except ScopeError as exc:
        return emit_error(exc.code, str(exc), use_json=use_json, exit_code=exc.exit_code)


def _tag(row: dict, target: MemoryTarget) -> dict:
    return {**row, "scope": target.scope, "scope_key": target.scope_key}


def _scope_label(row: dict) -> str:
    return f"{row['scope']}:{row['scope_key']}"


def cmd_mem_add(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    if not args.body or not args.body.strip():
        return emit_error(
            "VALIDATION_FAILED",
            "--body must be a non-empty string",
            use_json=use_json,
            exit_code=EXIT_CONFLICT,
        )

    target = _target(args, create=True)
    if isinstance(target, int):
        return target
    ws_id, proj_id, proj_slug = target.ws_id, target.proj_id, target.proj_slug

    from agent_notes.core.embed import embed
    from agent_notes.core.memory_model import add_memory

    vec = embed(args.body, task="document").tolist()
    attributes = json.loads(args.attributes) if args.attributes else {}
    try:
        mem = add_memory(
            workspace_id=ws_id,
            project_id=proj_id,
            name=args.name,
            memory_type=args.type,
            body=args.body,
            attributes=attributes,
            embedding=vec,
        )
    except NoteProjectionError as exc:
        # The regista event committed; the local projection failed. Report
        # this as a partial-commit error with a recovery hint — never as a
        # clean success. The note IS in regista and recoverable.
        return emit_error(
            "PROJECTION_FAILED",
            str(exc),
            use_json=use_json,
            detail=(
                f"The note event is committed in regista "
                f"(entity_id={exc.regista_note_id}). " + _RECOVERY_HINT.format(proj_slug=proj_slug)
            ),
            exit_code=EXIT_GENERIC,
        )
    except ValueError as exc:
        return emit_error(
            "VALIDATION_FAILED",
            str(exc),
            use_json=use_json,
            exit_code=EXIT_CONFLICT,
        )

    mem = _tag(mem, target)
    if use_json:
        print(json.dumps({"memory": mem}, indent=2, default=str))
    else:
        old_id = mem.get("supersedes")
        superseded_msg = f" (superseded id={old_id})" if old_id else ""
        mem_name = mem["name"]
        mem_id = mem["id"]
        mem_type = mem["memory_type"]
        where = "" if target.scope == "repo" else f" in {_scope_label(mem)}"
        print(f"Memory '{mem_name}' added{where} (id={mem_id}, type={mem_type}){superseded_msg}")
    return EXIT_SUCCESS


def cmd_mem_get(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    target = _target(args)
    if isinstance(target, int):
        return target

    from agent_notes.core.memory_model import get_memory

    mem = get_memory(target.ws_id, target.proj_id, args.name) if target.proj_id else None
    if mem is None:
        where = "" if target.scope == "repo" else f" in {target.scope}:{target.scope_key}"
        return emit_error(
            "NOT_FOUND",
            f"Memory '{args.name}' not found{where}.",
            use_json=use_json,
            exit_code=EXIT_NOT_FOUND,
        )

    mem = _tag(mem, target)
    if use_json:
        print(json.dumps({"memory": mem}, indent=2, default=str))
    else:
        lines = [
            f"**{mem['name']}** (type={mem['memory_type']}, id={mem['id']})",
            f"Created: {mem['created_at']}",
            f"Updated: {mem['updated_at']}",
            "",
            mem["body"],
        ]
        print("\n".join(lines))
    return EXIT_SUCCESS


def _composed_target(
    args: argparse.Namespace,
) -> tuple[Any, list, list[str]] | int:
    """Composed-scope resolution with the shared error reporting."""
    try:
        return resolve_composed_scopes(args)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return code
    except ScopeError as exc:
        return emit_error(
            exc.code, str(exc), use_json=getattr(args, "json", False), exit_code=exc.exit_code
        )


def _scopes_payload(refs: list) -> list[dict]:
    return [{"scope": r.scope, "scope_key": r.scope_key} for r in refs]


def _print_warnings(warnings: list[str]) -> None:
    import sys

    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)


def _print_shadows(row: dict) -> None:
    for sh in row.get("shadows") or []:
        preview = (sh.get("body_preview") or "").replace("\n", " ")
        print(f"  shadows {sh['scope']}:{sh['scope_key']}: {preview}")


def cmd_mem_list(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    limit = min(args.limit or 50, 200)
    scope = getattr(args, "scope", "repo")

    from agent_notes.core.memory_model import list_memories, list_memories_composed

    extra: dict[str, Any] = {}
    if scope == "composed":
        resolved = _composed_target(args)
        if isinstance(resolved, int):
            return resolved
        ctx, refs, warnings = resolved
        rows = list_memories_composed(ctx.ws_id, refs, memory_type=args.type, limit=limit)
        extra["scopes"] = _scopes_payload(refs)
        if warnings:
            extra["warnings"] = warnings
            if not use_json:
                _print_warnings(warnings)
    else:
        target = _target(args)
        if isinstance(target, int):
            return target
        rows = (
            list_memories(
                workspace_id=target.ws_id,
                project_id=target.proj_id,
                memory_type=args.type,
                limit=limit,
            )
            if target.proj_id
            else []
        )
        rows = [_tag(r, target) for r in rows]

    if use_json:
        print(json.dumps({"memories": rows, **extra}, indent=2, default=str))
    else:
        if not rows:
            print("No memories found.")
        else:
            print(f"{len(rows)} memory(ies):")
            for r in rows:
                preview = (r.get("body_preview") or "").replace("\n", " ")[:80]
                tag = "" if scope == "repo" else f" [{_scope_label(r)}]"
                print(f"- **{r['name']}**{tag} (type={r['memory_type']}) {preview}")
                _print_shadows(r)
    return EXIT_SUCCESS


def cmd_mem_search(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    limit = min(args.limit or 10, 50)
    scope = getattr(args, "scope", "composed")

    from agent_notes.core.embed import embed
    from agent_notes.core.memory_model import search_memory_composed, search_memory_with_body

    extra: dict[str, Any] = {}
    if scope == "composed":
        resolved = _composed_target(args)
        if isinstance(resolved, int):
            return resolved
        ctx, refs, warnings = resolved
        vec = embed(args.query, task="query").tolist()
        rows = search_memory_composed(ctx.ws_id, vec, refs, limit=limit)
        extra["scopes"] = _scopes_payload(refs)
        if warnings:
            extra["warnings"] = warnings
            if not use_json:
                _print_warnings(warnings)
    else:
        target = _target(args)
        if isinstance(target, int):
            return target
        vec = embed(args.query, task="query").tolist()
        rows = (
            search_memory_with_body(
                workspace_id=target.ws_id,
                query_vec=vec,
                project_id=target.proj_id,
                limit=limit,
            )
            if target.proj_id
            else []
        )
        rows = [_tag(r, target) for r in rows]

    if use_json:
        print(json.dumps({"memories": rows, **extra}, indent=2, default=str))
    else:
        if not rows:
            print("No matching memories.")
        else:
            print(f"{len(rows)} memory(ies) matched:")
            for r in rows:
                tag = "" if scope == "repo" else f" [{_scope_label(r)}]"
                print(f"- **{r['name']}**{tag} (type={r['memory_type']}, score={r['score']:.3f})")
                if r.get("body"):
                    print(f"  {r['body'][:200]}")
                _print_shadows(r)
    return EXIT_SUCCESS


def cmd_mem_recall(args: argparse.Namespace) -> int:
    """Recall from the configured memory engine across one or all memory tiers."""
    use_json = getattr(args, "json", False)
    scope = getattr(args, "scope", "composed")

    from agent_notes.core.db import vm_project_slug
    from agent_notes.core.memory_engine import MemoryScope, RecallQuery, get_engine

    # (scope, scope_key, engine project slug), most specific first.
    tiers: list[tuple[str, str, str]] = []
    warnings: list[str] = []
    refs: list = []
    if scope == "composed":
        resolved = _composed_target(args)
        if isinstance(resolved, int):
            return resolved
        ctx, refs, warnings = resolved
        ws_slug = ctx.ws_slug
        for r in refs:
            slug = vm_project_slug(r.scope_key) if r.scope == "vm" else r.scope_key
            tiers.append((r.scope, r.scope_key, slug))
    else:
        target = _target(args)
        if isinstance(target, int):
            return target
        ws_slug = target.ws_slug
        if target.proj_id:
            tiers.append((target.scope, target.scope_key, target.proj_slug))

    try:
        engine = get_engine()
    except ValueError as exc:
        return emit_error("ENGINE_UNAVAILABLE", str(exc), use_json=use_json, exit_code=EXIT_GENERIC)

    limit = min(args.limit or 10, 50)
    merged: list[dict] = []
    for rank, (tier, key, slug) in enumerate(tiers):
        try:
            response = engine.recall(
                RecallQuery(
                    query=args.query,
                    scope=MemoryScope(project_slug=slug, workspace_slug=ws_slug),
                    budget=args.budget,
                    max_tokens=4096,
                )
            )
        except Exception as exc:
            warnings.append(f"{tier}:{key} recall failed: {type(exc).__name__}")
            continue
        if response.usage.get("error"):
            warnings.append(f"{tier}:{key} recall degraded: {response.usage['error']}")
            continue
        for res in response.results:
            merged.append({**res.to_dict(), "scope": tier, "scope_key": key, "_rank": rank})

    # A more specific tier's memory can fall below its own recall cutoff while
    # a less specific one with the same name is returned: look the names up
    # exactly and let it take that name's place (and score).
    if refs:
        from agent_notes.core.memory_model import active_by_name

        best: dict[str, dict] = {}
        for r in merged:
            ref = r.get("source_ref")
            if ref and (ref not in best or r["score"] > best[ref]["score"]):
                best[ref] = r
        returned = {(r.get("source_ref"), r["scope"], r["scope_key"]) for r in merged}
        rank_of = {(t, k): i for i, (t, k, _) in enumerate(tiers)}
        for row in active_by_name(ctx.ws_id, refs, list(best)):
            name, rank = row["name"], rank_of[(row["scope"], row["scope_key"])]
            top = best[name]
            if rank < top["_rank"] and (name, row["scope"], row["scope_key"]) not in returned:
                merged.append(
                    {
                        **top,
                        "text": row["body"],
                        "memory_type": row["memory_type"],
                        "scope": row["scope"],
                        "scope_key": row["scope_key"],
                        "_rank": rank,
                    }
                )
                returned.add((name, row["scope"], row["scope_key"]))

    # Rank by score with no tier boost, then let the most specific tier win a
    # name collision (source_ref is the memory name for exact-source results).
    merged.sort(key=lambda r: r["score"], reverse=True)
    winners: dict[str, dict] = {}
    for r in merged:
        ref = r.get("source_ref")
        if ref and (ref not in winners or r["_rank"] < winners[ref]["_rank"]):
            winners[ref] = r
    results: list[dict] = []
    for r in merged:
        ref = r.get("source_ref")
        if ref and winners[ref] is not r:
            continue
        r["shadows"] = [
            {
                "scope": o["scope"],
                "scope_key": o["scope_key"],
                "body_preview": (o["text"] or "")[:120],
            }
            for o in merged
            if ref and o is not r and o.get("source_ref") == ref
        ]
        results.append(r)
    for r in results:
        r.pop("_rank", None)
    results = results[:limit]

    if use_json:
        payload: dict[str, Any] = {
            "engine": engine.engine_name,
            "results": results,
            "scopes": [{"scope": t, "scope_key": k} for t, k, _ in tiers],
        }
        if warnings:
            payload["warnings"] = warnings
        print(json.dumps(payload, indent=2, default=str))
    else:
        _print_warnings(warnings)
        if not results:
            print("No recalled memories.")
        else:
            print(f"{len(results)} recalled ({engine.engine_name}):")
            for r in results:
                name = r.get("source_ref") or "(synthesized)"
                print(
                    f"- **{name}** [{_scope_label(r)}] "
                    f"({r['origin']}, score={r['score']:.3f}) {r['text'][:200]}"
                )
                _print_shadows(r)
    return EXIT_SUCCESS


def cmd_mem_delete(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    target = _target(args)
    if isinstance(target, int):
        return target

    from agent_notes.core.memory_model import delete_memory

    try:
        row = delete_memory(target.ws_id, target.proj_id, args.name) if target.proj_id else None
    except NoteProjectionError as exc:
        return emit_error(
            "PROJECTION_FAILED",
            str(exc),
            use_json=use_json,
            detail=(
                f"The note event is committed in regista "
                f"(entity_id={exc.regista_note_id}). "
                + _RECOVERY_HINT.format(proj_slug=target.proj_slug)
            ),
            exit_code=EXIT_GENERIC,
        )
    if row is None:
        return emit_error(
            "NOT_FOUND",
            f"Memory '{args.name}' not found (or already deleted).",
            use_json=use_json,
            exit_code=EXIT_NOT_FOUND,
        )
    if use_json:
        print(
            json.dumps(
                {
                    "deleted": args.name,
                    "id": row["id"],
                    "scope": target.scope,
                    "scope_key": target.scope_key,
                },
                indent=2,
                default=str,
            )
        )
    else:
        print(f"Memory '{args.name}' soft-deleted (id={row['id']}).")
    return EXIT_SUCCESS


def cmd_mem_update(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    target = _target(args)
    if isinstance(target, int):
        return target

    from agent_notes.core.memory_model import update_memory

    if args.body is None and args.attributes is None:
        msg = "At least one of --body or --attributes is required"
        return emit_error(
            "VALIDATION_FAILED",
            msg,
            use_json=use_json,
            exit_code=EXIT_CONFLICT,
        )

    attributes = json.loads(args.attributes) if args.attributes else None
    try:
        if target.proj_id is None:
            raise ValueError(f"Memory '{args.name}' not found (or deleted)")
        mem = update_memory(
            workspace_id=target.ws_id,
            project_id=target.proj_id,
            name=args.name,
            body=args.body,
            attributes=attributes,
        )
    except NoteProjectionError as exc:
        return emit_error(
            "PROJECTION_FAILED",
            str(exc),
            use_json=use_json,
            detail=(
                f"The note event is committed in regista "
                f"(entity_id={exc.regista_note_id}). "
                + _RECOVERY_HINT.format(proj_slug=target.proj_slug)
            ),
            exit_code=EXIT_GENERIC,
        )
    except ValueError as exc:
        return emit_error(
            "NOT_FOUND",
            str(exc),
            use_json=use_json,
            exit_code=EXIT_NOT_FOUND,
        )

    mem = _tag(mem, target)
    if use_json:
        print(json.dumps({"memory": mem}, indent=2, default=str))
    else:
        print(f"Memory '{mem['name']}' updated (id={mem['id']}).")
    return EXIT_SUCCESS


def cmd_mem_rebuild(args: argparse.Namespace) -> int:
    """Rebuild the local memories projection from the regista note authority.

    Uses the same document-task embedder and dimension semantics as the write
    path (``embed(body, task="document")``), so rebuilt rows are search-ready
    without a separate re-embed pass. This is the operator recovery path for
    PROJECTION_FAILED split-brain states.
    """
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return code

    from agent_notes.core.face_factory import get_face
    from agent_notes.core.note_model import rebuild_from_regista

    face = get_face()
    if face is None:
        return emit_error(
            "NOT_CONFIGURED",
            "regista writes not enabled; set REGISTA_DSN and AGENT_NOTES_REGISTA_WRITES=1",
            use_json=use_json,
            exit_code=EXIT_NOT_CONFIGURED,
        )

    from agent_notes.core.embed import embed

    def _embed_for_rebuild(body: str) -> list[float]:
        vec: list[float] = embed(body, task="document").tolist()
        return vec

    report = rebuild_from_regista(face, project_id=proj_id, embed_fn=_embed_for_rebuild)

    if use_json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(
            f"Rebuilt memories projection from regista: "
            f"{report['mirrored']} mirrored, {report['created']} created, "
            f"{report['skipped']} skipped, {report['failed']} failed."
        )
    if report["failed"] > 0:
        return EXIT_GENERIC
    return EXIT_SUCCESS


def cmd_mem_check_drift(args: argparse.Namespace) -> int:
    """Read-only drift check: compare regista note entities vs local memories.

    Reports entities that exist in regista but are missing or stale in the
    local projection (the hard-crash split-brain class). Never mutates —
    this is a diagnostic, not a repair. Repair is ``memory rebuild-from-regista``.
    """
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return code

    from agent_notes.core.face_factory import get_face
    from agent_notes.core.note_model import check_projection_drift

    face = get_face()
    if face is None:
        return emit_error(
            "NOT_CONFIGURED",
            "regista writes not enabled; set REGISTA_DSN and AGENT_NOTES_REGISTA_WRITES=1",
            use_json=use_json,
            exit_code=EXIT_NOT_CONFIGURED,
        )

    drift = check_projection_drift(face, project_id=proj_id)

    if use_json:
        print(json.dumps(drift, indent=2, default=str))
    else:
        if drift["drifted"] == 0:
            print("No projection drift detected.")
        else:
            print(
                f"DRIFT: {drift['drifted']} regista note entit(y/ies) diverge "
                f"from the local projection."
            )
            for entity_id in drift["missing_entity_ids"]:
                print(f"  missing locally: {entity_id}")
            for entry in drift["stale"]:
                print(
                    f"  stale: {entry['entity_id']} ({entry['name']}): "
                    f"{'; '.join(entry['reasons'])}"
                )
            print(f"Run 'agent-notes memory rebuild-from-regista --project {proj_slug}' to repair.")
    # Exit nonzero when drift exists so monitoring can gate on the result.
    # The structured JSON is still emitted (above) for programmatic consumption.
    return EXIT_CONFLICT if drift["drifted"] > 0 else EXIT_SUCCESS


def register_memory_parsers(sub: argparse._SubParsersAction) -> None:
    mem = sub.add_parser("memory", help="Memory operations")
    mem_sub = mem.add_subparsers(dest="mem_cmd")

    mem_add = mem_sub.add_parser("add", help="Add a memory")
    mem_add.add_argument("--name", required=True)
    mem_add.add_argument("--body", required=True)
    mem_add.add_argument("--type", required=True, dest="type")
    mem_add.add_argument("--attributes", default=None)
    add_scope_args(mem_add, default="repo")
    _add_common(mem_add)
    mem_add.set_defaults(func=cmd_mem_add)

    mem_get = mem_sub.add_parser("get", help="Get a memory")
    mem_get.add_argument("name")
    add_scope_args(mem_get, default="repo")
    _add_common(mem_get)
    mem_get.set_defaults(func=cmd_mem_get)

    mem_list = mem_sub.add_parser("list", help="List memories")
    mem_list.add_argument("--type", default=None, dest="type")
    mem_list.add_argument("--limit", type=int, default=50)
    add_scope_args(mem_list, composed=True, default="repo")
    _add_common(mem_list)
    mem_list.set_defaults(func=cmd_mem_list)

    mem_search = mem_sub.add_parser("search", help="Search memories")
    mem_search.add_argument("query")
    mem_search.add_argument("--limit", type=int, default=10)
    add_scope_args(mem_search, composed=True, default="composed")
    _add_common(mem_search)
    mem_search.set_defaults(func=cmd_mem_search)

    mem_recall = mem_sub.add_parser(
        "recall", help="Recall from the configured memory engine across memory tiers"
    )
    mem_recall.add_argument("query")
    mem_recall.add_argument("--limit", type=int, default=10)
    mem_recall.add_argument("--budget", choices=["low", "mid", "high"], default="mid")
    add_scope_args(mem_recall, composed=True, default="composed")
    _add_common(mem_recall)
    mem_recall.set_defaults(func=cmd_mem_recall)

    mem_delete = mem_sub.add_parser("delete", help="Delete (soft) a memory")
    mem_delete.add_argument("name")
    add_scope_args(mem_delete, default="repo")
    _add_common(mem_delete)
    mem_delete.set_defaults(func=cmd_mem_delete)

    mem_update = mem_sub.add_parser("update", help="Update a memory in-place")
    mem_update.add_argument("name")
    mem_update.add_argument("--body", default=None, help="Replace body text")
    mem_update.add_argument("--attributes", default=None, help="Merge attributes (JSON)")
    add_scope_args(mem_update, default="repo")
    _add_common(mem_update)
    mem_update.set_defaults(func=cmd_mem_update)

    mem_rebuild = mem_sub.add_parser(
        "rebuild-from-regista",
        help="Rebuild the local memories projection from the regista note authority",
    )
    _add_common(mem_rebuild)
    mem_rebuild.set_defaults(func=cmd_mem_rebuild)

    mem_drift = mem_sub.add_parser(
        "check-drift",
        help="Read-only check for regista-vs-local memory projection drift",
    )
    _add_common(mem_drift)
    mem_drift.set_defaults(func=cmd_mem_check_drift)

    mem.set_defaults(func=lambda args: _print_sub_help(mem))
