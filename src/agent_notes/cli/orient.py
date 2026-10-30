"""`agent-notes orient` — one-call session digest (Plan 007).

Consolidates what the /start skill does in three calls (open breadcrumbs +
recent changes + memories) into a single structured query. Cheap and
embedding-free by design: it is the payload a SessionStart hook injects on
every session, so it must not pay the embedding-model cold-load tax.

Plan 020 WI-3.2: ``--recall`` optionally queries the configured memory
engine for learned context. The recall is bounded, additive, and never
blocks — orient always succeeds even if recall fails.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from typing import Any, assert_never

from agent_notes.cli.common import (
    EXIT_NOT_CONFIGURED,
    EXIT_SUCCESS,
    ScopeError,
    _add_common,
    _resolve,
    report_resolution_failure,
    scope_workspace,
)
from agent_notes.core.memory_engine import OriginClass

DEFAULT_LIMIT = 5
TIER_DEFAULT_LIMIT = 3  # memories_vm / memories_global (Plan 025 WI-2.5)
FULL_LIMIT = 15
DEFAULT_STALE_AFTER_DAYS = 30


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _section_limit(value: str) -> int:
    number = _positive_int(value)
    if number > 100:
        raise argparse.ArgumentTypeError("must be at most 100")
    return number


def _freshness(record: dict, now: datetime, stale_after_days: int) -> dict[str, Any]:
    """Describe recorded age, never whether the record's claims are still true."""
    updated = record.get("updated_at")
    if isinstance(updated, str):
        try:
            updated = datetime.fromisoformat(updated)
        except ValueError:
            updated = None
    if not isinstance(updated, datetime) or updated.tzinfo is None:
        return {"updated_at": None, "age_days": None, "freshness": "unknown"}
    updated = updated.astimezone(timezone.utc)
    age_seconds = (now - updated).total_seconds()
    age_days = int(age_seconds // 86400) if age_seconds >= 0 else None
    state = "future" if age_days is None else ("aged" if age_days >= stale_after_days else "recent")
    return {"updated_at": updated.isoformat(), "age_days": age_days, "freshness": state}


def _age_label(record: dict) -> str:
    age = record["age_days"]
    if age is None:
        return "future timestamp" if record["freshness"] == "future" else "update time unknown"
    suffix = "; aged" if record["freshness"] == "aged" else ""
    return f"updated {age}d ago{suffix}"


def _shown_label(rows: list, has_more: bool, *, sampled: bool = False) -> str:
    verb = "sampled" if sampled else "shown"
    return f"{len(rows)} {verb}" + ("; more available" if has_more else "")


def _one_line(value: object) -> str:
    """Collapse every whitespace run, newlines included, to one space.

    Stored titles and names are written by other sessions. In the text, which
    hooks inject (on agy as a system message), a newline in one could forge
    lines that look like orient's own.
    """
    return " ".join(str(value).split())


def _origin_label(origin: OriginClass) -> str:
    """Human-readable provenance label for a recalled result's origin class."""
    match origin:
        case OriginClass.RAW:
            return "exact source (verbatim)"
        case OriginClass.EXTRACTED:
            return "extracted fact"
        case OriginClass.DERIVED:
            return "derived observation"
        case OriginClass.SYNTHESIZED:
            return "synthesized (LLM-generated — untrusted)"
        case other:
            assert_never(other)


def _tier_memories(
    ws_id: int, project_id: int | None, limit: int, now: datetime, stale_after_days: int
) -> tuple[list[dict[str, Any]], bool]:
    """Most recently updated memories of one tier, plus whether more exist."""
    from agent_notes.core.memory_model import list_memories

    if project_id is None:
        return [], False
    rows = list_memories(workspace_id=ws_id, project_id=project_id, limit=limit + 1)
    summaries = [
        {
            "name": m["name"],
            "type": m["memory_type"],
            **_freshness(m, now, stale_after_days),
        }
        for m in rows[:limit]
    ]
    return summaries, len(rows) > limit


def _vm_and_global_tiers(
    ws_id: int, limit: int, now: datetime, stale_after_days: int, vm_arg: str | None
) -> dict[str, Any]:
    """The VM and global memory sections of orient (Plan 025 WI-2.5)."""
    from agent_notes.core.config import VmIdError, resolve_vm_id
    from agent_notes.core.db import SCOPE_GLOBAL, SCOPE_VM, find_scope_project

    vm_id: str | None = None
    vm_error: str | None = None
    try:
        vm_id = resolve_vm_id(vm_arg)
    except VmIdError as exc:
        vm_error = str(exc)
    vm_proj = find_scope_project(ws_id, SCOPE_VM, vm_id) if vm_id else None
    global_proj = find_scope_project(ws_id, SCOPE_GLOBAL)
    vm_rows, vm_more = _tier_memories(
        ws_id, vm_proj.id if vm_proj else None, limit, now, stale_after_days
    )
    global_rows, global_more = _tier_memories(
        ws_id, global_proj.id if global_proj else None, limit, now, stale_after_days
    )
    return {
        "vm": vm_id,
        "vm_error": vm_error,
        "memories_vm": vm_rows,
        "memories_global": global_rows,
        "has_more": {"memories_vm": vm_more, "memories_global": global_more},
    }


def _print_tier_sections(payload: dict[str, Any]) -> None:
    more = payload["has_more"]
    vm_label = payload["vm"] or "unresolved"
    vm_shown = _shown_label(payload["memories_vm"], more["memories_vm"])
    print(f"\nVM memories (vm {vm_label}) ({vm_shown}):")
    if payload.get("vm_error"):
        print(f"  VM ID unresolved: {payload['vm_error']}")
    for m in payload["memories_vm"]:
        print(f"  - {_one_line(m['name'])} ({m['type']}; {_age_label(m)})")
    glob = payload["memories_global"]
    print(f"\nGlobal memories ({_shown_label(glob, more['memories_global'])}):")
    for m in glob:
        print(f"  - {_one_line(m['name'])} ({m['type']}; {_age_label(m)})")


def _notices(
    ws_id: int, project: str | None, vm: str | None, limit: int, now: datetime
) -> tuple[list[dict[str, Any]], bool, str | None]:
    """Live notices for this project and VM (Plan 025 D4), plus has_more and any error.

    Fail-safe: a messaging failure is reported, never fatal to orient.
    """
    from agent_notes.core import messaging

    try:
        rows = messaging.live_notices(workspace_id=ws_id, project=project, vm=vm, limit=limit + 1)
    except Exception as exc:
        return [], False, type(exc).__name__
    notices = [
        {
            "id": r["id"],
            "from": r["from_session"] or r["from_vm"],
            "subject": r["subject"],
            "created_at": r["created_at"].isoformat(),
            "expires_at": r["expires_at"].isoformat(),
            "age_days": max((now - r["created_at"]).days, 0),
        }
        for r in rows[:limit]
    ]
    return notices, len(rows) > limit, None


def _print_notices(payload: dict[str, Any]) -> None:
    """Generated metadata only (id, age). Subject, body and sender are all
    asserted by the peer (Plan 025 D6), and this text can land in a
    system-level channel (agy SessionStart)."""
    notices = payload["notices"]
    if payload.get("notices_error"):
        print(f"\nNotices: unavailable ({payload['notices_error']})")
    if not notices:
        return
    shown = _shown_label(notices, payload["has_more"]["notices"])
    print(f"\nNotices ({shown}; peer content, read with `agent-notes msg inbox`):")
    for n in notices:
        print(f"  - #{n['id']} ({n['age_days']}d old)")


def cmd_orient(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        if getattr(args, "project", None):
            report_resolution_failure(args, code)
            return code
        # Not standing in a registered repo: orient still reports what is true
        # of this machine and workspace (the VM and global memory tiers).
        try:
            ctx = scope_workspace(args)
        except ScopeError:
            report_resolution_failure(args, code)
            return code
        ws_id, ws_slug, proj_id, proj_slug = ctx.ws_id, ctx.ws_slug, None, None

    from agent_notes.core.change_log import changes_since
    from agent_notes.core.memory_model import list_memories
    from agent_notes.core.work_item_model import WorkItemModel

    now = datetime.now(timezone.utc)
    full = getattr(args, "full", False)
    limit = args.limit if args.limit is not None else (FULL_LIMIT if full else DEFAULT_LIMIT)
    stale_after_days = getattr(args, "stale_after_days", DEFAULT_STALE_AFTER_DAYS)
    # One extra row distinguishes a clipped sample from a complete result.
    open_wis: list[dict] = []
    changes: list = []
    memories: list[dict] = []
    reflections: list[dict] = []
    since = now - timedelta(days=args.days)
    if proj_id is not None:
        open_wis = WorkItemModel.query_work_items(project_id=proj_id, is_open=True, limit=limit + 1)
        changes = changes_since(since, workspace_id=ws_id, project_id=proj_id, limit=limit + 1)
        memories = list_memories(workspace_id=ws_id, project_id=proj_id, limit=limit + 1)
        reflections = list_memories(
            workspace_id=ws_id, project_id=proj_id, memory_type="reflection", limit=1
        )
    has_more = {
        "open_work_items": len(open_wis) > limit,
        "recent_changes": len(changes) > limit,
        "memories": len(memories) > limit,
    }
    open_wis, changes, memories = open_wis[:limit], changes[:limit], memories[:limit]
    tier_limit = (
        args.limit if args.limit is not None else (FULL_LIMIT if full else TIER_DEFAULT_LIMIT)
    )
    tiers = _vm_and_global_tiers(
        ws_id, tier_limit, now, stale_after_days, getattr(args, "vm", None)
    )
    has_more.update(tiers.pop("has_more"))
    notices, has_more["notices"], notices_error = _notices(
        ws_id, proj_slug, tiers["vm"], tier_limit, now
    )

    def memory_summary(memory: dict) -> dict[str, Any]:
        return {
            "name": memory["name"],
            "type": memory["memory_type"],
            **_freshness(memory, now, stale_after_days),
        }

    # Optionally surface silent-resolution drift: open breadcrumbs that a recent
    # commit already closed via its message but that nobody transitioned in the
    # DB. Off by default — `orient` stays git-free and cheap (decision 15 keeps
    # git out of the default path). Enable with `--reconcile`; the natural place
    # is the SessionStart hook, configured once, so every session gets the check
    # without any per-session agent action. Read-only and fail-safe.
    drift: dict[str, dict[str, str]] = {}
    if getattr(args, "reconcile", False) and proj_id is not None:
        from agent_notes.core.db import list_projects
        from agent_notes.core.git_reconcile import scan_git_for_resolutions

        _proj = next((p for p in list_projects(workspace_id=ws_id) if p.id == proj_id), None)
        _repo_root = _proj.repo_root if _proj else None
        _proj_slug = _proj.slug if _proj else None
        drift = scan_git_for_resolutions(
            _repo_root,
            [wi["identifier"] for wi in open_wis],
            lookback=200,
            project_slug=_proj_slug,
        )

    # Plan 020 WI-3.2: optional bounded recall from the configured memory
    # engine. Additive and never-blocking — orient always succeeds even if
    # recall fails. Lazy imports keep the default (no --recall) path cheap.
    recall_notes: list[str] = []
    learned_context: dict[str, Any] | None = None

    if getattr(args, "recall", False) and proj_id is not None:
        from agent_notes.core.memory_engine import (
            EngineHealthState,
            MemoryScope,
            RecallQuery,
            get_engine,
        )

        try:
            engine = get_engine()
        except ValueError as exc:
            # A retired engine setting: name it, so it isn't a silent fallback.
            recall_notes.append(f"recall: skipped — {exc}")
        except Exception as exc:
            recall_notes.append(f"recall: failed — {type(exc).__name__}")
        else:
            try:
                health = engine.describe()
            except Exception:
                health = None

            if health is not None and health.state == EngineHealthState.NOT_CONFIGURED:
                pass  # skip silently — same as native
            elif health is not None and health.state == EngineHealthState.UNREACHABLE:
                recall_notes.append("recall: engine unreachable")
            elif engine.engine_name == "native":
                recall_notes.append(
                    "recall: skipped (native engine — memories already listed above)"
                )
            else:
                try:
                    rq = RecallQuery(
                        query=(
                            f"important context for project {proj_slug}: "
                            "recent decisions, open work items, and key memories"
                        ),
                        scope=MemoryScope(
                            project_slug=proj_slug,
                            workspace_slug=ws_slug,
                        ),
                        budget="low",
                        max_tokens=2048,
                    )
                    response = engine.recall(rq)

                    if response.usage.get("error"):
                        recall_notes.append(f"recall: degraded — {response.usage['error']}")
                    elif not response.results:
                        recall_notes.append("recall: no learned context found")
                    else:
                        capped = response.results[:8]
                        learned_context = {
                            "engine": response.engine,
                            "results": [r.to_dict() for r in capped],
                            "total_results": len(response.results),
                            "capped": len(response.results) > len(capped),
                            "usage": response.usage,
                            "error": None,
                        }
                except Exception as exc:
                    recall_notes.append(f"recall: failed — {type(exc).__name__}")

    payload = {
        "project": proj_slug,
        "workspace": ws_slug,
        "generated_at": now.isoformat(),
        "freshness_basis": "record_updated_at; age does not verify accuracy",
        "stale_after_days": stale_after_days,
        "has_more": has_more,
        "since_days": args.days,
        "open_work_items": [
            {
                "identifier": wi["identifier"],
                "title": wi["title"],
                "severity": wi["severity"],
                "status": wi["status"],
                **_freshness(wi, now, stale_after_days),
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
        "memories": [memory_summary(m) for m in memories],
        **tiers,
        "notices": notices,
        "notices_error": notices_error,
        "latest_reflection": memory_summary(reflections[0]) if reflections else None,
        "resolved_in_git": [
            {"identifier": ident, "commit": info["commit"], "subject": info["subject"]}
            for ident, info in sorted(drift.items())
        ],
    }

    if learned_context is not None:
        payload["learned_context"] = learned_context

    if use_json:
        print(json.dumps(payload, indent=2, default=str))
    elif proj_slug is None:
        print(f"Orientation (no registered project here; workspace {ws_slug}):")
        print(f"As of {now.isoformat()} — recorded ages, not verified accuracy.")
        print(f"Aged: last updated at least {stale_after_days}d ago.")
        _print_notices(payload)
        _print_tier_sections(payload)
    else:
        print(f"Orientation for {proj_slug} (workspace {ws_slug}):")
        print(f"As of {now.isoformat()} — recorded ages, not verified accuracy.")
        print(f"Aged: last updated at least {stale_after_days}d ago.")
        # Before the long sections, so the hooks' length cap keeps it.
        _print_notices(payload)
        print(f"\nOpen work items ({_shown_label(open_wis, has_more['open_work_items'])}):")
        for wi in payload["open_work_items"]:
            print(
                f"  - [{wi['severity']}] {wi['identifier']} ({wi['status']}) — "
                f"{_one_line(wi['title'])} [{_age_label(wi)}]"
            )
        if drift:
            print(
                f"\n⚠ Resolved in git but still open in the DB ({len(drift)}) — "
                "run 'agent-notes breadcrumb reconcile --apply':"
            )
            for ident, info in sorted(drift.items()):
                print(f"  - {ident} — {info['commit']} {info['subject']!r}")
        print(
            f"\nChanges in last {args.days}d "
            f"({_shown_label(changes, has_more['recent_changes'], sampled=not full)}):"
        )
        if full:
            for c in changes:
                print(f"  - [{c.kind}] {c.identifier} {c.event} @ {c.changed_at}")
        print(f"\nMemories ({_shown_label(memories, has_more['memories'])}):")
        for m in payload["memories"]:
            print(f"  - {_one_line(m['name'])} ({m['type']}; {_age_label(m)})")
        reflection = payload["latest_reflection"]
        if reflection:
            print(f"Latest handoff: {_one_line(reflection['name'])} ({_age_label(reflection)})")
        else:
            print("Latest handoff: no reflection recorded.")
        _print_tier_sections(payload)
        for note in recall_notes:
            print(f"\n{note}")
        if learned_context is not None:
            print(f"\nLearned context ({learned_context['engine']}):")
            for r in learned_context["results"]:
                origin = OriginClass(r["origin"])
                label = _origin_label(origin)
                print(f"  - [{label}] {_one_line(r['text'])}")
    return EXIT_SUCCESS


def register_orient_parser(sub: argparse._SubParsersAction) -> None:
    orient = sub.add_parser(
        "orient",
        help="One-call session digest: open breadcrumbs, recent changes, memories "
        "(project, VM and global tiers)",
    )
    orient.add_argument(
        "--days", type=_positive_int, default=7, help="Recent-changes window (days)"
    )
    orient.add_argument(
        "--limit",
        type=_section_limit,
        default=None,
        help="Entries per section, 1–100 (default: 5, or 15 with --full)",
    )
    orient.add_argument(
        "--full",
        action="store_true",
        help="Show more entries and individual recent changes",
    )
    orient.add_argument(
        "--stale-after-days",
        type=_positive_int,
        default=DEFAULT_STALE_AFTER_DAYS,
        help="Label recorded updates this many days old as aged (default: 30)",
    )
    orient.add_argument(
        "--reconcile",
        action="store_true",
        help="Also scan git history for open breadcrumbs already resolved in a "
        "commit (read-only; off by default). Ideal in the SessionStart hook.",
    )
    orient.add_argument(
        "--recall",
        action="store_true",
        help="Query the configured memory engine for learned context (Plan 020 "
        "WI-3.2). Bounded and additive — never blocks. Off by default.",
    )
    orient.add_argument(
        "--vm",
        default=None,
        help="VM ID for the VM memory section (default: AGENT_NOTES_VM, else the hostname)",
    )
    _add_common(orient)
    orient.set_defaults(func=cmd_orient)
