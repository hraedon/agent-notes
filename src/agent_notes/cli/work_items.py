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
    emit_error,
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
        return emit_error(
            "VALIDATION_FAILED",
            str(exc),
            use_json=use_json,
            exit_code=EXIT_CONFLICT,
        )

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
        return emit_error(
            "FLAG_CONFLICT",
            "--body and --append-body are mutually exclusive",
            use_json=use_json,
            exit_code=EXIT_CONFLICT,
        )
    if args.body is not None:
        fields["body"] = args.body
    if args.append_body is not None:
        old = WorkItemModel.get_work_item(proj_id, args.identifier)
        if old is None:
            return emit_error(
                "NOT_FOUND",
                f"Work item '{args.identifier}' not found.",
                use_json=use_json,
                exit_code=EXIT_NOT_FOUND,
            )
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
            return emit_error(
                "NOT_FOUND",
                f"Work item '{args.identifier}' not found.",
                use_json=use_json,
                exit_code=EXIT_NOT_FOUND,
            )
        old_body = WorkItemModel.get_work_item_body(proj_id, args.identifier) or ""
        text = fields.get("title", old.get("title", "")) + " " + fields.get("body", old_body)
        fields["embedding"] = embed(text, task="document").tolist()

    try:
        wi = WorkItemModel.update_work_item(
            project_id=proj_id,
            identifier=args.identifier,
            force=getattr(args, "force", False),
            **fields,
        )
    except ValueError as exc:
        return emit_error(
            "NOT_FOUND",
            str(exc),
            use_json=use_json,
            exit_code=EXIT_NOT_FOUND,
        )

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
        return emit_error(
            "NOT_FOUND",
            f"Work item '{args.identifier}' not found.",
            use_json=use_json,
            exit_code=EXIT_NOT_FOUND,
        )

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
        return emit_error(
            "NOT_FOUND",
            f"Work item '{args.identifier}' not found.",
            use_json=use_json,
            exit_code=EXIT_NOT_FOUND,
        )

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
        wi = WorkItemModel.close_work_item(
            proj_id, args.identifier, force=getattr(args, "force", False)
        )
    except ValueError as exc:
        return emit_error(
            "NOT_FOUND",
            str(exc),
            use_json=use_json,
            exit_code=EXIT_NOT_FOUND,
        )

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
        return emit_error(
            "NOT_FOUND",
            str(exc),
            use_json=use_json,
            exit_code=EXIT_NOT_FOUND,
        )

    if use_json:
        print(json.dumps(result, indent=2, default=str))
    else:
        wi = result["work_item"]
        print(f"**{wi['identifier']}** — {wi['title']}")
        print(f"Status: {wi['status']} | Kind: {wi['kind']}")
        print(f"\nOps ({len(result['ops'])})\u003a")
        for op in result["ops"]:
            print(f"  {op['op_type']} (lamport={op['lamport']})")
        print(f"\nRecent changes ({len(result['recent_changes'])})\u003a")
        for ch in result["recent_changes"]:
            print(f"  {ch['event']} @ {ch['changed_at']}")
    return EXIT_SUCCESS


