from __future__ import annotations

import argparse
import json

from agent_notes.cli.common import (
    EXIT_CONFLICT,
    EXIT_GENERIC,
    EXIT_NOT_CONFIGURED,
    EXIT_NOT_FOUND,
    EXIT_SUCCESS,
    _add_common,
    _print_sub_help,
    _resolve,
    emit_error,
    report_resolution_failure,
)
from agent_notes.core.note_model import NoteProjectionError

_RECOVERY_HINT = (
    "Run 'agent-notes memory rebuild-from-regista --project {proj_slug}' "
    "to re-derive the local projection from the regista authority."
)


def cmd_mem_add(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return code

    from agent_notes.core.embed import embed
    from agent_notes.core.memory_model import add_memory

    if not args.body or not args.body.strip():
        return emit_error(
            "VALIDATION_FAILED",
            "--body must be a non-empty string",
            use_json=use_json,
            exit_code=EXIT_CONFLICT,
        )

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

    if use_json:
        print(json.dumps({"memory": mem}, indent=2, default=str))
    else:
        old_id = mem.get("supersedes")
        superseded_msg = f" (superseded id={old_id})" if old_id else ""
        mem_name = mem["name"]
        mem_id = mem["id"]
        mem_type = mem["memory_type"]
        print(f"Memory '{mem_name}' added (id={mem_id}, type={mem_type}){superseded_msg}")
    return EXIT_SUCCESS


def cmd_mem_get(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return code

    from agent_notes.core.memory_model import get_memory

    mem = get_memory(ws_id, proj_id, args.name)
    if mem is None:
        return emit_error(
            "NOT_FOUND",
            f"Memory '{args.name}' not found.",
            use_json=use_json,
            exit_code=EXIT_NOT_FOUND,
        )

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


def cmd_mem_list(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return code

    from agent_notes.core.memory_model import list_memories

    rows = list_memories(
        workspace_id=ws_id,
        project_id=proj_id,
        memory_type=args.type,
        limit=min(args.limit or 50, 200),
    )
    if use_json:
        print(json.dumps({"memories": rows}, indent=2, default=str))
    else:
        if not rows:
            print("No memories found.")
        else:
            print(f"{len(rows)} memory(ies):")
            for r in rows:
                preview = (r.get("body_preview") or "").replace("\n", " ")[:80]
                print(f"- **{r['name']}** (type={r['memory_type']}) {preview}")
    return EXIT_SUCCESS


def cmd_mem_search(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return code

    from agent_notes.core.embed import embed
    from agent_notes.core.memory_model import search_memory_with_body

    vec = embed(args.query, task="query").tolist()
    rows = search_memory_with_body(
        workspace_id=ws_id,
        query_vec=vec,
        project_id=proj_id,
        limit=min(args.limit or 10, 50),
    )
    if use_json:
        print(json.dumps({"memories": rows}, indent=2, default=str))
    else:
        if not rows:
            print("No matching memories.")
        else:
            print(f"{len(rows)} memory(ies) matched:")
            for r in rows:
                print(f"- **{r['name']}** (type={r['memory_type']}, score={r['score']:.3f})")
                if r.get("body"):
                    print(f"  {r['body'][:200]}")
    return EXIT_SUCCESS


def cmd_mem_delete(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return code

    from agent_notes.core.memory_model import delete_memory

    try:
        row = delete_memory(ws_id, proj_id, args.name)
    except NoteProjectionError as exc:
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
    if row is None:
        return emit_error(
            "NOT_FOUND",
            f"Memory '{args.name}' not found (or already deleted).",
            use_json=use_json,
            exit_code=EXIT_NOT_FOUND,
        )
    if use_json:
        print(json.dumps({"deleted": args.name, "id": row["id"]}, indent=2, default=str))
    else:
        print(f"Memory '{args.name}' soft-deleted (id={row['id']}).")
    return EXIT_SUCCESS


def cmd_mem_update(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return code

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
        mem = update_memory(
            workspace_id=ws_id,
            project_id=proj_id,
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
                f"(entity_id={exc.regista_note_id}). " + _RECOVERY_HINT.format(proj_slug=proj_slug)
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
    _add_common(mem_add)
    mem_add.set_defaults(func=cmd_mem_add)

    mem_get = mem_sub.add_parser("get", help="Get a memory")
    mem_get.add_argument("name")
    _add_common(mem_get)
    mem_get.set_defaults(func=cmd_mem_get)

    mem_list = mem_sub.add_parser("list", help="List memories")
    mem_list.add_argument("--type", default=None, dest="type")
    mem_list.add_argument("--limit", type=int, default=50)
    _add_common(mem_list)
    mem_list.set_defaults(func=cmd_mem_list)

    mem_search = mem_sub.add_parser("search", help="Search memories")
    mem_search.add_argument("query")
    mem_search.add_argument("--limit", type=int, default=10)
    _add_common(mem_search)
    mem_search.set_defaults(func=cmd_mem_search)

    mem_delete = mem_sub.add_parser("delete", help="Delete (soft) a memory")
    mem_delete.add_argument("name")
    _add_common(mem_delete)
    mem_delete.set_defaults(func=cmd_mem_delete)

    mem_update = mem_sub.add_parser("update", help="Update a memory in-place")
    mem_update.add_argument("name")
    mem_update.add_argument("--body", default=None, help="Replace body text")
    mem_update.add_argument("--attributes", default=None, help="Merge attributes (JSON)")
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
