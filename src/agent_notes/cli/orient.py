"""`agent-notes orient` — one-call session digest (Plan 007).

Consolidates what the /start skill does in three calls (open breadcrumbs +
recent changes + memories) into a single structured query. Cheap and
embedding-free by design: it is the payload a SessionStart hook injects on
every session, so it must not pay the embedding-model cold-load tax.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from agent_notes.cli.common import (
    EXIT_NOT_CONFIGURED,
    EXIT_SUCCESS,
    _add_common,
    _resolve,
    report_resolution_failure,
)
from agent_notes.core import config as reg_config
from agent_notes.core import outbox, projection
from agent_notes.core.db import _conn


def cmd_orient(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return code

    from agent_notes.core.change_log import changes_since
    from agent_notes.core.memory_model import list_memories
    from agent_notes.core.work_item_model import WorkItemModel

    open_wis = WorkItemModel.query_work_items(project_id=proj_id, is_open=True, limit=args.limit)
    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    changes = changes_since(since, workspace_id=ws_id, project_id=proj_id, limit=args.limit)
    memories = list_memories(workspace_id=ws_id, project_id=proj_id, limit=args.limit)

    # Optionally surface silent-resolution drift: open breadcrumbs that a recent
    # commit already closed via its message but that nobody transitioned in the
    # DB. Off by default — `orient` stays git-free and cheap (decision 15 keeps
    # git out of the default path). Enable with `--reconcile`; the natural place
    # is the SessionStart hook, configured once, so every session gets the check
    # without any per-session agent action. Read-only and fail-safe.
    drift: dict[str, dict[str, str]] = {}
    if getattr(args, "reconcile", False):
        from agent_notes.core.db import list_projects
        from agent_notes.core.git_reconcile import scan_git_for_resolutions

        _proj = next((p for p in list_projects(workspace_id=ws_id) if p.id == proj_id), None)
        _repo_root = _proj.repo_root if _proj else None
        drift = scan_git_for_resolutions(
            _repo_root, [wi["identifier"] for wi in open_wis], lookback=200
        )

    cfg = reg_config.regista_config()
    regista_sync: dict[str, Any] = {
        "enabled": cfg.enabled,
        "project": cfg.project,
        "outbox_pending": 0,
        "outbox_conflicts": 0,
        "outbox_rejected": 0,
        "pending_sync_rows": 0,
    }
    if cfg.enabled:
        regista_sync["outbox_pending"] = outbox.count_ops(cfg.project)
        regista_sync["outbox_conflicts"] = outbox.count_sidecar(cfg.project, "conflicts.jsonl")
        regista_sync["outbox_rejected"] = outbox.count_sidecar(cfg.project, "rejected.jsonl")
        with _conn() as conn:
            regista_sync["pending_sync_rows"] = projection.count_pending(conn, proj_id)

    payload = {
        "project": proj_slug,
        "workspace": ws_slug,
        "since_days": args.days,
        "open_work_items": [
            {
                "identifier": wi["identifier"],
                "title": wi["title"],
                "severity": wi["severity"],
                "status": wi["status"],
            }
            for wi in open_wis
        ],
        "recent_changes": [
            {
                "kind": c.kind,
                "identifier": c.identifier,
                "event": c.event,
                "changed_at": c.changed_at.isoformat(),
            }
            for c in changes
        ],
        "memories": [{"name": m["name"], "type": m["memory_type"]} for m in memories],
        "resolved_in_git": [
            {"identifier": ident, "commit": info["commit"], "subject": info["subject"]}
            for ident, info in sorted(drift.items())
        ],
        "regista_sync": regista_sync,
    }

    if use_json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(f"Orientation for {proj_slug} (workspace {ws_slug}):")
        print(f"\nOpen work items ({len(open_wis)}):")
        for wi in open_wis:
            print(f"  - [{wi['severity']}] {wi['identifier']} ({wi['status']}) — {wi['title']}")
        if drift:
            print(
                f"\n⚠ Resolved in git but still open in the DB ({len(drift)}) — "
                "run 'agent-notes breadcrumb reconcile --apply':"
            )
            for ident, info in sorted(drift.items()):
                print(f"  - {ident} — {info['commit']} {info['subject']!r}")
        pending = regista_sync["outbox_pending"]
        if cfg.enabled and (pending or regista_sync["outbox_conflicts"]
                or regista_sync["outbox_rejected"] or regista_sync["pending_sync_rows"]):
            print(
                f"\n⚠ STALE — {pending} op(s) pending sync"
                f" (+{regista_sync['outbox_conflicts']} conflicts, "
                f"{regista_sync['outbox_rejected']} rejected, "
                f"{regista_sync['pending_sync_rows']} stale projection rows); "
                "run 'agent-notes outbox reconcile'"
            )
        print(f"\nChanges in last {args.days}d ({len(changes)}):")
        for c in changes:
            print(f"  - [{c.kind}] {c.identifier} {c.event} @ {c.changed_at}")
        print(f"\nMemories ({len(memories)}):")
        for m in memories:
            print(f"  - {m['name']} ({m['memory_type']})")
    return EXIT_SUCCESS


def register_orient_parser(sub: argparse._SubParsersAction) -> None:
    orient = sub.add_parser(
        "orient",
        help="One-call session digest: open breadcrumbs, recent changes, memories",
    )
    orient.add_argument("--days", type=int, default=7, help="Recent-changes window (days)")
    orient.add_argument("--limit", type=int, default=15)
    orient.add_argument(
        "--reconcile",
        action="store_true",
        help="Also scan git history for open breadcrumbs already resolved in a "
        "commit (read-only; off by default). Ideal in the SessionStart hook.",
    )
    _add_common(orient)
    orient.set_defaults(func=cmd_orient)
