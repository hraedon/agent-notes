"""Bounded jobs run on behalf of ``request`` messages (Plan 025 D8).

A request's ``payload.job`` names an allowlisted handler. The handler returns a
:class:`JobReply`; whatever happens, the dispatcher posts it as a direct
``wake=true`` reply to the requester, so a requester never waits on silence.
A request is answered at most once (:func:`agent_notes.core.messaging.answer_request`).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from agent_notes.core import messaging

_log = logging.getLogger("agent_notes.dispatcher")

BAD_REQUEST = "BAD_REQUEST"
JOB_TYPE_UNSUPPORTED = "JOB_TYPE_UNSUPPORTED"
JOB_FAILED = "JOB_FAILED"
JOB_INTERRUPTED = "JOB_INTERRUPTED"

_REPLY_ATTEMPTS = 3
_REPLY_BACKOFF = 2.0


@dataclass(frozen=True)
class JobContext:
    vm: str


@dataclass
class JobReply:
    body: str
    payload: dict[str, Any] = field(default_factory=dict)
    subject: str | None = None


JobHandler = Callable[[dict, JobContext], JobReply]


def job_name(request: Mapping[str, Any]) -> str | None:
    """``payload.job`` when the payload is an object and the job a string."""
    job = messaging.payload_of(request).get("job")
    return job if isinstance(job, str) else None


def failure_reply(request: Mapping[str, Any], code: str, detail: str) -> JobReply:
    job = job_name(request)
    body = (
        f"Request #{request['id']} ({job or 'unknown job'}) failed: {code}\n{detail}\n\n"
        f"Status: agent-notes review show {request['id']}"
        if job == "review"
        else f"Request #{request['id']} ({job or 'unknown job'}) failed: {code}\n{detail}"
    )
    return JobReply(
        body=body,
        payload={
            "job": job,
            "request_id": request["id"],
            "status": "error",
            "error_code": code,
            "error_detail": detail[:1000],
        },
    )


def execute_request(request: dict, handlers: Mapping[str, JobHandler], ctx: JobContext) -> JobReply:
    """Run one claimed request and return its reply, without posting it."""
    job = job_name(request)
    started = time.monotonic()
    try:
        if not isinstance(request.get("payload"), dict):
            reply = failure_reply(request, BAD_REQUEST, "the request payload is not a JSON object")
        elif job is None:
            reply = failure_reply(request, BAD_REQUEST, "payload.job is missing or not a string")
        elif (handler := handlers.get(job)) is None:
            supported = ", ".join(sorted(handlers)) or "none"
            reply = failure_reply(
                request, JOB_TYPE_UNSUPPORTED, f"unknown job type {job!r}; supported: {supported}"
            )
        else:
            reply = handler(request, ctx)
    except Exception as exc:  # a handler bug must still answer the requester
        _log.exception("job %s for request #%s raised", job, request["id"])
        reply = failure_reply(request, JOB_FAILED, f"job raised {type(exc).__name__}")
    _log.info(
        "request #%s (%s) finished in %.1fs: %s",
        request["id"],
        job,
        time.monotonic() - started,
        reply.payload.get("status", "?"),
    )
    return reply


def run_request(request: dict, handlers: Mapping[str, JobHandler], ctx: JobContext) -> bool:
    """Run one claimed request and post its reply. True once it is answered."""
    return post_reply(request, execute_request(request, handlers, ctx), ctx)


def post_reply(
    request: Mapping[str, Any],
    reply: JobReply,
    ctx: JobContext,
    *,
    attempts: int = _REPLY_ATTEMPTS,
) -> bool:
    """Post ``reply`` unless the request already has one. True once it is answered
    (or the request is gone); False when every attempt failed."""
    body = reply.body
    if len(body) > messaging.MAX_BODY:
        body = body[: messaging.MAX_BODY - 20] + "\n...(truncated)"
    for attempt in range(1, attempts + 1):
        try:
            row, created = messaging.answer_request(
                request["id"],
                from_vm=ctx.vm,
                from_project=request.get("from_project"),
                subject=reply.subject,
                body=body,
                payload=reply.payload,
                wake=True,
            )
        except Exception:  # noqa: BLE001 - the store may be briefly unavailable
            if attempt == attempts:
                _log.exception("could not post the reply to request #%s", request["id"])
                return False
            time.sleep(_REPLY_BACKOFF * attempt)
            continue
        if row is None:
            _log.warning("request #%s is gone; its reply was dropped", request["id"])
        elif not created:
            _log.info(
                "request #%s already has reply #%s; not posting another", request["id"], row["id"]
            )
        return True
    return False
