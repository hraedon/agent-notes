from __future__ import annotations

import argparse
import json
import sys
from typing import Any

EXIT_SUCCESS = 0
EXIT_GENERIC = 1
EXIT_NOT_FOUND = 2
EXIT_NOT_CONFIGURED = 3
EXIT_CONFLICT = 4


def _resolve(
    ws_slug: str | None, proj_slug: str | None, path: str | None
) -> tuple[int, int, str, str]:
    from agent_notes.core.db import list_projects, list_workspaces

    if path:
        from agent_notes.core.db import resolve_project as db_resolve_project

        try:
            result = db_resolve_project(path)
        except ValueError as exc:
            raise SystemExit(EXIT_NOT_CONFIGURED) from exc
        ws = next((w for w in list_workspaces() if w.slug == result["workspace"]), None)
        if ws is None:
            raise SystemExit(EXIT_NOT_CONFIGURED)
        proj = next(
            (p for p in list_projects(workspace_id=ws.id) if p.slug == result["project"]), None
        )
        if proj is None:
            raise SystemExit(EXIT_NOT_CONFIGURED)
        return ws.id, proj.id, ws.slug, proj.slug

    if ws_slug and proj_slug:
        ws = next((w for w in list_workspaces() if w.slug == ws_slug), None)
        if ws is None:
            available = [w.slug for w in list_workspaces()]
            hint = f" Available: {', '.join(available)}" if available else ""
            suggestion = " Use --path for auto-resolution or run 'agent-notes workspace list'."
            print(f"Workspace '{ws_slug}' not found.{hint}{suggestion}", file=sys.stderr)
            raise SystemExit(EXIT_NOT_FOUND)
        proj = next((p for p in list_projects(workspace_id=ws.id) if p.slug == proj_slug), None)
        if proj is None:
            available = [p.slug for p in list_projects(workspace_id=ws.id)]
            hint = f" Available in '{ws_slug}': {', '.join(available)}" if available else ""
            print(f"Project '{proj_slug}' not found.{hint}", file=sys.stderr)
            raise SystemExit(EXIT_NOT_FOUND)
        return ws.id, proj.id, ws.slug, proj.slug

    raise SystemExit(EXIT_NOT_CONFIGURED)


def report_resolution_failure(
    args: argparse.Namespace, code: int, use_json: bool | None = None
) -> None:
    """Emit a structured, non-silent error when project/workspace resolution fails.

    Commands that resolve a project catch the resolution ``SystemExit`` and
    return a bare exit code. Without this reporter they print nothing — and in
    ``--json`` mode a caller parsing stdout reads the empty output as "no
    results found" rather than "lookup failed". That silent failure is exactly
    how a duplicate breadcrumb gets filed against an unregistered project.

    ``use_json`` defaults to ``args.json``; pass it explicitly for JSON-native
    commands (e.g. ``export``) that have no ``--json`` flag.
    """
    if use_json is None:
        use_json = getattr(args, "json", False)
    detail: str | None = None
    path = getattr(args, "path", None)
    if path:
        from agent_notes.core.db import resolve_project

        try:
            resolve_project(path)
        except ValueError as exc:
            detail = str(exc)
        except Exception:  # pragma: no cover - defensive: DB/transport issue
            detail = None
    if detail is None:
        detail = (
            "could not resolve a project/workspace. Pass --path to a registered "
            "repo, or --workspace/--project, or use --scope global."
        )
    if use_json:
        print(json.dumps({"error": detail, "code": code}, indent=2))
    else:
        print(f"Error: {detail}", file=sys.stderr)


def _output(data: Any, use_json: bool) -> None:
    if use_json:
        from importlib.metadata import version
        payload = {"version": version("agent-notes"), **data}
        print(json.dumps(payload, indent=2, default=str, ensure_ascii=False))
    else:
        print(data)


def _bc_format(bc: dict) -> str:
    lines = [
        f"**{bc['identifier']}** — {bc['title']}",
        f"Kind: {bc['kind']} | Status: {bc['status']} | Severity: {bc['severity']}",
        f"Created: {bc['created_at']}",
        f"Updated: {bc['updated_at']}",
        "",
        bc.get("body", ""),
    ]
    return "\n".join(lines)


def _print_sub_help(parser: argparse.ArgumentParser) -> None:
    parser.print_help()


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--path", default=None, help="Filesystem path for project resolution")
    p.add_argument("--workspace", default=None, help="Workspace slug (optional if --path given)")
    p.add_argument("--project", default=None, help="Project slug (optional if --path given)")
    p.add_argument("--json", action="store_true", help="Emit JSON instead of human text")
