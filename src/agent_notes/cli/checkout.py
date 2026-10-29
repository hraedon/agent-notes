"""`agent-notes checkout list|add|scan`: per-VM project checkouts (Plan 025 WI-2.6)."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from agent_notes.cli.common import (
    EXIT_CONFLICT,
    EXIT_GENERIC,
    EXIT_NOT_CONFIGURED,
    EXIT_NOT_FOUND,
    EXIT_SUCCESS,
    emit_error,
)
from agent_notes.core import checkouts
from agent_notes.core.config import VmIdError, resolve_vm_id
from agent_notes.core.db import Project, get_project, list_projects, list_workspaces

DEFAULT_WORKSPACE = "default"


class _CheckoutError(Exception):
    def __init__(self, code: str, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


def _print_json(data: dict[str, Any]) -> None:
    print(json.dumps(data, indent=2, default=str, ensure_ascii=False))


def _vm(args: argparse.Namespace) -> str:
    try:
        return resolve_vm_id(getattr(args, "vm", None))
    except VmIdError as exc:
        raise _CheckoutError("VM_NOT_RESOLVED", str(exc), EXIT_NOT_CONFIGURED) from exc


def _workspace_id(slug: str) -> int:
    ws = next((w for w in list_workspaces() if w.slug == slug), None)
    if ws is None:
        raise _CheckoutError("NOT_FOUND", f"Workspace {slug!r} not found.", EXIT_NOT_FOUND)
    return ws.id


def _project(workspace: str, slug: str) -> Project:
    proj = get_project(_workspace_id(workspace), slug)
    if proj is None:
        raise _CheckoutError(
            "NOT_FOUND",
            f"Project {slug!r} not found in workspace {workspace!r}. "
            "Register it first with `agent-notes init`.",
            EXIT_NOT_FOUND,
        )
    return proj


def _guarded(func):
    def wrapper(args: argparse.Namespace) -> int:
        use_json = bool(getattr(args, "json", False))
        try:
            return func(args)
        except _CheckoutError as exc:
            return emit_error(exc.code, str(exc), use_json=use_json, exit_code=exc.exit_code)
        except Exception as exc:
            import psycopg

            if not isinstance(exc, psycopg.Error):
                raise
            return emit_error(
                "STORE_UNAVAILABLE",
                f"database error: {exc}",
                use_json=use_json,
                retryable=isinstance(exc, psycopg.OperationalError),
                exit_code=EXIT_GENERIC,
            )

    wrapper.__name__ = func.__name__
    return wrapper


@_guarded
def cmd_checkout_list(args: argparse.Namespace) -> int:
    project_id = None
    if args.project:
        project_id = _project(args.workspace, args.project).id
    rows = checkouts.list_checkouts(project_id=project_id, vm_id=args.vm)
    if args.json:
        _print_json({"checkouts": rows})
        return EXIT_SUCCESS
    if not rows:
        print("No checkouts registered.")
        return EXIT_SUCCESS
    for r in rows:
        print(f"{r['workspace']}/{r['project']}  {r['vm_id']}  {r['path']}")
    return EXIT_SUCCESS


@_guarded
def cmd_checkout_add(args: argparse.Namespace) -> int:
    vm_id = _vm(args)
    proj = _project(args.workspace, args.slug)
    path = checkouts.canonical_path(args.path)
    owner = checkouts.find_project_by_checkout(path, vm_id)
    if owner and owner["resolved_via"] == "exact" and owner["project_id"] != proj.id:
        raise _CheckoutError(
            "CHECKOUT_CONFLICT",
            f"{path!r} is already registered on {vm_id} for project {owner['project']!r}.",
            EXIT_CONFLICT,
        )
    checkouts.register_checkout(proj.id, vm_id, path)
    exists = os.path.isdir(path)
    if args.json:
        _print_json(
            {
                "ok": True,
                "project": proj.slug,
                "workspace": args.workspace,
                "vm_id": vm_id,
                "path": path,
                "exists": exists,
            }
        )
        return EXIT_SUCCESS
    print(f"Checkout registered: {args.workspace}/{proj.slug} on {vm_id} at {path}")
    if not exists:
        print(f"Warning: {path} is not a directory on this machine.")
    return EXIT_SUCCESS


def _git_toplevel(directory: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = proc.stdout.strip()
    return out if proc.returncode == 0 and out else None


def plan_scan(
    scan_dir: str,
    projects: list[Project],
    existing: list[dict[str, Any]],
    *,
    include_worktrees: bool = False,
) -> list[dict[str, Any]]:
    """Plan which immediate subdirectories of *scan_dir* become checkouts.

    Each entry carries ``status``: ``add`` (will be registered), ``exists``
    (already registered for that project), ``conflict`` (path belongs to
    another project; never moved by a scan), ``unmatched`` (a git repo whose
    toplevel basename is no project), ``skipped_worktree`` or ``not_git``.
    """
    by_slug = {p.slug.lower(): p for p in projects}
    owners = {checkouts.match_key(r["path"]): r["project_id"] for r in existing}
    plan: list[dict[str, Any]] = []
    for child in sorted(Path(scan_dir).iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir():
            continue
        entry: dict[str, Any] = {"dir": str(child), "path": checkouts.canonical_path(str(child))}
        dotgit = child / ".git"
        if not dotgit.exists():
            plan.append({**entry, "status": "not_git"})
            continue
        if dotgit.is_file() and not include_worktrees:
            plan.append({**entry, "status": "skipped_worktree"})
            continue
        toplevel = _git_toplevel(child) or str(child)
        slug = Path(toplevel).name
        proj = by_slug.get(slug.lower())
        entry["toplevel"] = toplevel
        if proj is None:
            plan.append({**entry, "status": "unmatched", "slug": slug})
            continue
        owner = owners.get(checkouts.match_key(entry["path"]))
        if owner is None:
            status = "add"
        elif owner == proj.id:
            status = "exists"
        else:
            status = "conflict"
        plan.append({**entry, "status": status, "project": proj.slug, "project_id": proj.id})
    return plan


@_guarded
def cmd_checkout_scan(args: argparse.Namespace) -> int:
    vm_id = _vm(args)
    scan_dir = os.path.abspath(args.dir)
    if not os.path.isdir(scan_dir):
        raise _CheckoutError("NOT_FOUND", f"{scan_dir!r} is not a directory.", EXIT_NOT_FOUND)
    ws_id = _workspace_id(args.workspace)
    projects = list_projects(ws_id)
    existing = checkouts.list_checkouts(vm_id=vm_id)
    plan = plan_scan(scan_dir, projects, existing, include_worktrees=args.include_worktrees)
    if args.apply:
        for e in plan:
            if e["status"] == "add":
                checkouts.register_checkout(e["project_id"], vm_id, e["path"])
                e["applied"] = True
    if args.json:
        _print_json({"vm_id": vm_id, "dir": scan_dir, "apply": bool(args.apply), "entries": plan})
        return EXIT_SUCCESS
    verb = "registered" if args.apply else "would register"
    print(f"Scanned {scan_dir} for {vm_id}:")
    for e in plan:
        if e["status"] == "add":
            print(f"  {verb}: {e['project']}  <- {e['path']}")
        elif e["status"] == "exists":
            print(f"  already registered: {e['project']}  <- {e['path']}")
        elif e["status"] == "conflict":
            print(f"  CONFLICT (path belongs to another project, not moved): {e['path']}")
        elif e["status"] == "unmatched":
            print(f"  no project named {e['slug']!r}: {e['path']}")
        elif e["status"] == "skipped_worktree":
            print(f"  skipped worktree: {e['path']}")
    skipped = sum(1 for e in plan if e["status"] == "not_git")
    if skipped:
        print(f"  ({skipped} non-git director{'y' if skipped == 1 else 'ies'} ignored)")
    if not args.apply and any(e["status"] == "add" for e in plan):
        print("Dry run. Re-run with --apply to register.")
    return EXIT_SUCCESS


def register_checkout_parsers(sub: argparse._SubParsersAction) -> None:
    checkout = sub.add_parser(
        "checkout", help="Per-VM project checkouts (where each machine keeps a project)"
    )
    csub = checkout.add_subparsers(dest="checkout_cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--workspace", default=DEFAULT_WORKSPACE, help="Workspace slug (default: default)"
        )
        p.add_argument("--json", action="store_true")

    lst = csub.add_parser("list", help="List registered checkouts")
    common(lst)
    lst.add_argument("--project", default=None, help="Only this project slug")
    lst.add_argument("--vm", default=None, help="Only this VM (default: all VMs)")
    lst.set_defaults(func=cmd_checkout_list)

    add = csub.add_parser("add", help="Register a path as this VM's checkout of a project")
    common(add)
    add.add_argument("slug", help="Project slug")
    add.add_argument("path", help="Checkout directory")
    add.add_argument("--vm", default=None, help="VM ID (default: this machine)")
    add.set_defaults(func=cmd_checkout_add)

    scan = csub.add_parser(
        "scan",
        help="Map git repos under a directory to existing projects (dry run unless --apply)",
    )
    common(scan)
    scan.add_argument("dir", help="Directory whose immediate subdirectories are checked")
    scan.add_argument("--apply", action="store_true", help="Register the planned checkouts")
    scan.add_argument(
        "--include-worktrees",
        action="store_true",
        help="Also consider git worktrees (a .git file rather than a directory)",
    )
    scan.add_argument("--vm", default=None, help="VM ID (default: this machine)")
    scan.set_defaults(func=cmd_checkout_scan)
