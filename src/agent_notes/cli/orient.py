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

from agent_notes.cli.common import (
    EXIT_NOT_CONFIGURED,
    EXIT_SUCCESS,
    _add_common,
    _resolve,
    report_resolution_failure,
)


def cmd_orient(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return code

    from agent_notes.core.breadcrumbs_model import BreadcrumbModel
    from agent_notes.core.change_log import changes_since
    from agent_notes.core.memory_model import list_memories

    open_bcs = BreadcrumbModel.query_breadcrumbs(project_id=proj_id, is_open=True, limit=args.limit)
    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    changes = changes_since(since, workspace_id=ws_id, project_id=proj_id, limit=args.limit)
    memories = list_memories(workspace_id=ws_id, project_id=proj_id, limit=args.limit)

    payload = {
        "project": proj_slug,
        "workspace": ws_slug,
        "since_days": args.days,
        "open_breadcrumbs": [
            {
                "identifier": b["identifier"],
                "title": b["title"],
                "severity": b["severity"],
                "status": b["status"],
            }
            for b in open_bcs
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
    }

    if use_json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(f"Orientation for {proj_slug} (workspace {ws_slug}):")
        print(f"\nOpen breadcrumbs ({len(open_bcs)}):")
        for b in open_bcs:
            print(f"  - [{b['severity']}] {b['identifier']} ({b['status']}) — {b['title']}")
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
    _add_common(orient)
    orient.set_defaults(func=cmd_orient)
