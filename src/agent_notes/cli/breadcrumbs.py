"""Breadcrumb CLI — compatibility alias for work-item operations.

The ``breadcrumbs`` table has been dropped (Plan 008 Tier A).  All data lives
in ``work_items``.  This module keeps the ``agent-notes breadcrumb`` subcommands
working as thin wrappers around :class:`WorkItemModel`, translating bc_status
values to wi_status values so existing skills and scripts continue to work.
"""

from __future__ import annotations

import argparse
import json
import subprocess
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
    _print_sub_help,
    _resolve,
    emit_error,
    report_resolution_failure,
)
from agent_notes.cli.work_items import _add_author_identity
from agent_notes.core import config as reg_config
from agent_notes.core import outbox
from agent_notes.core.lifecycle import map_legacy_to_canonical as _map_status

# Plan 013: the legacy bc_status → wi_status mapping now lives in
# ``lifecycle.LEGACY_TO_CANONICAL`` (single source). The ``_map_status`` alias
# is imported above from ``lifecycle.map_legacy_to_canonical``. Canonical
# states pass through; ``claimed`` is never emitted (liveness axis, not a
# workflow state).


def _wi_to_bc_display(wi: dict, body: str | None = None) -> dict:
    out = dict(wi)
    out.pop("embedding", None)
    out.pop("body_hash", None)
    if body is not None:
        out["body"] = body
    return out


def cmd_bc_file(args: argparse.Namespace) -> int:
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
            status=_map_status(args.status),
            severity=args.severity or "medium",
            external_refs=external_refs,
            diagnostic_keys=diagnostic_keys,
            embedding=vec,
            actor_id=getattr(args, "actor_id", None),
            model_lineage=getattr(args, "model_lineage", None),
        )
    except ValueError as exc:
        return emit_error(
            "VALIDATION_FAILED",
            str(exc),
            use_json=use_json,
            exit_code=EXIT_CONFLICT,
        )

    if use_json:
        body = WorkItemModel.get_work_item_body(proj_id, wi["identifier"]) or ""
        print(json.dumps({"breadcrumb": _wi_to_bc_display(wi, body)}, indent=2, default=str))
    else:
        ident = wi["identifier"]
        kind = wi["kind"]
        status = wi["status"]
        print(f"Work item filed: **{ident}** ({kind} / {status}) in project {proj_slug}")
    return EXIT_SUCCESS


def cmd_bc_update(args: argparse.Namespace) -> int:
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
        existing = (WorkItemModel.get_work_item_body(proj_id, args.identifier) or "").rstrip()
        sep = "\n\n" if existing else ""
        fields["body"] = f"{existing}{sep}{args.append_body}"
    if args.type is not None:
        fields["kind"] = args.type
    if args.status is not None:
        fields["status"] = _map_status(args.status)
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
            actor_id=getattr(args, "actor_id", None),
            model_lineage=getattr(args, "model_lineage", None),
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
        body = WorkItemModel.get_work_item_body(proj_id, args.identifier) or ""
        print(json.dumps({"breadcrumb": _wi_to_bc_display(wi, body)}, indent=2, default=str))
    else:
        print(f"Work item updated: **{wi['identifier']}** ({wi['kind']} / {wi['status']})")
    return EXIT_SUCCESS


def cmd_bc_get(args: argparse.Namespace) -> int:
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

    body = WorkItemModel.get_work_item_body(proj_id, args.identifier) or ""

    if use_json:
        display = _wi_to_bc_display(wi)
        display["body"] = body
        print(json.dumps({"breadcrumb": display}, indent=2, default=str))
    else:
        print(f"**{wi['identifier']}** — {wi['title']}")
        print(f"Kind: {wi['kind']} | Status: {wi['status']} | Severity: {wi['severity']}")
        if body:
            print(f"\n{body}")
    return EXIT_SUCCESS


