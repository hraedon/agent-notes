from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_notes.core.errors import ErrorCode
    from agent_notes.core.memory_model import ScopeRef

EXIT_SUCCESS = 0
EXIT_GENERIC = 1
EXIT_NOT_FOUND = 2
EXIT_NOT_CONFIGURED = 3
EXIT_CONFLICT = 4


def exception_detail(exc: BaseException) -> dict | None:
    """Extract the structured ``detail`` dict from an exception chain (WI-050).

    A coded failure (``core.errors.AgentNotesError``) may carry a ``detail``
    dict with exactly the facts an operator needs to act. A wrapper raised
    ``from`` it can have no ``detail`` of its own while the original survives
    as the cause, so walk the chain and return the first non-empty ``detail``
    dict, and the CLI surfaces it instead of discarding it.
    """
    seen: set[int] = set()
    node: BaseException | None = exc
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        detail = getattr(node, "detail", None)
        if isinstance(detail, dict) and detail:
            return dict(detail)
        node = node.__cause__ or node.__context__
    return None


def emit_error(
    code: ErrorCode | str,
    message: str,
    *,
    use_json: bool,
    detail: str | dict | None = None,
    retryable: bool = False,
    exit_code: int = EXIT_GENERIC,
) -> int:
    """Report an error per the suite CLI contract v1 and return the exit code.

    Under ``--json`` the common error envelope is the single stdout
    document; otherwise the human message goes to *stderr*. The numeric
    exit codes keep agent-notes' decision-52 taxonomy (2/3/4) for
    compatibility; the envelope's ``code`` carries the stable semantic. Every
    ``code`` is a ``core.errors.ErrorCode`` value (``tests/test_error_envelope.py``
    enforces it for string literals); a member and its string literal render
    identically.

    ``detail`` may be a dict (WI-050: e.g. a review validator's rejection
    facts). The envelope schema types ``error.detail`` as string-or-null
    (``data/cli-error-envelope.schema.json``), so a dict is serialized to a
    single JSON string in that slot; the human path prints it one
    ``key: value`` line at a time.
    """
    if use_json:
        if isinstance(detail, dict):
            detail_out: str | None = json.dumps(detail, sort_keys=True, default=str)
        else:
            detail_out = detail
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": code,
                        "message": message,
                        "detail": detail_out,
                        "retryable": retryable,
                        "partial": None,
                    },
                },
                indent=2,
            )
        )
    else:
        print(f"Error: {message}", file=sys.stderr)
        if isinstance(detail, dict):
            for key in sorted(detail):
                value = detail[key]
                rendered = value if isinstance(value, str) else json.dumps(value, default=str)
                print(f"  {key}: {rendered}", file=sys.stderr)
        elif detail:
            print(f"  {detail}", file=sys.stderr)
    return exit_code


def _resolve(
    ws_slug: str | None, proj_slug: str | None, path: str | None, *, repo_only: bool = False
) -> tuple[int, int, str, str]:
    """Resolve the current workspace/project.

    ``repo_only`` refuses ``--workspace`` alone, which otherwise selects the
    workspace's ``global`` memory tier; work items live only in repo projects.
    """
    return _resolve_impl(ws_slug, proj_slug, path, repo_only=repo_only)


def resolve_repo_project(
    ws_slug: str | None, proj_slug: str | None, path: str | None
) -> tuple[int, int, str, str]:
    """:func:`_resolve` for work-item commands: never a memory-tier row (Plan 025 D3)."""
    return _resolve(ws_slug, proj_slug, path, repo_only=True)


