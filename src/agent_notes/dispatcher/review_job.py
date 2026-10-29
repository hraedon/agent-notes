"""The ``review`` job: one bounded Claude review through Plan 023's runner.

The request payload (written by ``agent-notes review request``) names the
project, the base and head SHAs, the reviewer, and either a brief or a work
item's title and body. The job finds this VM's checkout of the project, fetches,
checks both SHAs exist, and runs 023's ``evaluate_and_record`` with
``ClaudePrintRunner`` in a detached worktree. It stops at ``result_validated``
and makes no lifecycle transition (Plan 025 §8). Every failure becomes a reply
with an error code.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Mapping

from agent_notes.core.work_item._delegated_review import (
    DelegatedReviewError,
    ReviewEvaluation,
    ReviewRunner,
    brief_review_request,
)
from agent_notes.core.work_item._review_journal import AttemptStateError

from .jobs import JobContext, JobReply, failure_reply

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_TIMEOUT = 600.0
MIN_TIMEOUT = 30.0
MAX_TIMEOUT = 1800.0
FINDINGS_BUDGET = 4096
FETCH_TIMEOUT = 180.0
REVIEWERS = ("claude",)

MODEL_ENV = "AGENT_NOTES_REVIEW_MODEL"
TIMEOUT_ENV = "AGENT_NOTES_REVIEW_TIMEOUT"
CLAUDE_BIN_ENV = "AGENT_NOTES_REVIEW_CLAUDE_BIN"
CREDENTIAL_ENV = "AGENT_NOTES_REVIEW_CREDENTIAL_ENV"
ACCEPT_MODELS_ENV = "AGENT_NOTES_REVIEW_ACCEPT_MODELS"

# Error codes this job adds to 023's ReviewErrorCode values.
PLATFORM_UNSUPPORTED = "REVIEW_PLATFORM_UNSUPPORTED"
REQUEST_INVALID = "REVIEW_REQUEST_INVALID"
REVIEWER_UNSUPPORTED = "REVIEW_REVIEWER_UNSUPPORTED"
NO_CHECKOUT = "REVIEW_NO_CHECKOUT"
REVISION_MISSING = "REVIEW_REVISION_MISSING"
ATTEMPT_CONFLICT = "REVIEW_ATTEMPT_CONFLICT"
JOB_ERROR = "REVIEW_JOB_FAILED"

_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,127}$")
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

_log = logging.getLogger("agent_notes.dispatcher")
_fetch_locks: dict[str, threading.Lock] = {}
_fetch_locks_guard = threading.Lock()


class _JobError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def platform_supported() -> bool:
    """023's bounded runner is POSIX-only (``os.killpg``)."""
    return os.name != "nt"


def _env_list(name: str, env: Mapping[str, str]) -> list[str]:
    return [part.strip() for part in (env.get(name) or "").split(",") if part.strip()]


def default_checkout_resolver(workspace_id: int, project_slug: str, vm_id: str) -> str | None:
    """This VM's checkout of the project (``project_checkouts``, WI-2.6)."""
    from agent_notes.core.checkouts import resolve_checkout

    return resolve_checkout(workspace_id, project_slug, vm_id)


def default_runner_factory(model: str, env: Mapping[str, str]) -> ReviewRunner:
    from agent_notes.core.work_item._review_runners import ClaudePrintRunner

    credentials = {}
    for name in _env_list(CREDENTIAL_ENV, env):
        value = env.get(name)
        if value:
            credentials[name] = value
        else:
            _log.warning("review credential variable %s is not set", name)
    return ClaudePrintRunner(
        model,
        executable=(env.get(CLAUDE_BIN_ENV) or "claude").strip(),
        credentials=credentials,
    )


# ---------------------------------------------------------------------------
# Payload and git
# ---------------------------------------------------------------------------