def cmd_bc_find(args: argparse.Namespace) -> int:
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
        if text.isdigit() or (
            (text.startswith("BC-") or text.startswith("WI-")) and text[3:].isdigit()
        ):
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
            status=_map_status(args.status) if args.status else None,
            kind=args.type,
            limit=min(args.limit or 50, 200),
        )

    if use_json:
        for r in rows:
            r.pop("embedding", None)
        display_rows = [_wi_to_bc_display(r) for r in rows]
        print(json.dumps({"breadcrumbs": display_rows}, indent=2, default=str))
    else:
        if not rows:
            print("No work items found.")
        else:
            print(f"{len(rows)} work item(s) found:")
            for r in rows:
                print(f"- **{r['identifier']}** ({r['kind']} / {r['status']}) — {r['title']}")
    return EXIT_SUCCESS


def cmd_bc_delete(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return code

    from agent_notes.core.work_item_model import WorkItemModel

    deleted = WorkItemModel.delete_work_item(
        proj_id,
        args.identifier,
        actor_id=getattr(args, "actor_id", None),
        model_lineage=getattr(args, "model_lineage", None),
    )
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
        return emit_error(
            "OPERATION_FAILED",
            msg,
            use_json=use_json,
            exit_code=EXIT_GENERIC,
        )

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


def _check_gitignored(repo_root: str | Path, path: Path) -> bool | None:
    """Return whether *path* is gitignored inside the repo at *repo_root*.

    Mirrors the fail-safe subprocess style of
    :func:`agent_notes.core.git_reconcile.scan_git_for_resolutions`.
    Returns ``True`` when *path* matches an ignore rule (git exit 0),
    ``False`` when it is explicitly not ignored (git exit 1), or ``None``
    when git could not decide — not a repository, git unavailable, or any
    other non-0/non-1 exit. ``None`` means "skip the guard": we refuse to
    block a write solely because we could not ask git.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "check-ignore", str(path)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    return None


def cmd_bc_export_index(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return code

    from agent_notes.core.db import list_projects
    from agent_notes.core.work_item_model import WorkItemModel

    repo_root: str | None = None
    if args.output is not None:
        out_path = Path(args.output)
    else:
        proj = next((p for p in list_projects(workspace_id=ws_id) if p.id == proj_id), None)
        repo_root = proj.repo_root if proj else None
        if not repo_root:
            msg = (
                "cannot determine repo root for this project; pass --output to "
                "choose the output path explicitly."
            )
            return emit_error(
                "OPERATION_FAILED",
                msg,
                use_json=use_json,
                exit_code=EXIT_GENERIC,
            )
        out_path = Path(repo_root) / "OPEN_WORK_ITEMS.txt"

    # Guard (WI-011/WI-012): refuse to write the generated index into the repo
    # tree unless it is gitignored, so a `git add -A` can't commit a churning
    # STALE banner. Only the default repo-root path is guarded — an explicit
    # --output is the operator's choice and is respected as-is. When git
    # cannot decide (no repo / git error) the guard is skipped rather than
    # blocking a legitimate write.
    if args.output is None and repo_root:
        ignored = _check_gitignored(repo_root, out_path.resolve())
        if ignored is False:
            msg = (
                f"refusing to write {out_path} — not gitignored. Add it to "
                ".gitignore or use --output to write outside the repo."
            )
            if use_json:
                print(
                    json.dumps(
                        {
                            "error": f"refusing to write {out_path} — not gitignored",
                            "path": str(out_path),
                            "hint": "add OPEN_WORK_ITEMS.txt to .gitignore, or use "
                            "--output to write outside the repo",
                        },
                        indent=2,
                    )
                )
            else:
                print(msg, file=sys.stderr)
            return EXIT_GENERIC

    open_wis = WorkItemModel.query_work_items(project_id=proj_id, is_open=True, limit=200)
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    open_wis.sort(
        key=lambda b: (
            severity_order.get(b.get("severity", "medium"), 2),
            b.get("updated_at", ""),
        )
    )

    lines = [
        f"# Open Work Items for {proj_slug}",
        f"# Generated: {datetime.now().isoformat()}",
        f"# Total: {len(open_wis)}",
        "#",
        "# This file is a plain-text fallback. If the agent-notes CLI is unavailable,",
        "# agents can read this file to see open work items.",
        "# Do not edit by hand; regenerate with: agent-notes breadcrumb export-index",
        "",
    ]

    cfg = reg_config.regista_config()
    if cfg.enabled:
        n = outbox.count_ops(cfg.project)
        if n:
            lines.insert(
                0,
                f"> ⚠ STALE — {n} ops pending sync; run `agent-notes outbox reconcile`",
            )

    for wi in open_wis:
        ident = wi.get("identifier", "?")
        kind = wi.get("kind", "?")
        status = wi.get("status", "?")
        severity = wi.get("severity", "medium")
        title = wi.get("title", "(no title)")
        lines.append(f"[{severity}] {ident} ({kind} / {status}) — {title}")

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if use_json:
        print(json.dumps({"path": str(out_path), "count": len(open_wis)}, indent=2))
    else:
        print(f"Exported {len(open_wis)} open work item(s) to {out_path}")
    return EXIT_SUCCESS


def cmd_bc_reconcile(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return code

    from agent_notes.core.actor import InvalidLineageError, UndeclaredLineageError
    from agent_notes.core.db import list_projects
    from agent_notes.core.git_reconcile import scan_git_for_resolutions
    from agent_notes.core.work_item_model import WorkItemModel

    proj = next((p for p in list_projects(workspace_id=ws_id) if p.id == proj_id), None)
    repo_root = proj.repo_root if proj else None
    if not repo_root:
        msg = (
            f"Project {proj_slug!r} has no repo_root registered; cannot scan git "
            "history. Set it with 'agent-notes init' / project registration."
        )
        if use_json:
            print(json.dumps({"error": msg, "results": []}))
        else:
            print(msg, file=sys.stderr)
        return EXIT_NOT_CONFIGURED

    open_wis = WorkItemModel.query_work_items(project_id=proj_id, is_open=True, limit=200)
    by_id = {wi["identifier"]: wi for wi in open_wis}
    hits = scan_git_for_resolutions(
        repo_root, list(by_id), lookback=args.lookback, project_slug=proj_slug
    )

    results: list[dict] = []
    for ident, info in hits.items():
        wi = by_id[ident]
        applied = False
        error: str | None = None
        if args.apply:
            refs = dict(wi.get("external_refs") or {})
            refs["resolved_by_commit"] = info["commit"]
            refs["resolved_by_subject"] = info["subject"]
            try:
                WorkItemModel.update_work_item(
                    proj_id,
                    ident,
                    status="closed",
                    external_refs=refs,
                    force=True,
                    # WI-062: reconcile writes a real status transition, so it
                    # is a real authored event and needs a declared lineage
                    # like any other. Without these the only source would be
                    # the env, and `/start` / `/end` invoke this unattended.
                    actor_id=getattr(args, "actor_id", None),
                    model_lineage=getattr(args, "model_lineage", None),
                )
                applied = True
            except (UndeclaredLineageError, InvalidLineageError):
                # WI-068: never fold the lineage refusal into a generic
                # per-item error — burying it under a relabeled code is the
                # exact failure mode the gate's RuntimeError choice exists to
                # prevent (WI-062), and it is not a per-item condition anyway:
                # every remaining item would refuse identically. Re-raise so
                # ``_dispatch`` emits the canonical UNDECLARED_LINEAGE /
                # INVALID_MODEL_LINEAGE envelope (exit 3) with the remedy text
                # intact.
                raise
            except Exception as exc:
                # A work item in a state with no direct terminal transition
                # (e.g. in_review, which must go through the review gate)
                # cannot be auto-closed. Do not let one such item abort the
                # whole reconcile; record the failure and continue so the
                # operator sees every suggestion and can close the stragglers
                # by hand.
                error = f"{type(exc).__name__}: {exc}"
        results.append(
            {
                "identifier": ident,
                "current_status": wi.get("status"),
                "suggested_status": "closed",
                "commit": info["commit"],
                "subject": info["subject"],
                "applied": applied,
                "error": error,
            }
        )
    results.sort(key=lambda r: r["identifier"])

    had_apply_errors = args.apply and any(r.get("error") for r in results)

    if use_json:
        print(json.dumps({"results": results, "applied": args.apply}, indent=2, default=str))
        return EXIT_CONFLICT if had_apply_errors else EXIT_SUCCESS

    if not results:
        print("No open work items appear resolved in git history.")
        return EXIT_SUCCESS

    for r in results:
        if r["applied"]:
            print(f"  Resolved {r['identifier']} — {r['commit']} {r['subject']!r}")
        elif args.apply and r["error"]:
            print(f"  Could not auto-close {r['identifier']} ({r['current_status']}): {r['error']}")
        else:
            print(f"  Would resolve {r['identifier']} — {r['commit']} {r['subject']!r}")
    if not args.apply:
        print(f"\n{len(results)} suggestion(s). Re-run with --apply to update the DB.")
    return EXIT_CONFLICT if had_apply_errors else EXIT_SUCCESS


def register_breadcrumb_parsers(sub: argparse._SubParsersAction) -> None:
    bc = sub.add_parser("breadcrumb", help="Breadcrumb operations (alias for work-item)")
    bc_sub = bc.add_subparsers(dest="bc_cmd")

    bc_file = bc_sub.add_parser("file", help="File a breadcrumb (creates a work item)")
    bc_file.add_argument("--title", required=True)
    bc_file.add_argument("--body", default="")
    bc_file.add_argument("--identifier", default=None)
    bc_file.add_argument("--type", default="todo", dest="type")
    bc_file.add_argument("--status", default="open")
    bc_file.add_argument("--severity", default="medium")
    bc_file.add_argument("--external-refs", default=None)
    bc_file.add_argument("--diagnostic-keys", default=None)
    _add_author_identity(bc_file)
    _add_common(bc_file)
    bc_file.set_defaults(func=cmd_bc_file)

    bc_update = bc_sub.add_parser("update", help="Update a breadcrumb (updates a work item)")
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
    bc_update.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Bypass the transition pre-flight check (Plan 013 WI-5; admin/repair only)",
    )
    _add_author_identity(bc_update)
    _add_common(bc_update)
    bc_update.set_defaults(func=cmd_bc_update)

    bc_get = bc_sub.add_parser("get", help="Get a breadcrumb (work item)")
    bc_get.add_argument("identifier")
    _add_common(bc_get)
    bc_get.set_defaults(func=cmd_bc_get)

    bc_find = bc_sub.add_parser("find", help="Find breadcrumbs (work items) by text or filters")
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

    bc_delete = bc_sub.add_parser("delete", help="Delete a breadcrumb (work item)")
    bc_delete.add_argument("identifier")
    _add_author_identity(bc_delete)
    _add_common(bc_delete)
    bc_delete.set_defaults(func=cmd_bc_delete)

    bc_sync = bc_sub.add_parser(
        "sync", help="Import breadcrumb .md files as work items into the DB"
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
        help="Delete DB work items not present in files (hard delete, destructive)",
    )
    _add_common(bc_sync)
    bc_sync.set_defaults(func=cmd_bc_sync)

    bc_export_index = bc_sub.add_parser(
        "export-index",
        help="Export open work items to a plain-text fallback index in the repo root",
    )
    bc_export_index.add_argument(
        "--output",
        default=None,
        help="Output path (default: <repo-root>/OPEN_WORK_ITEMS.txt)",
    )
    _add_common(bc_export_index)
    bc_export_index.set_defaults(func=cmd_bc_export_index)

    bc_reconcile = bc_sub.add_parser(
        "reconcile",
        help="Detect (and optionally resolve) open work items already closed in git history",
    )
    bc_reconcile.add_argument(
        "--apply",
        action="store_true",
        help="Transition matched items to 'closed' (default: dry-run/suggest only)",
    )
    bc_reconcile.add_argument(
        "--lookback",
        type=int,
        default=400,
        help="Number of recent commits to scan (default: 400)",
    )
    _add_author_identity(bc_reconcile)
    _add_common(bc_reconcile)
    bc_reconcile.set_defaults(func=cmd_bc_reconcile)

    bc.set_defaults(func=lambda args: _print_sub_help(bc))