def _resolve_impl(
    ws_slug: str | None, proj_slug: str | None, path: str | None, *, repo_only: bool = False
) -> tuple[int, int, str, str]:
    from agent_notes.core.db import (
        SCOPE_GLOBAL,
        find_scope_project,
        get_project,
        list_projects,
        list_workspaces,
    )

    if path:
        from agent_notes.core.db import resolve_project as db_resolve_project

        try:
            result = db_resolve_project(path)
        except ValueError as exc:
            raise SystemExit(EXIT_NOT_CONFIGURED) from exc
        ws = next((w for w in list_workspaces() if w.slug == result["workspace"]), None)
        if ws is None:
            raise SystemExit(EXIT_NOT_CONFIGURED)
        proj = get_project(ws.id, result["project"])
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
        proj = get_project(ws.id, proj_slug)
        if proj is None:
            available = [p.slug for p in list_projects(workspace_id=ws.id)]
            hint = f" Available in '{ws_slug}': {', '.join(available)}" if available else ""
            if proj_slug == "global" or proj_slug.startswith("vm:"):
                hint += " (memory tiers are not projects; use 'memory ... --scope vm|global')"
            print(f"Project '{proj_slug}' not found.{hint}", file=sys.stderr)
            raise SystemExit(EXIT_NOT_FOUND)
        return ws.id, proj.id, ws.slug, proj.slug

    if ws_slug:
        ws = next((w for w in list_workspaces() if w.slug == ws_slug), None)
        if ws is None:
            available = [w.slug for w in list_workspaces()]
            hint = f" Available: {', '.join(available)}" if available else ""
            suggestion = " Use --path for auto-resolution or run 'agent-notes workspace list'."
            print(f"Workspace '{ws_slug}' not found.{hint}{suggestion}", file=sys.stderr)
            raise SystemExit(EXIT_NOT_FOUND)
        if repo_only:
            raise SystemExit(EXIT_NOT_CONFIGURED)
        # Workspace-level operations land in the workspace's global memory tier.
        proj = find_scope_project(ws.id, SCOPE_GLOBAL)
        if proj is None:
            print(
                f"Workspace '{ws_slug}' has no 'global' project for workspace-level "
                f"operations; create one or pass --project/--path explicitly.",
                file=sys.stderr,
            )
            raise SystemExit(EXIT_NOT_CONFIGURED)
        return ws.id, proj.id, ws.slug, proj.slug

    # Nothing was selected explicitly. Before failing, look at where the caller
    # is standing: an agent working inside a registered repo should not have to
    # name the project it is obviously in. This runs last, so it can only turn
    # a NOT_CONFIGURED exit into a resolution — never override an explicit
    # selector, never change an invocation that already worked.
    from agent_notes.core.project_discovery import discover_project

    discovered = discover_project()
    if discovered is not None:
        ws = next((w for w in list_workspaces() if w.slug == discovered.workspace), None)
        if ws is not None:
            proj = get_project(ws.id, discovered.project)
            if proj is not None:
                return ws.id, proj.id, ws.slug, proj.slug

    raise SystemExit(EXIT_NOT_CONFIGURED)


# ---------------------------------------------------------------------------
# Memory scopes (Plan 025 §4): repo / vm / global / composed
# ---------------------------------------------------------------------------

MEMORY_SCOPES = ("repo", "vm", "global")


