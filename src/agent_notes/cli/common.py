from __future__ import annotations

import argparse
import json
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
            raise SystemExit(EXIT_NOT_FOUND)
        proj = next((p for p in list_projects(workspace_id=ws.id) if p.slug == proj_slug), None)
        if proj is None:
            raise SystemExit(EXIT_NOT_FOUND)
        return ws.id, proj.id, ws.slug, proj.slug

    raise SystemExit(EXIT_NOT_CONFIGURED)


def _output(data: Any, use_json: bool) -> None:
    if use_json:
        print(json.dumps(data, indent=2, default=str, ensure_ascii=False))
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


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--path", default=None, help="Filesystem path for project resolution")
    p.add_argument("--workspace", default=None, help="Workspace slug (optional if --path given)")
    p.add_argument("--project", default=None, help="Project slug (optional if --path given)")
    p.add_argument("--json", action="store_true", help="Emit JSON instead of human text")