def _payload(request: Mapping[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    raw = request.get("payload") or {}
    reviewer = raw.get("reviewer", "claude")
    if reviewer not in REVIEWERS:
        raise _JobError(REVIEWER_UNSUPPORTED, f"reviewer {reviewer!r} is not supported here")
    project = raw.get("project")
    workspace_id = raw.get("workspace_id")
    if not isinstance(project, str) or not _SLUG_RE.match(project):
        raise _JobError(REQUEST_INVALID, "payload.project is missing or invalid")
    if not isinstance(workspace_id, int) or isinstance(workspace_id, bool):
        raise _JobError(REQUEST_INVALID, "payload.workspace_id is missing or invalid")
    shas = {}
    for side in ("base", "head"):
        value = raw.get(side)
        if not isinstance(value, str) or not _SHA_RE.match(value):
            raise _JobError(REQUEST_INVALID, f"payload.{side} must be a full commit SHA")
        shas[side] = value
    model = raw.get("model") or env.get(MODEL_ENV) or DEFAULT_MODEL
    if not isinstance(model, str) or not _MODEL_RE.match(model):
        raise _JobError(REQUEST_INVALID, "payload.model is invalid")
    timeout = raw.get("timeout_seconds") or env.get(TIMEOUT_ENV) or DEFAULT_TIMEOUT
    try:
        timeout = min(MAX_TIMEOUT, max(MIN_TIMEOUT, float(timeout)))
    except (TypeError, ValueError) as exc:
        raise _JobError(REQUEST_INVALID, "payload.timeout_seconds is invalid") from exc
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        title = f"Review {raw.get('head_ref') or shas['head'][:12]}"
    brief = raw.get("brief")
    if not isinstance(brief, str):
        brief = ""
    return {
        "project": project,
        "workspace_id": workspace_id,
        "base": shas["base"],
        "head": shas["head"],
        "model": model,
        "timeout": timeout,
        "title": title.strip()[:256],
        "brief": brief,
        "work_item": raw.get("work_item") if isinstance(raw.get("work_item"), str) else None,
    }


def _git(checkout: str, *args: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(
        ["git", "-C", checkout, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        env=env,
        stdin=subprocess.DEVNULL,
    )


def _fetch(checkout: str) -> str | None:
    """Fetch every remote; return an error line, or ``None`` on success."""
    with _fetch_locks_guard:
        lock = _fetch_locks.setdefault(
            os.path.normcase(os.path.abspath(checkout)), threading.Lock()
        )
    with lock:
        try:
            proc = _git(checkout, "fetch", "--all", "--quiet", timeout=FETCH_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"git fetch failed: {type(exc).__name__}"
    if proc.returncode != 0:
        lines = (proc.stderr or "").strip().splitlines()
        return f"git fetch exited {proc.returncode}: {lines[-1][:200] if lines else ''}"
    return None


def _has_commit(checkout: str, sha: str) -> bool:
    try:
        return _git(checkout, "cat-file", "-e", f"{sha}^{{commit}}").returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


# ---------------------------------------------------------------------------
# Reply text
# ---------------------------------------------------------------------------


def _bounded(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(0, limit - 3)] + "..."


def _success_reply(request: Mapping[str, Any], spec: Mapping[str, Any], recorded: Any) -> JobReply:
    evaluation: ReviewEvaluation = recorded.evaluation
    artifact = evaluation.artifact
    result = artifact.result
    target = artifact.target
    verdict = result.verdict.value
    rid = request["id"]
    lines = [
        f"Review #{rid} by claude ({artifact.process.reported_model}): "
        f"{verdict.upper().replace('_', ' ')}",
        f"Project {spec['project']}, {target.base_sha[:12]}..{target.head_sha[:12]}, "
        f"{len(target.changed_paths)} changed path(s).",
        "",
        f"Summary: {_bounded(result.summary, 1500)}",
    ]
    findings: list[str] = []
    used = 0
    for index, finding in enumerate(result.blocking_findings, 1):
        paths = ", ".join(finding.paths[:5]) + (" ..." if len(finding.paths) > 5 else "")
        entry = f"{index}. {finding.title}" + (f" [{paths}]" if paths else "")
        entry += f"\n   {_bounded(finding.detail, 800)}"
        if used + len(entry) > FINDINGS_BUDGET:
            findings.append(
                f"... {len(result.blocking_findings) - index + 1} more in the full artifact"
            )
            break
        findings.append(entry)
        used += len(entry)
    if result.blocking_findings:
        lines += ["", f"Blocking findings ({len(result.blocking_findings)}):", *findings]
    if result.non_blocking_risks:
        lines += ["", f"Non-blocking risks: {len(result.non_blocking_risks)} (see the artifact)."]
    lines += ["", f"Full artifact: agent-notes review show {rid}"]
    attempt = recorded.attempt
    return JobReply(
        body="\n".join(lines),
        payload={
            "job": "review",
            "request_id": rid,
            "status": "ok",
            "verdict": verdict,
            "recommendation": evaluation.recommendation.value,
            "attempt_id": str(attempt["attempt_id"]),
            "artifact_digest": attempt.get("artifact_digest") or artifact.digest,
            "base_sha": artifact.target.base_sha,
            "head_sha": artifact.target.head_sha,
            "model": spec["model"],
            "reported_model": artifact.process.reported_model,
            "blocking_findings": len(result.blocking_findings),
            "non_blocking_risks": len(result.non_blocking_risks),
        },
    )


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------


def run_review_job(
    request: dict,
    ctx: JobContext,
    *,
    env: Mapping[str, str] | None = None,
    checkout_resolver: Callable[[int, str, str], str | None] | None = None,
    runner_factory: Callable[[str, Mapping[str, str]], ReviewRunner] | None = None,
    record: Callable[..., Any] | None = None,
) -> JobReply:
    env = os.environ if env is None else env
    try:
        return _run(
            request,
            ctx,
            env=env,
            checkout_resolver=checkout_resolver or default_checkout_resolver,
            runner_factory=runner_factory or default_runner_factory,
            record=record,
        )
    except _JobError as exc:
        _log.info("review request #%s failed: %s %s", request["id"], exc.code, exc.detail)
        return failure_reply(request, exc.code, exc.detail)


def _run(
    request: dict,
    ctx: JobContext,
    *,
    env: Mapping[str, str],
    checkout_resolver: Callable[[int, str, str], str | None],
    runner_factory: Callable[[str, Mapping[str, str]], ReviewRunner],
    record: Callable[..., Any] | None,
) -> JobReply:
    from agent_notes.core import db as coredb

    if not platform_supported():
        raise _JobError(
            PLATFORM_UNSUPPORTED,
            f"review jobs need Linux or macOS; {ctx.vm} is Windows. Address the request "
            "to a Linux VM with --on (or AGENT_NOTES_REVIEW_VM).",
        )
    spec = _payload(request, env)

    project = coredb.get_project(spec["workspace_id"], spec["project"])
    if project is None:
        raise _JobError(NO_CHECKOUT, f"project {spec['project']!r} is not registered")
    checkout = checkout_resolver(spec["workspace_id"], spec["project"], ctx.vm)
    if not checkout or not os.path.isdir(checkout):
        raise _JobError(
            NO_CHECKOUT,
            f"no checkout of {spec['project']} on {ctx.vm}; register one there with "
            "`agent-notes init` or `agent-notes checkout scan <dir> --apply`",
        )

    fetch_error = _fetch(checkout)
    missing = [side for side in ("base", "head") if not _has_commit(checkout, spec[side])]
    if missing:
        detail = ", ".join(f"{side} {spec[side][:12]}" for side in missing)
        hint = f" ({fetch_error})" if fetch_error else ""
        raise _JobError(
            REVISION_MISSING,
            f"{detail} not found in {ctx.vm}'s checkout after fetching{hint}; push it first",
        )
    if fetch_error:
        _log.warning("review request #%s: %s (revisions were present)", request["id"], fetch_error)

    workspace_slug = _workspace_slug(spec["workspace_id"])
    review = brief_review_request(
        subject_ref=f"agent-message-{request['id']}",
        title=spec["title"],
        body=spec["brief"],
        repository_identity=f"{workspace_slug}/{spec['project']}",
        repository_root=Path(checkout),
        base_revision=spec["base"],
        head_revision=spec["head"],
        harness="claude",
        requested_model=spec["model"],
        accepted_reported_models=tuple(_env_list(ACCEPT_MODELS_ENV, env)),
        timeout_seconds=spec["timeout"],
        allow_dirty_checkout=True,
    )
    runner = runner_factory(spec["model"], env)
    if record is None:
        from agent_notes.core.work_item._review_orchestrator import evaluate_and_record

        record = evaluate_and_record
    _log.info(
        "review request #%s: %s %s..%s with %s",
        request["id"],
        spec["project"],
        spec["base"][:12],
        spec["head"][:12],
        spec["model"],
    )
    try:
        recorded = record(project_id=project.id, request=review, runner=runner)
    except DelegatedReviewError as exc:
        raise _JobError(exc.code.value, str(exc)) from exc
    except AttemptStateError as exc:
        raise _JobError(ATTEMPT_CONFLICT, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - reported to the requester by code
        _log.exception("review request #%s raised", request["id"])
        raise _JobError(JOB_ERROR, f"review raised {type(exc).__name__}") from exc
    return _success_reply(request, spec, recorded)


def _workspace_slug(workspace_id: int) -> str:
    from agent_notes.core.db import _conn

    with _conn() as conn:
        row = conn.execute("SELECT slug FROM workspaces WHERE id = %s", (workspace_id,)).fetchone()
    return str(row["slug"]) if row else str(workspace_id)