class ScopeError(Exception):
    """A memory scope could not be resolved; carries the CLI error envelope fields."""

    def __init__(self, code: str, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


@dataclass(frozen=True)
class MemoryTarget:
    """One memory scope resolved to a project row (``proj_id`` None: not created yet)."""

    ws_id: int
    ws_slug: str
    proj_id: int | None
    proj_slug: str
    scope: str
    scope_key: str


@dataclass(frozen=True)
class ScopeContext:
    """The workspace a scope command runs in, plus the repo it is standing in (if any)."""

    ws_id: int
    ws_slug: str
    repo_proj_id: int | None = None
    repo_slug: str | None = None


def add_scope_args(p: argparse.ArgumentParser, *, composed: bool = False, default: str) -> None:
    choices = [*MEMORY_SCOPES, "composed"] if composed else list(MEMORY_SCOPES)
    p.add_argument(
        "--scope",
        choices=choices,
        default=default,
        help="Memory tier: repo (this project), vm (this machine), global (cross-host)"
        + (", or composed (all three, repo > vm > global)" if composed else "")
        + f". Default: {default}.",
    )
    p.add_argument(
        "--vm",
        default=None,
        help="VM ID for vm scope (default: AGENT_NOTES_VM, else the short hostname)",
    )


def scope_workspace(args: argparse.Namespace, *, strict_path: bool = False) -> ScopeContext:
    """Pick the workspace for a scope command without requiring a registered repo.

    ``strict_path`` makes an explicit ``--path`` that is not inside a registered
    repo (of ``--workspace``, when given) an error, for composed queries where a
    silently absent repo tier would read as "no repo memories".
    """
    from agent_notes.core.db import get_project, list_workspaces
    from agent_notes.core.project_discovery import discover_project

    ws_slug = getattr(args, "workspace", None)
    if getattr(args, "project", None):
        ws_id, proj_id, ws_slug_out, proj_slug = _resolve(ws_slug, args.project, None)
        return ScopeContext(ws_id, ws_slug_out, proj_id, proj_slug)

    workspaces = list_workspaces()
    if ws_slug:
        ws = next((w for w in workspaces if w.slug == ws_slug), None)
        if ws is None:
            available = ", ".join(w.slug for w in workspaces)
            raise ScopeError(
                "WORKSPACE_NOT_FOUND",
                f"Workspace '{ws_slug}' not found. Available: {available or '(none)'}",
                EXIT_NOT_FOUND,
            )
        path = getattr(args, "path", None)
        if path:
            found = discover_project(path)
            proj = (
                get_project(ws.id, found.project) if found and found.workspace == ws.slug else None
            )
            if proj is not None:
                return ScopeContext(ws.id, ws.slug, proj.id, proj.slug)
            if strict_path:
                if found is None:
                    raise SystemExit(EXIT_NOT_CONFIGURED)
                raise ScopeError(
                    "PROJECT_NOT_RESOLVED",
                    f"--path {path!r} is in workspace '{found.workspace}', not '{ws.slug}'.",
                    EXIT_NOT_CONFIGURED,
                )
        return ScopeContext(ws.id, ws.slug)

    discovered = discover_project(getattr(args, "path", None))
    if discovered is not None:
        ws = next((w for w in workspaces if w.slug == discovered.workspace), None)
        proj = get_project(ws.id, discovered.project) if ws else None
        if ws is not None and proj is not None:
            return ScopeContext(ws.id, ws.slug, proj.id, proj.slug)

    if strict_path and getattr(args, "path", None):
        raise SystemExit(EXIT_NOT_CONFIGURED)

    ws = next((w for w in workspaces if w.slug == "default"), None)
    if ws is None:
        raise ScopeError(
            "PROJECT_NOT_RESOLVED",
            "no registered repo here and no 'default' workspace; pass --workspace.",
            EXIT_NOT_CONFIGURED,
        )
    return ScopeContext(ws.id, ws.slug)


def _vm_id(args: argparse.Namespace) -> str:
    from agent_notes.core.config import VmIdError, resolve_vm_id

    try:
        return resolve_vm_id(getattr(args, "vm", None))
    except VmIdError as exc:
        raise ScopeError("VM_ID_UNRESOLVED", str(exc), EXIT_NOT_CONFIGURED) from exc


def resolve_memory_target(args: argparse.Namespace, *, create: bool = False) -> MemoryTarget:
    """Resolve ``--scope`` (repo|vm|global) to one project row.

    ``repo`` keeps the historical resolution (and raises ``SystemExit`` on
    failure, which callers report via :func:`report_resolution_failure`).
    ``vm`` and ``global`` need no registered repo. ``create`` makes a missing
    tier row (writes); reads leave ``proj_id`` None instead.
    """
    from agent_notes.core.db import (
        SCOPE_GLOBAL,
        SCOPE_VM,
        find_scope_project,
        get_or_create_scope_project,
        vm_project_slug,
    )

    scope = getattr(args, "scope", None) or "repo"
    if scope == "repo":
        ws_id, proj_id, ws_slug, proj_slug = _resolve(args.workspace, args.project, args.path)
        return MemoryTarget(ws_id, ws_slug, proj_id, proj_slug, "repo", proj_slug)

    if getattr(args, "project", None):
        raise ScopeError(
            "VALIDATION_FAILED",
            f"--project selects a repo and cannot be combined with --scope {scope}",
            EXIT_CONFLICT,
        )
    ctx = scope_workspace(args)
    vm_id: str | None = None
    if scope == SCOPE_GLOBAL:
        key, slug = "global", "global"
    elif scope == SCOPE_VM:
        vm_id = _vm_id(args)
        key, slug = vm_id, vm_project_slug(vm_id)
    else:
        raise ScopeError("VALIDATION_FAILED", f"unknown scope {scope!r}", EXIT_CONFLICT)

    if create:
        proj = get_or_create_scope_project(ctx.ws_id, scope, vm_id)
    else:
        proj = find_scope_project(ctx.ws_id, scope, vm_id)
    return MemoryTarget(ctx.ws_id, ctx.ws_slug, proj.id if proj else None, slug, scope, key)


def resolve_composed_scopes(
    args: argparse.Namespace,
) -> tuple[ScopeContext, list[ScopeRef], list[str]]:
    """Scopes for a composed query, most specific first, plus skip warnings.

    Tiers that do not exist (no repo here, no memory ever filed for this VM)
    are simply absent. An unresolvable VM ID drops the vm tier with a warning
    rather than failing the whole query.
    """
    from agent_notes.core.config import VmIdError, resolve_vm_id
    from agent_notes.core.db import SCOPE_GLOBAL, SCOPE_VM, find_scope_project
    from agent_notes.core.memory_model import ScopeRef

    ctx = scope_workspace(args, strict_path=True)
    refs: list[ScopeRef] = []
    warnings: list[str] = []
    if ctx.repo_proj_id is not None and ctx.repo_slug is not None:
        refs.append(ScopeRef("repo", ctx.repo_slug, ctx.repo_proj_id))
    try:
        vm_id = resolve_vm_id(getattr(args, "vm", None))
    except VmIdError as exc:
        warnings.append(f"vm scope skipped: {exc}")
    else:
        vm_proj = find_scope_project(ctx.ws_id, SCOPE_VM, vm_id)
        if vm_proj is not None:
            refs.append(ScopeRef("vm", vm_id, vm_proj.id))
    global_proj = find_scope_project(ctx.ws_id, SCOPE_GLOBAL)
    if global_proj is not None:
        refs.append(ScopeRef("global", "global", global_proj.id))
    return ctx, refs, warnings


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
    if (
        detail is None
        and code == EXIT_NOT_CONFIGURED
        and getattr(args, "workspace", None)
        and not getattr(args, "project", None)
        and not path
    ):
        detail = (
            f"--workspace {args.workspace} alone selects no project here: work items "
            "live only in repo projects. Pass --project with it, or --path to a "
            "registered repo."
        )
    if detail is None:
        # No --path was given, so cwd discovery is what actually failed. Name
        # the directory that was tried — "pass --path" is unhelpful advice when
        # the caller is already standing in the directory they mean.
        import os as _os

        detail = (
            f"could not resolve a project/workspace: {_os.getcwd()!r} is not "
            "inside a registered project. Run `agent-notes init` there, or pass "
            "--path to a registered repo, or --workspace/--project, or use "
            "--scope global."
        )
    emit_error(
        "PROJECT_NOT_RESOLVED",
        detail,
        use_json=use_json,
        exit_code=code,
    )


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


def _print_sub_help(parser: argparse.ArgumentParser) -> int:
    parser.print_help()
    return EXIT_SUCCESS


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--path", default=None, help="Filesystem path for project resolution")
    p.add_argument(
        "--workspace",
        default=None,
        help="Workspace slug (optional if --path given). With --workspace alone "
        "(no --project/--path), resolves to the workspace's 'global' project.",
    )
    p.add_argument("--project", default=None, help="Project slug (optional if --path given)")
    p.add_argument("--json", action="store_true", help="Emit JSON instead of human text")
