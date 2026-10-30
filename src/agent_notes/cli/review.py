"""`agent-notes review ...`: ask another harness for a code review (Plan 025 §6a).

``review request`` inserts a ``request`` row for a VM's dispatcher, which runs
one bounded Claude review (Plan 023's runner) and replies directly to the
requesting session with ``wake=true``. ``review show`` and ``review list`` read
the request, its reply and the stored artifact. Distinct from
``work-item review``, which drives the lifecycle review gate.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

from agent_notes.cli.common import (
    EXIT_CONFLICT,
    EXIT_NOT_CONFIGURED,
    EXIT_NOT_FOUND,
    EXIT_SUCCESS,
    emit_error,
)
from agent_notes.cli.msg import (
    EXIT_TIMEOUT,
    _guarded,
    _message_out,
    _print_json,
    _read_body,
    add_as_argument,
    resolve_caller,
)
from agent_notes.core import messaging

REVIEW_VM_ENV = "AGENT_NOTES_REVIEW_VM"
REVIEWERS = ("claude",)
INSTRUCTION = "Keep working or end your turn; the verdict will arrive as a message."
MAX_BRIEF_BYTES = 16 * 1024
MAX_TITLE_CHARS = 200
_LOG_LINES = 50


class _GitError(Exception):
    pass


def _review_vm(explicit: str | None, current: str) -> str:
    if explicit:
        return explicit.strip().lower()
    from agent_notes.core.config import _env_or_suite
    from agent_notes.core.suite_env import load_suite_env

    configured = _env_or_suite(REVIEW_VM_ENV, load_suite_env())
    return configured.strip().lower() if configured else current


def _git(cwd: str, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _GitError(f"git {args[0]} failed: {type(exc).__name__}") from exc
    if proc.returncode != 0:
        lines = (proc.stderr or "").strip().splitlines()
        raise _GitError(lines[-1] if lines else f"git {args[0]} exited {proc.returncode}")
    return proc.stdout


def _resolve_rev(cwd: str, rev: str) -> str:
    if not rev or rev.startswith("-"):
        raise _GitError(f"not a revision: {rev!r}")
    return _git(cwd, "rev-parse", "--verify", "--end-of-options", f"{rev}^{{commit}}").strip()


def _bounded(text: str, limit_bytes: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit_bytes:
        return text
    return data[: limit_bytes - 20].decode("utf-8", errors="ignore") + "\n...(truncated)"


def _default_subject(cwd: str, base_sha: str, head_sha: str) -> tuple[str, str]:
    title = _git(cwd, "log", "-1", "--format=%s", head_sha).strip() or head_sha[:12]
    log = _git(cwd, "log", f"--max-count={_LOG_LINES}", "--format=- %s", f"{base_sha}..{head_sha}")
    return title, f"Commits under review:\n{log.strip()}" if log.strip() else ""


# ---------------------------------------------------------------------------
# review request
# ---------------------------------------------------------------------------


@_guarded
def cmd_review_request(args: argparse.Namespace) -> int:
    use_json = args.json
    caller = resolve_caller(args)
    project = args.project or caller.project
    if project is None:
        return emit_error(
            "PROJECT_NOT_RESOLVED",
            "run inside a registered project checkout, or pass --project",
            use_json=use_json,
            exit_code=EXIT_NOT_CONFIGURED,
        )
    from agent_notes.core import db as coredb

    project_row = coredb.get_project(caller.workspace_id, project)
    if project_row is None:
        return emit_error(
            "NOT_FOUND",
            f"project {project!r} is not registered",
            use_json=use_json,
            exit_code=EXIT_NOT_FOUND,
        )

    if args.model:
        from agent_notes.dispatcher.review_job import model_alias_refusal

        if refusal := model_alias_refusal(args.model):
            return emit_error("MODEL_INVALID", refusal, use_json=use_json, exit_code=EXIT_CONFLICT)

    cwd = os.getcwd()
    try:
        head_sha = _resolve_rev(cwd, args.head)
        base_tip = _resolve_rev(cwd, args.base)
        base_sha = _git(cwd, "merge-base", base_tip, head_sha).strip()
        pushed = bool(_git(cwd, "branch", "-r", "--contains", head_sha).strip())
        derived_title, derived_brief = _default_subject(cwd, base_sha, head_sha)
    except _GitError as exc:
        return emit_error("REVISION_INVALID", str(exc), use_json=use_json, exit_code=EXIT_CONFLICT)
    if not pushed and not args.allow_unpushed:
        return emit_error(
            "HEAD_NOT_PUSHED",
            f"head {head_sha[:12]} is on no remote-tracking branch; push it first (the "
            "reviewing VM fetches it), or pass --allow-unpushed",
            use_json=use_json,
            exit_code=EXIT_CONFLICT,
        )

    work_item = work_item_entity_id = None
    if args.work_item:
        from agent_notes.core.work_item_model import WorkItemModel

        wi = WorkItemModel.get_work_item(project_row.id, args.work_item)
        if wi is None:
            return emit_error(
                "NOT_FOUND",
                f"work item {args.work_item!r} not found in {project}",
                use_json=use_json,
                exit_code=EXIT_NOT_FOUND,
            )
        work_item = wi["identifier"]
        work_item_entity_id = wi.get("entity_id")
        title = f"{wi['identifier']}: {wi['title']}"
        brief = WorkItemModel.get_work_item_body(project_row.id, args.work_item) or ""
    elif args.brief is not None:
        brief = _read_body(args.brief)
        first = next((line.strip() for line in brief.splitlines() if line.strip()), "")
        title = first or derived_title
    else:
        title, brief = derived_title, derived_brief
    if args.title and not args.work_item:
        title = args.title
    title = title.strip()[:MAX_TITLE_CHARS] or head_sha[:12]
    brief = _bounded(brief, MAX_BRIEF_BYTES)

    to_vm = _review_vm(args.on, caller.vm)
    payload: dict[str, Any] = {
        "job": "review",
        "reviewer": args.reviewer,
        "workspace_id": caller.workspace_id,
        "project": project,
        "base": base_sha,
        "head": head_sha,
        "base_ref": args.base,
        "head_ref": args.head,
        "title": title,
        "brief": brief,
        "work_item": work_item,
        # The job binds the attempt to this entity, in this project (WI-087).
        "work_item_entity_id": work_item_entity_id,
        "model": args.model,
        "timeout_seconds": args.timeout,
    }
    body = (
        f"Review request: {args.reviewer} to review {project} {base_sha[:12]}..{head_sha[:12]} "
        f"({args.base}..{args.head}).\n{title}"
    )
    msg = messaging.send_message(
        workspace_id=caller.workspace_id,
        from_session=caller.key,
        from_vm=caller.vm,
        from_project=project,
        to_vm=to_vm,
        kind="request",
        subject=f"Review: {title}"[:MAX_TITLE_CHARS],
        body=body,
        payload=payload,
    )
    warnings: list[str] = []
    if caller.key is None:
        warnings.append(
            f"no session resolved: the verdict goes to the {project} inbox and can't wake you"
        )
    if not pushed:
        warnings.append("head is not pushed; the review fails unless the reviewing VM has it")
    if os.name == "nt" and to_vm == caller.vm:
        warnings.append(
            f"{to_vm} is Windows, where review jobs can't run; set {REVIEW_VM_ENV} or pass --on"
        )

    reply = None
    if args.wait:
        reply = messaging.await_message(
            caller.key,
            reply_to=msg["id"],
            timeout=args.wait,
            vm=caller.vm,
            project=project,
            workspace_id=caller.workspace_id,
        )
        if reply is None:
            return emit_error(
                "AWAIT_TIMEOUT",
                f"request #{msg['id']} has no verdict after {args.wait:g}s; "
                f"check later with `agent-notes review show {msg['id']}`",
                use_json=use_json,
                detail={"request_id": msg["id"]},
                retryable=True,
                exit_code=EXIT_TIMEOUT,
            )

    if use_json:
        _print_json(
            {
                "request_id": msg["id"],
                "request": _message_out(msg),
                "to_vm": to_vm,
                "base": {"ref": args.base, "sha": base_sha},
                "head": {"ref": args.head, "sha": head_sha, "pushed": pushed},
                "session": caller.key,
                "instruction": INSTRUCTION,
                "reply": reply,
                "warnings": warnings,
            }
        )
        return EXIT_SUCCESS
    for warning in warnings:
        print(f"Warning: {warning}", file=sys.stderr)
    print(
        f"Review request #{msg['id']} queued for {to_vm} "
        f"({args.reviewer}, {base_sha[:12]}..{head_sha[:12]})."
    )
    if reply is None:
        print(INSTRUCTION)
    else:
        print()
        print(messaging.format_peer_message(reply))
    return EXIT_SUCCESS


# ---------------------------------------------------------------------------
# review show / list
# ---------------------------------------------------------------------------


def _status(request: dict, reply_id: int | None) -> str:
    if reply_id is not None:
        return "replied"
    if request["claimed_by"] is not None:
        return "claimed"
    if request["expires_at"] <= datetime.now(timezone.utc):
        return "expired"
    return "queued"


def _attempt_and_artifact(reply: dict | None) -> tuple[dict | None, dict | None]:
    payload = messaging.payload_of(reply or {})
    attempt_id = payload.get("attempt_id")
    if not attempt_id:
        return None, None
    try:
        attempt_uuid = uuid.UUID(str(attempt_id))
    except ValueError:
        return None, None
    from agent_notes.core import kernel
    from agent_notes.core.db import _conn
    from agent_notes.core.work_item._review_journal import get_attempt

    with _conn() as conn:
        attempt = get_attempt(conn, attempt_uuid)
        artifact_text = None
        if attempt and attempt.get("artifact_digest"):
            artifact_text = kernel.get_blob(conn, attempt["artifact_digest"])
    keep = (
        "attempt_id",
        "status",
        "harness",
        "model_identity",
        "reported_model_identity",
        "harness_session_id",
        "identity_assurance",
        "base_sha",
        "head_sha",
        "merge_base_sha",
        "artifact_digest",
        "error_code",
        "error_detail",
        "created_at",
        "started_at",
        "result_validated_at",
        "completed_at",
    )
    attempt_out = {k: attempt.get(k) for k in keep} if attempt else None
    artifact = json.loads(artifact_text) if artifact_text else None
    return attempt_out, artifact


def _print_artifact(artifact: dict) -> None:
    result = artifact.get("result") or {}
    print(f"Verdict: {result.get('verdict')}")
    print(f"Summary: {result.get('summary')}")
    for label, key in (("Blocking findings", "blocking_findings"), ("Risks", "non_blocking_risks")):
        items = result.get(key) or []
        if not items:
            continue
        print(f"\n{label} ({len(items)}):")
        for index, finding in enumerate(items, 1):
            paths = ", ".join(finding.get("paths") or [])
            print(f"{index}. {finding.get('title')}" + (f" [{paths}]" if paths else ""))
            print(f"   {finding.get('detail')}")


@_guarded
def cmd_review_show(args: argparse.Namespace) -> int:
    request = messaging.get_message(args.id)
    if request is None or request["kind"] != "request":
        return emit_error(
            "NOT_FOUND", f"no request #{args.id}", use_json=args.json, exit_code=EXIT_NOT_FOUND
        )
    replies = messaging.replies_to(args.id)
    reply = replies[0] if replies else None
    status = _status(request, reply["id"] if reply else None)
    attempt, artifact = _attempt_and_artifact(reply)
    if args.json:
        _print_json(
            {
                "request": request,
                "status": status,
                "reply": reply,
                "attempt": attempt,
                "artifact": artifact,
            }
        )
        return EXIT_SUCCESS
    payload = messaging.payload_of(request)
    print(
        f"Request #{request['id']} ({payload.get('job')}, {payload.get('reviewer')}) "
        f"to {request['to_vm']}: {status}"
    )
    print(
        f"From {messaging.sender_label(request)}, project {payload.get('project')}, "
        f"{str(payload.get('base'))[:12]}..{str(payload.get('head'))[:12]}"
    )
    if payload.get("title"):
        print(f"Title: {payload['title']}")
    if request["claimed_by"]:
        print(f"Claimed by {request['claimed_by']} at {request['claimed_at']}")
    if reply is not None:
        print()
        print(messaging.format_peer_message(reply))
    if attempt is not None:
        print()
        print(f"Attempt {attempt['attempt_id']}: {attempt['status']}")
    if artifact is not None:
        print()
        _print_artifact(artifact)
    return EXIT_SUCCESS


@_guarded
def cmd_review_list(args: argparse.Namespace) -> int:
    caller = resolve_caller(args, register=False)
    if args.mine and caller.key is None:
        return emit_error(
            "NO_SESSION",
            "--mine needs a session: pass --as, set AGENT_NOTES_SESSION, or run in a harness",
            use_json=args.json,
            exit_code=EXIT_NOT_FOUND,
        )
    rows = messaging.list_requests(
        caller.workspace_id,
        job="review",
        from_session=caller.key if args.mine else None,
        limit=args.limit,
    )
    items = []
    for row in rows:
        payload = messaging.payload_of(row)
        items.append(
            {
                "request_id": row["id"],
                "status": _status(row, row["reply_id"]),
                "reply_id": row["reply_id"],
                "from_session": row["from_session"],
                "to_vm": row["to_vm"],
                "project": payload.get("project"),
                "reviewer": payload.get("reviewer"),
                "base": payload.get("base"),
                "head": payload.get("head"),
                "title": payload.get("title"),
                "created_at": row["created_at"],
            }
        )
    if args.json:
        _print_json({"requests": items})
        return EXIT_SUCCESS
    if not items:
        print("No review requests.")
        return EXIT_SUCCESS
    for item in items:
        print(
            f"#{item['request_id']}  {item['status']:<8} {item['project']}  "
            f"{str(item['head'])[:12]} -> {item['to_vm']}  {item['title'] or ''}"
        )
    return EXIT_SUCCESS


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def register_review_parsers(sub: argparse._SubParsersAction) -> None:
    review = sub.add_parser(
        "review", help="Ask another harness to review a branch (Plan 025 review jobs)"
    )
    review_sub = review.add_subparsers(dest="review_cmd")

    req = review_sub.add_parser("request", help="Queue a review job; the verdict comes back")
    req.add_argument("--reviewer", required=True, choices=REVIEWERS)
    req.add_argument("--base", required=True, help="Base revision (the merge base is used)")
    req.add_argument("--head", required=True, help="Head revision; must be pushed")
    subject = req.add_mutually_exclusive_group()
    subject.add_argument("--work-item", default=None, metavar="ID", help="Review against a WI")
    subject.add_argument("--brief", default=None, help="What to review for ('-' reads stdin)")
    req.add_argument("--title", default=None, help="Short title (default: from the brief/head)")
    req.add_argument("--project", default=None, help="Project slug (default: from the cwd)")
    req.add_argument("--on", default=None, metavar="VM", help=f"Reviewing VM ({REVIEW_VM_ENV})")
    req.add_argument("--model", default=None, help="Reviewer model (AGENT_NOTES_REVIEW_MODEL)")
    req.add_argument(
        "--timeout", type=float, default=None, help="Reviewer timeout in seconds (default 600)"
    )
    req.add_argument(
        "--wait", type=float, default=None, metavar="SECONDS", help="Wait for the verdict"
    )
    req.add_argument("--allow-unpushed", action="store_true", help="Skip the pushed-head check")
    req.add_argument("--json", action="store_true")
    add_as_argument(req)
    req.set_defaults(func=cmd_review_request)

    show = review_sub.add_parser("show", help="Show a review request, its reply and artifact")
    show.add_argument("id", type=int)
    show.add_argument("--json", action="store_true")
    show.set_defaults(func=cmd_review_show)

    lst = review_sub.add_parser("list", help="Recent review requests in this workspace")
    lst.add_argument("--mine", action="store_true", help="Only this session's requests")
    lst.add_argument("--limit", type=int, default=20)
    lst.add_argument("--json", action="store_true")
    add_as_argument(lst)
    lst.set_defaults(func=cmd_review_list)

    review.set_defaults(func=lambda args: (review.print_help(), EXIT_SUCCESS)[1])
