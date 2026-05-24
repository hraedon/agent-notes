"""agent-notes CLI (Plan 004 Phase 9a).

Noun/verb argparse tree: `agent-notes breadcrumb file`, `agent-notes memory add`, etc.
All commands accept `--path` (default cwd) and resolve via `core.db.resolve_project`.
`--json` produces machine-parseable output. Stable exit codes per decision 52.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Any

# ---------------------------------------------------------------------------
# Exit codes (decision 52)
# ---------------------------------------------------------------------------

EXIT_SUCCESS = 0
EXIT_GENERIC = 1
EXIT_NOT_FOUND = 2
EXIT_NOT_CONFIGURED = 3
EXIT_CONFLICT = 4


def _resolve(
    ws_slug: str | None, proj_slug: str | None, path: str | None
) -> tuple[int, int, str, str]:
    """Return (workspace_id, project_id, workspace_slug, project_slug).

    If --path is given, resolve via core.db.resolve_project (overrides explicit args).
    If explicit workspace/project are given, use them.
    """
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


# ---------------------------------------------------------------------------
# init / resolve / doctor
# ---------------------------------------------------------------------------


def cmd_init(path: str | None) -> int:
    import os

    target = path or "."
    target = os.path.abspath(target)

    # Walk up to find a git root (or use path itself if none found)
    git_root = target
    while git_root != "/":
        if os.path.isdir(os.path.join(git_root, ".git")):
            break
        parent = os.path.dirname(git_root)
        if parent == git_root:
            break
        git_root = parent

    if not os.path.isdir(os.path.join(git_root, ".git")):
        print(f"Note: no git repo found above {target}; using {target} as repo root.")
        git_root = target

    repo_root = os.path.abspath(git_root)
    name = os.path.basename(repo_root)

    from agent_notes.core.db import get_or_create_project, get_or_create_workspace

    ws = get_or_create_workspace("default", "Default Workspace")
    get_or_create_project(ws.id, slug=name, name=name, repo_root=repo_root)
    print(f"Project '{name}' registered (workspace=default, repo_root={repo_root}).")
    return EXIT_SUCCESS


def cmd_resolve(path: str | None, use_json: bool) -> int:
    from agent_notes.core.db import resolve_project as db_resolve_project

    target = os.path.abspath(path or ".")
    try:
        result = db_resolve_project(target)
    except ValueError as exc:
        if use_json:
            print(json.dumps({"error": str(exc), "code": EXIT_NOT_CONFIGURED}))
        else:
            print(exc)
        return EXIT_NOT_CONFIGURED
    _output(result, use_json)
    return EXIT_SUCCESS


def cmd_doctor(use_json: bool) -> int:
    from agent_notes.scripts.doctor import run as doctor_run

    code = doctor_run()
    if code != 0 and use_json:
        print(json.dumps({"status": "unhealthy", "doctor_exit": code}))
    elif use_json:
        print(json.dumps({"status": "healthy"}))
    return code


# ---------------------------------------------------------------------------
# breadcrumb
# ---------------------------------------------------------------------------


def cmd_bc_file(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED  # type: ignore[arg-type]

    from agent_notes.core.breadcrumbs_model import BreadcrumbModel
    from agent_notes.core.embed import embed

    vec = embed(args.title + " " + (args.body or ""), task="document").tolist()
    try:
        bc = BreadcrumbModel.file_breadcrumb(
            project_id=proj_id,
            identifier=args.identifier,
            title=args.title,
            body=args.body or "",
            kind=args.kind,
            status=args.status,
            severity=args.severity or "medium",
            external_refs=args.external_refs,
            diagnostic_keys=args.diagnostic_keys,
            embedding=vec,
        )
    except ValueError as exc:
        if use_json:
            print(json.dumps({"error": str(exc)}, indent=2))
        else:
            print(f"Error: {exc}")
        return EXIT_CONFLICT

    if use_json:
        print(json.dumps({"breadcrumb": bc}, indent=2, default=str))
    else:
        ident = bc['identifier']
        kind = bc['kind']
        status = bc['status']
        print(
            f"Breadcrumb filed: **{ident}** ({kind} / {status}) in project {proj_slug}"
        )
    return EXIT_SUCCESS


def cmd_bc_update(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED  # type: ignore[arg-type]

    from agent_notes.core.breadcrumbs_model import BreadcrumbModel
    from agent_notes.core.embed import embed

    fields: dict[str, Any] = {}
    if args.title is not None:
        fields["title"] = args.title
    if args.body is not None:
        fields["body"] = args.body
    if args.kind is not None:
        fields["kind"] = args.kind
    if args.status is not None:
        fields["status"] = args.status
    if args.severity is not None:
        fields["severity"] = args.severity

    if "body" in fields or "title" in fields:
        old = BreadcrumbModel.get_breadcrumb(proj_id, args.identifier)
        text = (
            fields.get("title", old.get("title", ""))
            + " "
            + fields.get("body", old.get("body", ""))
        )
        fields["embedding"] = embed(text, task="document").tolist()

    try:
        bc = BreadcrumbModel.update_breadcrumb(
            project_id=proj_id,
            identifier=args.identifier,
            **fields,
        )
    except ValueError as exc:
        if use_json:
            print(json.dumps({"error": str(exc)}, indent=2))
        else:
            print(f"Error: {exc}")
        return EXIT_NOT_FOUND

    if use_json:
        print(json.dumps({"breadcrumb": bc}, indent=2, default=str))
    else:
        print(f"Breadcrumb updated: **{bc['identifier']}** ({bc['kind']} / {bc['status']})")
    return EXIT_SUCCESS


def cmd_bc_get(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED  # type: ignore[arg-type]

    from agent_notes.core.breadcrumbs_model import BreadcrumbModel

    bc = BreadcrumbModel.get_breadcrumb(proj_id, args.identifier)
    if bc is None:
        if use_json:
            print(json.dumps({"error": "not found"}, indent=2))
        else:
            print(f"Breadcrumb '{args.identifier}' not found.")
        return EXIT_NOT_FOUND

    if use_json:
        print(json.dumps({"breadcrumb": bc}, indent=2, default=str))
    else:
        print(_bc_format(bc))
    return EXIT_SUCCESS


def cmd_bc_find(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED  # type: ignore[arg-type]

    from agent_notes.core.breadcrumbs_model import BreadcrumbModel
    from agent_notes.core.embed import embed

    if args.text:
        vec = embed(args.text, task="query").tolist()
        rows = BreadcrumbModel.find_breadcrumbs(
            query_vec=vec,
            project_id=proj_id,
            workspace_id=ws_id,
            limit=min(args.limit or 10, 50),
        )
    else:
        rows = BreadcrumbModel.query_breadcrumbs(
            project_id=proj_id,
            workspace_id=ws_id,
            status=args.status,
            kind=args.type,
            limit=min(args.limit or 50, 200),
        )

    if use_json:
        print(json.dumps({"breadcrumbs": rows}, indent=2, default=str))
    else:
        if not rows:
            print("No breadcrumbs found.")
        else:
            print(f"{len(rows)} breadcrumb(s) found:")
            for r in rows:
                print(f"- **{r['identifier']}** ({r['kind']} / {r['status']}) — {r['title']}")
    return EXIT_SUCCESS


def cmd_bc_query(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED  # type: ignore[arg-type]

    from agent_notes.core.breadcrumbs_model import BreadcrumbModel

    rows = BreadcrumbModel.query_breadcrumbs(
        project_id=proj_id,
        workspace_id=ws_id,
        limit=min(args.limit or 50, 200),
    )
    if use_json:
        print(json.dumps({"breadcrumbs": rows}, indent=2, default=str))
    else:
        if not rows:
            print("No breadcrumbs found.")
        else:
            for r in rows:
                print(f"- {r['identifier']} | {r['title']} | {r['kind']} | {r['status']}")
    return EXIT_SUCCESS


# ---------------------------------------------------------------------------
# memory
# ---------------------------------------------------------------------------


def cmd_mem_add(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED  # type: ignore[arg-type]

    from agent_notes.core.embed import embed
    from agent_notes.core.memory_model import add_memory

    vec = embed(args.body, task="document").tolist()
    try:
        mem = add_memory(
            workspace_id=ws_id,
            project_id=proj_id,
            name=args.name,
            memory_type=args.type,
            body=args.body,
            attributes=args.attributes or {},
            embedding=vec,
        )
    except ValueError as exc:
        if use_json:
            print(json.dumps({"error": str(exc)}, indent=2))
        else:
            print(f"Error: {exc}")
        return EXIT_CONFLICT

    if use_json:
        print(json.dumps({"memory": mem}, indent=2, default=str))
    else:
        old_id = mem.get("supersedes")
        superseded_msg = f" (superseded id={old_id})" if old_id else ""
        mem_name = mem['name']
        mem_id = mem['id']
        mem_type = mem['memory_type']
        print(
            f"Memory '{mem_name}' added (id={mem_id}, type={mem_type}){superseded_msg}"
        )
    return EXIT_SUCCESS


def cmd_mem_get(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED  # type: ignore[arg-type]

    from agent_notes.core.memory_model import get_memory

    mem = get_memory(ws_id, proj_id, args.name)
    if mem is None:
        if use_json:
            print(json.dumps({"error": "not found"}, indent=2))
        else:
            print(f"Memory '{args.name}' not found.")
        return EXIT_NOT_FOUND

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
        return exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED  # type: ignore[arg-type]

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
        return exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED  # type: ignore[arg-type]

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
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED  # type: ignore[arg-type]

    from agent_notes.core.memory_model import delete_memory

    row = delete_memory(ws_id, proj_id, args.name)
    if row is None:
        print(f"Memory '{args.name}' not found (or already deleted).")
        return EXIT_NOT_FOUND
    print(f"Memory '{args.name}' soft-deleted (id={row['id']}).")
    return EXIT_SUCCESS


# ---------------------------------------------------------------------------
# link
# ---------------------------------------------------------------------------


def _parse_link_ref(ref: str) -> tuple[str, str, str, str]:
    """Parse `kind:workspace/project/identifier` into (kind, workspace, project, identifier)."""
    parts = ref.split(":", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid link ref: {ref!r}; expected kind:workspace/project/identifier")
    kind = parts[0]
    path = parts[1]
    path_parts = path.split("/")
    if len(path_parts) != 3:
        raise ValueError(f"Invalid link path: {path!r}; expected workspace/project/identifier")
    return kind, path_parts[0], path_parts[1], path_parts[2]


def _resolve_link_ref(
    kind: str, workspace_slug: str, project_slug: str, identifier: str
) -> tuple[int, int, int]:
    """Return (workspace_id, project_id, kind_str)."""
    from agent_notes.core.db import list_projects, list_workspaces

    ws = next((w for w in list_workspaces() if w.slug == workspace_slug), None)
    if ws is None:
        raise ValueError(f"workspace '{workspace_slug}' not found")
    proj = next((p for p in list_projects(workspace_id=ws.id) if p.slug == project_slug), None)
    if proj is None:
        raise ValueError(f"project '{project_slug}' not found")
    return ws.id, proj.id, identifier


def cmd_link_add(args: argparse.Namespace) -> int:
    fk, fwslug, fproj, fid = _parse_link_ref(args.from_)
    tk, twslug, tproj, tid = _parse_link_ref(args.to)
    from_ws, from_proj, from_id = _resolve_link_ref(fk, fwslug, fproj, fid)
    to_ws, to_proj, to_id = _resolve_link_ref(tk, twslug, tproj, tid)

    from agent_notes.core.links import add_link

    add_link(
        from_kind=fk,
        from_workspace=from_ws,
        from_project=from_proj,
        from_identifier=from_id,
        to_kind=tk,
        to_workspace=to_ws,
        to_project=to_proj,
        to_identifier=to_id,
        relationship=args.rel_type,
    )
    print(f"Link added: {fk}/{from_id} --{args.rel_type}--> {tk}/{to_id}")
    return EXIT_SUCCESS


def cmd_link_remove(args: argparse.Namespace) -> int:
    fk, fwslug, fproj, fid = _parse_link_ref(args.from_)
    tk, twslug, tproj, tid = _parse_link_ref(args.to)
    from_ws, from_proj, from_id = _resolve_link_ref(fk, fwslug, fproj, fid)
    to_ws, to_proj, to_id = _resolve_link_ref(tk, twslug, tproj, tid)

    from agent_notes.core.links import remove_link

    removed = remove_link(
        from_kind=fk,
        from_workspace=from_ws,
        from_project=from_proj,
        from_identifier=from_id,
        to_kind=tk,
        to_workspace=to_ws,
        to_project=to_proj,
        to_identifier=to_id,
        relationship=args.rel_type,
    )
    if removed:
        print(f"Link removed: {fk}/{from_id} --{args.rel_type}--> {tk}/{to_id}")
    else:
        print("No such link found.")
        return EXIT_NOT_FOUND
    return EXIT_SUCCESS


def cmd_link_trace(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    kind, ws_slug, proj_slug, identifier = _parse_link_ref(args.start)
    ws, proj, _ = _resolve_link_ref(kind, ws_slug, proj_slug, identifier)

    from agent_notes.core.links import trace_graph as core_local_trace_graph

    nodes = core_local_trace_graph(
        kind=kind,
        workspace=ws,
        project=proj,
        identifier=identifier,
        direction=args.direction,
        max_depth=min(args.depth or 3, 10),
    )
    if args.all:
        from agent_notes.core.search import trace_graph_all

        nodes = trace_graph_all(
            kind=kind,
            workspace=ws,
            project=proj,
            identifier=identifier,
            direction=args.direction,
            max_depth=min(args.depth or 3, 10),
        )

    if use_json:
        print(
            json.dumps(
                {
                    "nodes": [
                        {
                            "kind": n.kind,
                            "identifier": n.identifier,
                            "relationship": n.relationship,
                            "depth": n.depth,
                            "title": n.title,
                            "status": n.status,
                        }
                        for n in nodes
                    ]
                },
                indent=2,
                default=str,
            )
        )
    else:
        if not nodes:
            print(f"No linked notes found for {kind}/{identifier}.")
        else:
            print(f"{len(nodes)} linked note(s):")
            for n in nodes:
                print(
                    f"- [{n.kind}] {n.identifier} (relationship={n.relationship}, depth={n.depth})"
                )
    return EXIT_SUCCESS


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def cmd_search_all(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    try:
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED  # type: ignore[arg-type]

    from agent_notes.core.embed import embed
    from agent_notes.core.search import search_all_notes

    vec = embed(args.query, task="query").tolist()
    rows = search_all_notes(
        query_vec=vec,
        workspace_ids=[ws_id] if ws_id else None,
        project_ids=[proj_id] if proj_id else None,
        limit=min(args.limit or 20, 100),
    )
    if use_json:
        print(json.dumps({"results": rows}, indent=2, default=str))
    else:
        if not rows:
            print("No matching notes found.")
        else:
            print(f"{len(rows)} note(s) matched:")
            for r in rows:
                print(
                    f"- [{r['kind']}] **{r['identifier']}** — {r['title']} (score={r['score']:.3f})"
                )
    return EXIT_SUCCESS


# ---------------------------------------------------------------------------
# vocabulary
# ---------------------------------------------------------------------------


def cmd_vocab_list(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    from agent_notes.core.db import list_vocabulary, list_workspaces

    ws = next((w for w in list_workspaces() if w.slug == args.workspace), None)
    if ws is None:
        print(f"Workspace '{args.workspace}' not found.")
        return EXIT_NOT_FOUND

    vocabs = list_vocabulary(
        ws.id, kind_namespace=args.kind, include_archived=args.include_archived
    )
    if use_json:
        print(json.dumps({"vocabulary": [v.__dict__ for v in vocabs]}, indent=2, default=str))
    else:
        print(f"{len(vocabs)} vocab entry(ies):")
        for v in vocabs:
            print(f"- {v.kind_namespace}/{v.name} (sort={v.sort_order})")
    return EXIT_SUCCESS


def cmd_vocab_archive(args: argparse.Namespace) -> int:
    from agent_notes.core.db import archive_vocabulary, list_workspaces

    ws = next((w for w in list_workspaces() if w.slug == args.workspace), None)
    if ws is None:
        print(f"Workspace '{args.workspace}' not found.")
        return EXIT_NOT_FOUND
    archive_vocabulary(ws.id, args.kind, args.name)
    print(f"Archived vocabulary entry: {args.kind}/{args.name}")
    return EXIT_SUCCESS


# ---------------------------------------------------------------------------
# changes
# ---------------------------------------------------------------------------


def cmd_changes_since(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    from agent_notes.core.change_log import changes_since as cl_changes_since

    try:
        since = datetime.fromisoformat(args.since)
    except ValueError as exc:
        print(f"Invalid timestamp: {exc}")
        return EXIT_GENERIC

    rows = cl_changes_since(since, limit=min(args.limit or 50, 200))
    if use_json:
        print(
            json.dumps(
                {
                    "changes": [
                        {
                            "kind": r.kind,
                            "identifier": r.identifier,
                            "event": r.event,
                            "changed_at": r.changed_at.isoformat(),
                        }
                        for r in rows
                    ]
                },
                indent=2,
                default=str,
            )
        )
    else:
        if not rows:
            print("No changes found.")
        else:
            print(f"{len(rows)} change(s) since {args.since}:")
            for r in rows:
                print(f"- [{r.kind}] {r.identifier} event={r.event} at {r.changed_at}")
    return EXIT_SUCCESS


# ---------------------------------------------------------------------------
# install-skills
# ---------------------------------------------------------------------------


def cmd_install_skills(args: argparse.Namespace) -> int:
    target = args.target or "claude"
    dry_run = args.dry_run
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    skills_src = repo_root / "skills" / target
    if not skills_src.exists():
        print(f"No skills directory found at {skills_src}; creating stub.")
        skills_src.mkdir(parents=True, exist_ok=True)

    if target == "claude":
        skill_dir = Path.home() / ".claude" / "skills"
    else:
        skill_dir = Path.home() / ".config" / "opencode" / "skills"

    if not skill_dir.exists():
        if dry_run:
            print(f"Would create skill directory: {skill_dir}")
        else:
            skill_dir.mkdir(parents=True, exist_ok=True)

    installed = 0
    for src in skills_src.iterdir():
        if src.is_file() and src.suffix == ".md":
            dest = skill_dir / src.name
            if dry_run:
                print(f"Would install: {src} -> {dest}")
            else:
                # Copy instead of symlink for portability
                import shutil

                shutil.copy2(src, dest)
                print(f"Installed: {dest}")
            installed += 1

    print(f"Installed {installed} skill(s) for {target}.")
    return EXIT_SUCCESS


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--path", default=None, help="Filesystem path for project resolution")
    p.add_argument("--workspace", default=None, help="Workspace slug (optional if --path given)")
    p.add_argument("--project", default=None, help="Project slug (optional if --path given)")
    p.add_argument("--json", action="store_true", help="Emit JSON instead of human text")


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="agent-notes",
        description="agent-notes CLI — sync interface to breadcrumbs, memories, and search",
    )
    sub = parser.add_subparsers(dest="command")

    # ------------------------------------------------------------------
    # Top-level commands
    # ------------------------------------------------------------------

    init_p = sub.add_parser("init", help="Idempotently register a project from a path")
    init_p.add_argument("path", nargs="?", default=".")

    resolve_p = sub.add_parser("resolve", help="Resolve a filesystem path to a registered project")
    resolve_p.add_argument("--path", default=".")
    resolve_p.add_argument("--json", action="store_true")

    doctor_p = sub.add_parser("doctor", help="Health check")
    doctor_p.add_argument("--json", action="store_true")

    # ------------------------------------------------------------------
    # breadcrumb
    # ------------------------------------------------------------------
    bc = sub.add_parser("breadcrumb", help="Breadcrumb operations")
    bc_sub = bc.add_subparsers(dest="bc_cmd")

    bc_file = bc_sub.add_parser("file", help="File a breadcrumb")
    bc_file.add_argument("--title", required=True)
    bc_file.add_argument("--body", default="")
    bc_file.add_argument("--identifier", default=None)
    bc_file.add_argument("--kind", default="todo")
    bc_file.add_argument("--status", default="new")
    bc_file.add_argument("--severity", default="medium")
    bc_file.add_argument("--external-refs", default=None)
    bc_file.add_argument("--diagnostic-keys", default=None)
    _add_common(bc_file)

    bc_update = bc_sub.add_parser("update", help="Update a breadcrumb")
    bc_update.add_argument("identifier")
    bc_update.add_argument("--title", default=None)
    bc_update.add_argument("--body", default=None)
    bc_update.add_argument("--kind", default=None)
    bc_update.add_argument("--status", default=None)
    bc_update.add_argument("--severity", default=None)
    _add_common(bc_update)

    bc_get = bc_sub.add_parser("get", help="Get a breadcrumb")
    bc_get.add_argument("identifier")
    _add_common(bc_get)

    bc_find = bc_sub.add_parser("find", help="Find breadcrumbs by text or filters")
    bc_find.add_argument("--status", default=None)
    bc_find.add_argument("--type", default=None, dest="type")
    bc_find.add_argument("--text", default=None)
    bc_find.add_argument("--limit", type=int, default=None)
    _add_common(bc_find)

    bc_query = bc_sub.add_parser("query", help="Query breadcrumbs")
    bc_query.add_argument("filter", nargs="?", default=None)
    bc_query.add_argument("--limit", type=int, default=50)
    _add_common(bc_query)

    # ------------------------------------------------------------------
    # memory
    # ------------------------------------------------------------------
    mem = sub.add_parser("memory", help="Memory operations")
    mem_sub = mem.add_subparsers(dest="mem_cmd")

    mem_add = mem_sub.add_parser("add", help="Add a memory")
    mem_add.add_argument("--name", required=True)
    mem_add.add_argument("--body", required=True)
    mem_add.add_argument("--type", required=True, dest="type")
    mem_add.add_argument("--attributes", default=None)
    _add_common(mem_add)

    mem_get = mem_sub.add_parser("get", help="Get a memory")
    mem_get.add_argument("name")
    _add_common(mem_get)

    mem_list = mem_sub.add_parser("list", help="List memories")
    mem_list.add_argument("--type", default=None, dest="type")
    mem_list.add_argument("--limit", type=int, default=50)
    _add_common(mem_list)

    mem_search = mem_sub.add_parser("search", help="Search memories")
    mem_search.add_argument("query")
    mem_search.add_argument("--limit", type=int, default=10)
    _add_common(mem_search)

    mem_delete = mem_sub.add_parser("delete", help="Delete (soft) a memory")
    mem_delete.add_argument("name")
    _add_common(mem_delete)

    # ------------------------------------------------------------------
    # link
    # ------------------------------------------------------------------
    lnk = sub.add_parser("link", help="Link operations")
    lnk_sub = lnk.add_subparsers(dest="lnk_cmd")

    lnk_add = lnk_sub.add_parser("add", help="Add a typed link")
    lnk_add.add_argument("--from", required=True, dest="from_")
    lnk_add.add_argument("--to", required=True)
    lnk_add.add_argument("--type", required=True, dest="rel_type")

    lnk_remove = lnk_sub.add_parser("remove", help="Remove a typed link")
    lnk_remove.add_argument("--from", required=True, dest="from_")
    lnk_remove.add_argument("--to", required=True)
    lnk_remove.add_argument("--type", required=True, dest="rel_type")

    lnk_trace = lnk_sub.add_parser("trace", help="Trace links from a node")
    lnk_trace.add_argument("start")
    lnk_trace.add_argument("--all", action="store_true", help="Cross-kind traversal")
    lnk_trace.add_argument("--direction", default="dependencies")
    lnk_trace.add_argument("--depth", type=int, default=3)
    lnk_trace.add_argument("--json", action="store_true")

    # ------------------------------------------------------------------
    # search
    # ------------------------------------------------------------------
    srch = sub.add_parser("search", help="Cross-kind search")
    srch_sub = srch.add_subparsers(dest="srch_cmd")

    srch_all = srch_sub.add_parser("all", help="Search across all kinds")
    srch_all.add_argument("query")
    srch_all.add_argument("--limit", type=int, default=20)
    _add_common(srch_all)

    # ------------------------------------------------------------------
    # vocabulary
    # ------------------------------------------------------------------
    vocab = sub.add_parser("vocabulary", help="Vocabulary operations")
    vocab_sub = vocab.add_subparsers(dest="vocab_cmd")

    vocab_list = vocab_sub.add_parser("list", help="List vocabularies")
    vocab_list.add_argument("--workspace", required=True)
    vocab_list.add_argument("--kind", default=None)
    vocab_list.add_argument("--include-archived", action="store_true", default=False)
    vocab_list.add_argument("--json", action="store_true")

    vocab_archive = vocab_sub.add_parser("archive", help="Archive a vocabulary entry")
    vocab_archive.add_argument("--workspace", required=True)
    vocab_archive.add_argument("kind")
    vocab_archive.add_argument("name")

    # ------------------------------------------------------------------
    # changes
    # ------------------------------------------------------------------
    changes = sub.add_parser("changes", help="Change log operations")
    changes_sub = changes.add_subparsers(dest="changes_cmd")

    changes_since = changes_sub.add_parser("since", help="List changes since a timestamp")
    changes_since.add_argument("since")
    changes_since.add_argument("--limit", type=int, default=50)
    changes_since.add_argument("--json", action="store_true")

    # ------------------------------------------------------------------
    # install-skills
    # ------------------------------------------------------------------
    install = sub.add_parser("install-skills", help="Install skill files")
    install.add_argument("--target", default="claude", choices=["claude", "opencode"])
    install.add_argument("--dry-run", action="store_true")

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    args = parser.parse_args()

    if args.command == "init":
        return cmd_init(args.path)
    if args.command == "resolve":
        return cmd_resolve(args.path, args.json)
    if args.command == "doctor":
        return cmd_doctor(args.json)
    if args.command == "breadcrumb":
        if args.bc_cmd == "file":
            return cmd_bc_file(args)
        if args.bc_cmd == "update":
            return cmd_bc_update(args)
        if args.bc_cmd == "get":
            return cmd_bc_get(args)
        if args.bc_cmd == "find":
            return cmd_bc_find(args)
        if args.bc_cmd == "query":
            return cmd_bc_query(args)
        bc.print_help()
        return EXIT_GENERIC
    if args.command == "memory":
        if args.mem_cmd == "add":
            return cmd_mem_add(args)
        if args.mem_cmd == "get":
            return cmd_mem_get(args)
        if args.mem_cmd == "list":
            return cmd_mem_list(args)
        if args.mem_cmd == "search":
            return cmd_mem_search(args)
        if args.mem_cmd == "delete":
            return cmd_mem_delete(args)
        mem.print_help()
        return EXIT_GENERIC
    if args.command == "link":
        if args.lnk_cmd == "add":
            return cmd_link_add(args)
        if args.lnk_cmd == "remove":
            return cmd_link_remove(args)
        if args.lnk_cmd == "trace":
            return cmd_link_trace(args)
        lnk.print_help()
        return EXIT_GENERIC
    if args.command == "search":
        if args.srch_cmd == "all":
            return cmd_search_all(args)
        srch.print_help()
        return EXIT_GENERIC
    if args.command == "vocabulary":
        if args.vocab_cmd == "list":
            return cmd_vocab_list(args)
        if args.vocab_cmd == "archive":
            return cmd_vocab_archive(args)
        vocab.print_help()
        return EXIT_GENERIC
    if args.command == "changes":
        if args.changes_cmd == "since":
            return cmd_changes_since(args)
        changes.print_help()
        return EXIT_GENERIC
    if args.command == "install-skills":
        return cmd_install_skills(args)

    parser.print_help()
    return EXIT_GENERIC


# Legacy MCP entry-point shims (kept operational during Plan 004 Phase 9a–9c)

_KIND_ALIASES = {
    "bc": "breadcrumbs",
    "breadcrumbs": "breadcrumbs",
    "memory": "memory",
    "memories": "memory",
    "search": "search",
}


def serve(kinds: list[str]) -> None:
    """Instantiate and run a server mounting the given kind registries."""
    from agent_notes.core.server import Server

    server = Server()

    for kind in kinds:
        canonical = _KIND_ALIASES.get(kind, kind)
        kind_server = None
        if canonical == "breadcrumbs":
            from agent_notes.servers.breadcrumbs import BreadcrumbServer

            kind_server = BreadcrumbServer()
        elif canonical == "memory":
            from agent_notes.servers.memory import MemoryServer

            kind_server = MemoryServer()
        elif canonical == "search":
            from agent_notes.servers.search import SearchServer

            kind_server = SearchServer()
        else:
            raise NotImplementedError(f"unknown kind: {kind!r}")

        collisions = server.merge_registry(kind_server)
        if collisions:
            print(
                f"Warning: omnibus merge skipped colliding tool(s): {', '.join(collisions)}. "
                f"Use trace_graph_all for cross-kind traversal.",
                file=sys.stderr,
            )

    server.run()


def main_breadcrumbs() -> None:
    serve(["breadcrumbs"])


def main_memory() -> None:
    serve(["memory"])


def main_search() -> None:
    serve(["search"])


def main_omnibus() -> None:
    serve(["breadcrumbs", "memory", "search"])


if __name__ == "__main__":
    sys.exit(main())