def cmd_wi_attest_gate(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return code

    from agent_notes.core.work_item_model import WorkItemModel

    try:
        wi = WorkItemModel.attest_gate_waiver(proj_id, args.identifier, reason=args.reason)
    except ValueError as exc:
        return emit_error(
            "NOT_FOUND",
            str(exc),
            use_json=use_json,
            exit_code=EXIT_NOT_FOUND,
        )

    if use_json:
        print(json.dumps({"work_item": wi}, indent=2, default=str))
    else:
        diag = wi.get("diagnostic_keys") or {}
        att = diag.get("gate_attestation", {}) if isinstance(diag, dict) else {}
        print(f"Gate waiver attested for **{wi['identifier']}** (status={wi['status']}).")
        if att:
            print(f"  Reason: {att.get('reason', '')}")
            print(f"  Actor:  {att.get('actor_id', '')}")
    return EXIT_SUCCESS


def _review_resolve(args: argparse.Namespace) -> tuple[int, str, bool]:
    """Resolve project + return (proj_id, proj_slug, use_json)."""
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return -1, "", use_json
    return proj_id, proj_slug, use_json


def cmd_wi_review_list(args: argparse.Namespace) -> int:
    """List work items awaiting review (in_review or in_human_review)."""
    proj_id, proj_slug, use_json = _review_resolve(args)
    if proj_id < 0:
        return EXIT_NOT_CONFIGURED

    from agent_notes.core.work_item_model import WorkItemModel

    in_review = WorkItemModel.query_work_items(project_id=proj_id, status="in_review", limit=200)
    in_human = WorkItemModel.query_work_items(
        project_id=proj_id, status="in_human_review", limit=200
    )
    items = in_review + in_human

    if use_json:
        for r in items:
            r.pop("embedding", None)
        print(json.dumps({"review_queue": items}, indent=2, default=str))
    else:
        if not items:
            print("No work items awaiting review.")
        else:
            print(f"{len(items)} work item(s) awaiting review:")
            for r in items:
                stage = "adversarial" if r["status"] == "in_review" else "human-gate"
                print(f"- **{r['identifier']}** [{stage}] ({r['status']}) — {r['title']}")
    return EXIT_SUCCESS


def cmd_wi_review_pass(args: argparse.Namespace) -> int:
    """Drive adversarial_pass (in_review → in_human_review)."""
    proj_id, proj_slug, use_json = _review_resolve(args)
    if proj_id < 0:
        return EXIT_NOT_CONFIGURED

    from agent_notes.core.work_item_model import WorkItemModel

    try:
        wi = WorkItemModel.review_transition(
            proj_id,
            args.identifier,
            transition_name="adversarial_pass",
            review_note=args.note,
            actor_id=getattr(args, "actor_id", None),
            model_lineage=getattr(args, "model_lineage", None),
            same_lineage_acknowledged=getattr(args, "same_lineage_acknowledged", False),
        )
    except ValueError as exc:
        return emit_error(
            "VALIDATION_FAILED",
            str(exc),
            use_json=use_json,
            exit_code=EXIT_CONFLICT,
        )

    if use_json:
        print(json.dumps({"work_item": wi}, indent=2, default=str))
    else:
        print(f"Adversarial pass recorded: **{wi['identifier']}** ({wi['status']})")
    return EXIT_SUCCESS


def cmd_wi_review_accept(args: argparse.Namespace) -> int:
    """Drive accept (in_human_review → done). Requires regista (gate runs)."""
    proj_id, proj_slug, use_json = _review_resolve(args)
    if proj_id < 0:
        return EXIT_NOT_CONFIGURED

    from agent_notes.core.work_item_model import WorkItemModel

    try:
        wi = WorkItemModel.review_transition(
            proj_id,
            args.identifier,
            transition_name="accept",
            review_note=args.note,
            actor_id=getattr(args, "actor_id", None),
            model_lineage=getattr(args, "model_lineage", None),
            same_lineage_acknowledged=getattr(args, "same_lineage_acknowledged", False),
        )
    except ValueError as exc:
        return emit_error(
            "VALIDATION_FAILED",
            str(exc),
            use_json=use_json,
            exit_code=EXIT_CONFLICT,
        )

    if use_json:
        print(json.dumps({"work_item": wi}, indent=2, default=str))
    else:
        print(f"Accepted: **{wi['identifier']}** ({wi['status']})")
    return EXIT_SUCCESS


def cmd_wi_review_reject(args: argparse.Namespace) -> int:
    """Drive reject (in_human_review → in_progress)."""
    proj_id, proj_slug, use_json = _review_resolve(args)
    if proj_id < 0:
        return EXIT_NOT_CONFIGURED

    from agent_notes.core.work_item_model import WorkItemModel

    try:
        wi = WorkItemModel.review_transition(
            proj_id,
            args.identifier,
            transition_name="reject",
            review_note=args.note,
            actor_id=getattr(args, "actor_id", None),
            model_lineage=getattr(args, "model_lineage", None),
            same_lineage_acknowledged=getattr(args, "same_lineage_acknowledged", False),
        )
    except ValueError as exc:
        return emit_error(
            "VALIDATION_FAILED",
            str(exc),
            use_json=use_json,
            exit_code=EXIT_CONFLICT,
        )

    if use_json:
        print(json.dumps({"work_item": wi}, indent=2, default=str))
    else:
        print(f"Rejected (back to in_progress): **{wi['identifier']}** ({wi['status']})")
    return EXIT_SUCCESS


def cmd_wi_review_request_changes(args: argparse.Namespace) -> int:
    """Drive request_changes (in_review → in_progress)."""
    proj_id, proj_slug, use_json = _review_resolve(args)
    if proj_id < 0:
        return EXIT_NOT_CONFIGURED

    from agent_notes.core.work_item_model import WorkItemModel

    try:
        wi = WorkItemModel.review_transition(
            proj_id,
            args.identifier,
            transition_name="request_changes",
            review_note=args.note,
            actor_id=getattr(args, "actor_id", None),
            model_lineage=getattr(args, "model_lineage", None),
            same_lineage_acknowledged=getattr(args, "same_lineage_acknowledged", False),
        )
    except ValueError as exc:
        return emit_error(
            "VALIDATION_FAILED",
            str(exc),
            use_json=use_json,
            exit_code=EXIT_CONFLICT,
        )

    if use_json:
        print(json.dumps({"work_item": wi}, indent=2, default=str))
    else:
        print(f"Changes requested (back to in_progress): **{wi['identifier']}** ({wi['status']})")
    return EXIT_SUCCESS


def cmd_wi_request(args: argparse.Namespace) -> int:
    """Request a new work item in a target project (cross-project, P3)."""
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return code

    from agent_notes.core.work_item_model import WorkItemModel

    try:
        req = WorkItemModel.request_work_item(
            project_id=proj_id,
            target_project_slug=args.target_project,
            title=args.title,
            body=args.body or "",
            kind=args.type,
        )
    except ValueError as exc:
        return emit_error(
            "VALIDATION_FAILED",
            str(exc),
            use_json=use_json,
            exit_code=EXIT_CONFLICT,
        )

    if use_json:
        print(json.dumps({"request": req}, indent=2, default=str))
    else:
        print(f"Request filed: **{req['identifier']}** → {args.target_project} ({req['kind']})")
    return EXIT_SUCCESS


def cmd_wi_wait(args: argparse.Namespace) -> int:
    """Wait on a target project's work item (cross-project, P3)."""
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return code

    from agent_notes.core.work_item_model import WorkItemModel

    try:
        target_project, target_identifier = WorkItemModel.parse_address(args.target)
    except ValueError as exc:
        return emit_error(
            "VALIDATION_FAILED",
            str(exc),
            use_json=use_json,
            exit_code=EXIT_CONFLICT,
        )

    try:
        wait = WorkItemModel.wait_on_work_item(
            project_id=proj_id,
            target_project_slug=target_project,
            target_identifier=target_identifier,
        )
    except ValueError as exc:
        return emit_error(
            "NOT_FOUND",
            str(exc),
            use_json=use_json,
            exit_code=EXIT_NOT_FOUND,
        )

    if use_json:
        print(json.dumps({"wait": wait}, indent=2, default=str))
    else:
        print(f"Wait registered: **{wait['identifier']}** → {target_project}:{target_identifier}")
    return EXIT_SUCCESS


def cmd_wi_link_cross(args: argparse.Namespace) -> int:
    """Add a cross-project link (P3)."""
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return code

    from agent_notes.core.work_item_model import WorkItemModel

    try:
        target_project, target_identifier = WorkItemModel.parse_address(args.target)
    except ValueError as exc:
        return emit_error(
            "VALIDATION_FAILED",
            str(exc),
            use_json=use_json,
            exit_code=EXIT_CONFLICT,
        )

    try:
        result = WorkItemModel.add_cross_project_link(
            from_project_id=proj_id,
            from_identifier=args.identifier,
            to_project_slug=target_project,
            to_identifier=target_identifier,
            relationship=args.relationship,
        )
    except ValueError as exc:
        return emit_error(
            "NOT_FOUND",
            str(exc),
            use_json=use_json,
            exit_code=EXIT_NOT_FOUND,
        )

    if use_json:
        print(json.dumps({"link": result}, indent=2, default=str))
    else:
        print(
            f"Link added: **{result['from_identifier']}** "
            f"{result['relationship']} {target_project}:{result['to_identifier']}"
        )
    return EXIT_SUCCESS


def cmd_wi_export_ops(args: argparse.Namespace) -> int:
    """Export local op-log for a project as JSONL (cross-project interchange)."""
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return code

    from agent_notes.core.cross_project import export_ops_jsonl

    jsonl = export_ops_jsonl(proj_id)
    if use_json:
        lines = jsonl.strip().split("\n") if jsonl.strip() else []
        ops = [json.loads(line) for line in lines if line.strip()]
        print(json.dumps({"project": proj_slug, "ops": ops}, indent=2, default=str))
    else:
        print(jsonl, end="")
    return EXIT_SUCCESS


def cmd_wi_ingest_ops(args: argparse.Namespace) -> int:
    """Ingest JSONL ops into the cross-project derived index."""
    use_json = getattr(args, "json", False)
    source_project = args.source_project
    if not source_project:
        return emit_error(
            "FLAG_MISSING",
            "Error: --source-project is required",
            use_json=use_json,
            exit_code=EXIT_CONFLICT,
        )

    import sys

    from agent_notes.core.cross_project import ingest_jsonl_ops

    if args.file:
        try:
            with open(args.file, "r", encoding="utf-8") as f:
                jsonl_data = f.read()
        except OSError as exc:
            return emit_error(
                "FILE_UNREADABLE",
                f"Cannot read {args.file}: {exc}",
                use_json=use_json,
                exit_code=EXIT_NOT_FOUND,
            )
    else:
        jsonl_data = sys.stdin.read()

    count = ingest_jsonl_ops(jsonl_data, source_project)
    if use_json:
        print(json.dumps({"ingested": count, "source_project": source_project}, indent=2))
    else:
        print(f"Ingested {count} op(s) from {source_project}")
    return EXIT_SUCCESS


def cmd_wi_rebuild_cache(args: argparse.Namespace) -> int:
    """Rebuild the cross-project work_items cache from the derived index."""
    use_json = getattr(args, "json", False)
    from agent_notes.core.cross_project import rebuild_cross_project_cache

    count = rebuild_cross_project_cache()
    if use_json:
        print(json.dumps({"rebuilt": count}, indent=2))
    else:
        print(f"Rebuilt {count} cross-project work item(s)")
    return EXIT_SUCCESS


def cmd_wi_claim(args: argparse.Namespace) -> int:
    """Claim a work item (P4)."""
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return code

    from agent_notes.core.work_item_model import WorkItemModel

    try:
        wi = WorkItemModel.claim_work_item(
            project_id=proj_id,
            identifier=args.identifier,
            actor_id=args.actor_id,
            ttl_seconds=args.ttl,
        )
    except ValueError as exc:
        return emit_error(
            "VALIDATION_FAILED",
            str(exc),
            use_json=use_json,
            exit_code=EXIT_CONFLICT,
        )

    if use_json:
        print(json.dumps({"work_item": wi}, indent=2, default=str))
    else:
        print(f"Claimed: **{wi['identifier']}** (expires in {args.ttl}s)")
    return EXIT_SUCCESS


def cmd_wi_release(args: argparse.Namespace) -> int:
    """Release a claimed work item (P4)."""
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return code

    from agent_notes.core.work_item_model import WorkItemModel

    try:
        wi = WorkItemModel.release_work_item(
            project_id=proj_id,
            identifier=args.identifier,
            actor_id=args.actor_id,
        )
    except ValueError as exc:
        return emit_error(
            "VALIDATION_FAILED",
            str(exc),
            use_json=use_json,
            exit_code=EXIT_CONFLICT,
        )

    if use_json:
        print(json.dumps({"work_item": wi}, indent=2, default=str))
    else:
        print(f"Released: **{wi['identifier']}**")
    return EXIT_SUCCESS


def cmd_wi_heartbeat(args: argparse.Namespace) -> int:
    """Heartbeat a claimed work item to extend its lease (P4)."""
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return code

    from agent_notes.core.work_item_model import WorkItemModel

    try:
        lease = WorkItemModel.heartbeat_work_item(
            project_id=proj_id,
            identifier=args.identifier,
            actor_id=args.actor_id,
            ttl_seconds=args.ttl,
        )
    except ValueError as exc:
        return emit_error(
            "VALIDATION_FAILED",
            str(exc),
            use_json=use_json,
            exit_code=EXIT_CONFLICT,
        )

    if use_json:
        print(json.dumps({"lease": lease}, indent=2, default=str))
    else:
        print(
            f"Heartbeat: **{args.identifier}** "
            f"(expires {lease['expires_at']}, heartbeats: {lease['heartbeat_count']})"
        )
    return EXIT_SUCCESS


def cmd_wi_requeue_expired(args: argparse.Namespace) -> int:
    """Sweep expired work-item leases and return them to claimable (P4)."""
    use_json = getattr(args, "json", False)
    from agent_notes.core.db import _conn

    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT sweep_expired_leases()")
        count = cur.fetchone()[0]

    if use_json:
        print(json.dumps({"expired_leases_removed": count}, indent=2))
    else:
        print(f"Swept {count} expired lease(s)")
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
    wi_update.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Bypass the transition pre-flight check (Plan 013 WI-5; admin/repair only)",
    )
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

    # Claim / lease commands (P4)
    wi_claim = wi_sub.add_parser("claim", help="Claim a work item (acquire lease)")
    wi_claim.add_argument("identifier")
    wi_claim.add_argument("--actor-id", default=None, help="Actor claiming the item")
    wi_claim.add_argument(
        "--ttl", type=int, default=300, help="Lease TTL in seconds (default: 300)"
    )
    _add_common(wi_claim)
    wi_claim.set_defaults(func=cmd_wi_claim)

    wi_release = wi_sub.add_parser("release", help="Release a claimed work item")
    wi_release.add_argument("identifier")
    wi_release.add_argument("--actor-id", default=None, help="Actor releasing the item")
    _add_common(wi_release)
    wi_release.set_defaults(func=cmd_wi_release)

    wi_heartbeat = wi_sub.add_parser(
        "heartbeat", help="Heartbeat a claimed work item to extend lease"
    )
    wi_heartbeat.add_argument("identifier")
    wi_heartbeat.add_argument("--actor-id", default=None, help="Actor heartbeating the item")
    wi_heartbeat.add_argument(
        "--ttl", type=int, default=300, help="Extended TTL in seconds (default: 300)"
    )
    _add_common(wi_heartbeat)
    wi_heartbeat.set_defaults(func=cmd_wi_heartbeat)

    wi_requeue = wi_sub.add_parser(
        "requeue-expired", help="Sweep expired leases and return items to claimable"
    )
    _add_common(wi_requeue)
    wi_requeue.set_defaults(func=cmd_wi_requeue_expired)

    wi_close = wi_sub.add_parser("close", help="Close a work item (submits for review)")
    wi_close.add_argument("identifier")
    wi_close.add_argument(
        "--force",
        action="store_true",
        help="Admin/repair: write a terminal close instead of deferring to review",
    )
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

    wi_attest = wi_sub.add_parser(
        "attest-gate",
        help="Record that the review gate was waived for a degraded completion (Plan 014 WI-4)",
    )
    wi_attest.add_argument("identifier")
    wi_attest.add_argument(
        "--reason",
        required=True,
        help="Why the cross-lineage review gate was waived (recorded in the op-chain)",
    )
    _add_common(wi_attest)
    wi_attest.set_defaults(func=cmd_wi_attest_gate)

    # Review-gate commands (adversarial_pass / accept / reject / request_changes)
    wi_review = wi_sub.add_parser(
        "review",
        help="Drive the cross-lineage review gate "
        "(adversarial_pass / accept / reject / request_changes)",
    )
    wi_review_sub = wi_review.add_subparsers(dest="review_cmd")

    wi_review_list = wi_review_sub.add_parser(
        "list", help="List work items awaiting review (in_review / in_human_review)"
    )
    _add_common(wi_review_list)
    wi_review_list.set_defaults(func=cmd_wi_review_list)

    def _add_review_identity(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--actor-id",
            default=None,
            help="Override the reviewer's actor_id (default: AGENT_NOTES_ACTOR_ID env)",
        )
        parser.add_argument(
            "--model-lineage",
            default=None,
            help="Override the reviewer's model lineage (default: AGENT_NOTES_MODEL_LINEAGE env). "
            "Required for cross-lineage gate if the author was an agent.",
        )
        parser.add_argument(
            "--same-lineage-acknowledged",
            action="store_true",
            default=False,
            help="Acknowledge same-lineage review (regista validator requires this if the "
            "reviewer shares a model lineage with an author)",
        )

    wi_review_pass = wi_review_sub.add_parser(
        "pass", help="Record an adversarial review pass (in_review → in_human_review)"
    )
    wi_review_pass.add_argument("identifier")
    wi_review_pass.add_argument(
        "--note", required=True, help="Review findings (required by the gate validator)"
    )
    _add_review_identity(wi_review_pass)
    _add_common(wi_review_pass)
    wi_review_pass.set_defaults(func=cmd_wi_review_pass)

    wi_review_accept = wi_review_sub.add_parser(
        "accept",
        help="Accept the work item (in_human_review → done). Requires regista (gate runs).",
    )
    wi_review_accept.add_argument("identifier")
    wi_review_accept.add_argument(
        "--note", required=True, help="Acceptance rationale (required by the gate validator)"
    )
    _add_review_identity(wi_review_accept)
    _add_common(wi_review_accept)
    wi_review_accept.set_defaults(func=cmd_wi_review_accept)

    wi_review_reject = wi_review_sub.add_parser(
        "reject", help="Reject the work item (in_human_review → in_progress)"
    )
    wi_review_reject.add_argument("identifier")
    wi_review_reject.add_argument(
        "--note", required=True, help="Rejection rationale (required by the gate validator)"
    )
    _add_review_identity(wi_review_reject)
    _add_common(wi_review_reject)
    wi_review_reject.set_defaults(func=cmd_wi_review_reject)

    wi_review_rc = wi_review_sub.add_parser(
        "request-changes",
        help="Request changes (in_review → in_progress)",
    )
    wi_review_rc.add_argument("identifier")
    wi_review_rc.add_argument(
        "--note", required=True, help="Change requests (required by the gate validator)"
    )
    _add_review_identity(wi_review_rc)
    _add_common(wi_review_rc)
    wi_review_rc.set_defaults(func=cmd_wi_review_request_changes)

    wi_review.set_defaults(func=lambda args: (_print_sub_help(wi_review), EXIT_SUCCESS)[1])

    # Cross-project commands (P3)
    wi_request = wi_sub.add_parser(
        "request", help="Request a new work item in a target project (cross-project)"
    )
    wi_request.add_argument("target_project", help="Target project slug")
    wi_request.add_argument("--title", required=True)
    wi_request.add_argument("--body", default="")
    wi_request.add_argument("--type", default="task", dest="type")
    _add_common(wi_request)
    wi_request.set_defaults(func=cmd_wi_request)

    wi_wait = wi_sub.add_parser("wait", help="Wait on a target project's work item (cross-project)")
    wi_wait.add_argument("target", help="Target address: project:identifier")
    _add_common(wi_wait)
    wi_wait.set_defaults(func=cmd_wi_wait)

    wi_link_cross = wi_sub.add_parser(
        "link-cross", help="Add a cross-project link (blocks by default)"
    )
    wi_link_cross.add_argument("identifier", help="Local work item identifier")
    wi_link_cross.add_argument("target", help="Target address: project:identifier")
    wi_link_cross.add_argument(
        "--relationship", default="blocks", help="Link relationship (default: blocks)"
    )
    _add_common(wi_link_cross)
    wi_link_cross.set_defaults(func=cmd_wi_link_cross)

    # Cross-project interchange (P3)
    wi_export = wi_sub.add_parser(
        "export-ops", help="Export local op-log as JSONL (cross-project interchange)"
    )
    _add_common(wi_export)
    wi_export.set_defaults(func=cmd_wi_export_ops)

    wi_ingest = wi_sub.add_parser(
        "ingest-ops", help="Ingest JSONL ops into the cross-project derived index"
    )
    wi_ingest.add_argument("--source-project", required=True, help="Source project slug")
    wi_ingest.add_argument("--file", default=None, help="Path to JSONL file (default: stdin)")
    _add_common(wi_ingest)
    wi_ingest.set_defaults(func=cmd_wi_ingest_ops)

    wi_rebuild = wi_sub.add_parser(
        "rebuild-cache", help="Rebuild cross-project work_items cache from derived index"
    )
    _add_common(wi_rebuild)
    wi_rebuild.set_defaults(func=cmd_wi_rebuild_cache)

    wi.set_defaults(func=lambda args: (_print_sub_help(wi), EXIT_SUCCESS)[1])
