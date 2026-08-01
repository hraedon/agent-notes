"""Private harness runners for delegated review (Plan 023 Phase 2).

The subprocess boundary is intentionally small and dependency-free.  Output is
drained incrementally with hard caps, and the child process group is terminated
on timeout, cancellation, or overflow so a reviewer cannot outlive its caller.
"""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping, Sequence

from ._delegated_review import MAX_RESULT_BYTES, ReviewProcessResult, parse_review_result

MAX_RUNNER_STDOUT_BYTES = 1024 * 1024
MAX_RUNNER_STDERR_BYTES = 64 * 1024
MAX_VERSION_BYTES = 4096


class RunnerUnsupportedPlatformError(RuntimeError):
    """The bounded-process runner cannot enforce its guarantees on this platform."""


class RunnerProtocolError(ValueError):
    """The harness completed but did not satisfy its machine-output contract."""


class RunnerOutputLimitError(RunnerProtocolError):
    """A harness exceeded a bounded output channel."""


@dataclass(frozen=True, slots=True)
class BoundedProcessResult:
    exit_code: int
    stdout: bytes
    stderr: bytes


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def run_bounded_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    stdin: bytes = b"",
    env: Mapping[str, str] | None = None,
    max_stdout_bytes: int = MAX_RUNNER_STDOUT_BYTES,
    max_stderr_bytes: int = MAX_RUNNER_STDERR_BYTES,
) -> BoundedProcessResult:
    """Run an argument-vector command with bounded, separated output streams.

    POSIX only, deliberately and explicitly. Two of the guarantees in the name
    are implemented with POSIX-only primitives: the output bounds come from a
    ``selectors`` loop over the child's pipes (on Windows ``DefaultSelector``
    handles sockets, not pipes), and the timeout is enforced by signalling the
    child's process *group* via ``os.killpg``, which has no Windows analogue —
    ``terminate()`` would leave grandchildren running, so a runaway harness
    would outlive its own timeout.

    Rather than degrade quietly into a runner that cannot actually bound
    output or kill what it started, refuse up front. Delegated review is a
    trust boundary: a timeout that does not stop the process, or a cap that
    does not cap, is worse than an unavailable feature.
    """
    if not argv or timeout_seconds <= 0:
        raise ValueError("runner argv and positive timeout are required")
    if os.name == "nt":
        raise RunnerUnsupportedPlatformError(
            "delegated review runners are not supported on Windows: bounded "
            "output and process-group timeout enforcement rely on POSIX "
            "selectors and killpg. Run delegated review from Linux/WSL."
        )
    process = subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=dict(env) if env is not None else None,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    stdout = bytearray()
    stderr = bytearray()
    selector = selectors.DefaultSelector()
    selector.register(
        process.stdout,
        selectors.EVENT_READ,
        ("read", stdout, max_stdout_bytes, "stdout"),
    )
    selector.register(
        process.stderr,
        selectors.EVENT_READ,
        ("read", stderr, max_stderr_bytes, "stderr"),
    )
    stdin_offset = 0
    if stdin:
        os.set_blocking(process.stdin.fileno(), False)
        selector.register(process.stdin, selectors.EVENT_WRITE, ("write",))
    else:
        process.stdin.close()
    deadline = time.monotonic() + timeout_seconds
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("review runner timed out")
            for key, _ in selector.select(min(remaining, 0.1)):
                if key.data[0] == "write":
                    try:
                        written = os.write(key.fileobj.fileno(), stdin[stdin_offset:])
                        stdin_offset += written
                    except BrokenPipeError:
                        stdin_offset = len(stdin)
                    if stdin_offset == len(stdin):
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                    continue
                chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                _, destination, limit, channel = key.data
                if len(destination) + len(chunk) > limit:
                    raise RunnerOutputLimitError(f"review runner {channel} exceeded {limit} bytes")
                destination.extend(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("review runner timed out")
        try:
            exit_code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("review runner timed out") from exc
        return BoundedProcessResult(exit_code, bytes(stdout), bytes(stderr))
    except BaseException:
        _terminate_process_group(process)
        raise
    finally:
        selector.close()
        for stream in (process.stdout, process.stderr, process.stdin):
            if stream is not None and not stream.closed:
                stream.close()


def _strict_object(document: bytes, *, label: str) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate JSON key: {key}")
            value[key] = item
        return value

    try:
        value = json.loads(
            document.decode("utf-8", errors="strict"),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda constant: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON value: {constant}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RunnerProtocolError(f"invalid {label} JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise RunnerProtocolError(f"{label} must be a JSON object")
    return value


def review_result_json_schema() -> dict[str, object]:
    """Return the strict schema supplied to structured-output harnesses."""
    path = {"type": "string", "minLength": 1, "maxLength": 1024}
    finding = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "title": {"type": "string", "minLength": 1, "maxLength": 256},
            "detail": {"type": "string", "minLength": 1, "maxLength": 4096},
            "paths": {"type": "array", "maxItems": 50, "uniqueItems": True, "items": path},
        },
        "required": ["title", "detail", "paths"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "schema_version": {"const": 1},
            "verdict": {"enum": ["accept", "request_changes"]},
            "summary": {"type": "string", "minLength": 1, "maxLength": 4096},
            "blocking_findings": {"type": "array", "maxItems": 20, "items": finding},
            "non_blocking_risks": {"type": "array", "maxItems": 20, "items": finding},
            "reviewed_paths": {
                "type": "array",
                "maxItems": 500,
                "uniqueItems": True,
                "items": path,
            },
        },
        "required": [
            "schema_version",
            "verdict",
            "summary",
            "blocking_findings",
            "non_blocking_risks",
            "reviewed_paths",
        ],
    }


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _minimal_reviewer_env(home: str) -> dict[str, str]:
    """Build the minimal, explicit environment for a reviewer subprocess.

    Delegated review is a trust boundary. The reviewer runs untrusted-by-default
    harness code over repository content, so it must not inherit the operator's
    environment: host config, database DSNs, HMAC key paths, and cloud
    credentials would otherwise leak into the review path (WI-054). Only the
    bare minimum is passed — ``PATH`` so the harness executable resolves, and an
    isolated ``HOME`` so the harness reads none of the operator's state.
    """
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": home,
    }


class ClaudePrintRunner:
    """Claude print-mode runner using its native structured-output sink."""

    harness = "claude"

    def __init__(self, requested_model: str, *, executable: str = "claude") -> None:
        if not requested_model.strip():
            raise ValueError("requested_model is required")
        self.requested_model = requested_model
        self.executable = executable

    def _version(self, cwd: Path, env: Mapping[str, str]) -> str:
        result = run_bounded_process(
            [self.executable, "--version"],
            cwd=cwd,
            timeout_seconds=10.0,
            env=env,
            max_stdout_bytes=MAX_VERSION_BYTES,
            max_stderr_bytes=MAX_VERSION_BYTES,
        )
        if result.exit_code != 0:
            raise RunnerProtocolError("claude --version failed")
        version = result.stdout.decode("utf-8", errors="strict").strip()
        if not version:
            raise RunnerProtocolError("claude version is missing")
        return version[:512]

    def run(self, *, prompt: str, cwd: Path, timeout_seconds: float) -> ReviewProcessResult:
        with tempfile.TemporaryDirectory(prefix="agent-notes-reviewer-") as home:
            env = _minimal_reviewer_env(home)
            harness_version = self._version(cwd, env)
            schema = json.dumps(review_result_json_schema(), separators=(",", ":"))
            argv = [
                self.executable,
                "-p",
                "--safe-mode",
                "--disable-slash-commands",
                "--no-session-persistence",
                "--strict-mcp-config",
                "--mcp-config",
                '{"mcpServers":{}}',
                "--permission-mode",
                "dontAsk",
                "--tools",
                "Read,Grep,Glob",
                "--allowedTools",
                "Read,Grep,Glob",
                "--output-format",
                "json",
                "--json-schema",
                schema,
                "--model",
                self.requested_model,
            ]
            started_at = _iso_now()
            completed = run_bounded_process(
                argv,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                stdin=prompt.encode("utf-8", errors="strict"),
                env=env,
            )
            ended_at = _iso_now()
            if completed.exit_code != 0:
                return ReviewProcessResult(
                    exit_code=completed.exit_code,
                    structured_outputs=(),
                    harness=self.harness,
                    harness_version=harness_version,
                    requested_model=self.requested_model,
                    reported_model=self.requested_model,
                    session_id="unavailable",
                    started_at=started_at,
                    ended_at=ended_at,
                )
            envelope = _strict_object(completed.stdout, label="claude result envelope")
            if envelope.get("type") != "result" or envelope.get("subtype") != "success":
                raise RunnerProtocolError("claude did not report a successful result")
            if envelope.get("is_error") is not False:
                raise RunnerProtocolError("claude result is marked as an error")
            session_id = envelope.get("session_id")
            structured = envelope.get("structured_output")
            model_usage = envelope.get("modelUsage")
            permission_denials = envelope.get("permission_denials")
            if not isinstance(session_id, str) or not session_id.strip():
                raise RunnerProtocolError("claude session_id is missing")
            if not isinstance(structured, dict):
                raise RunnerProtocolError("claude structured_output is missing")
            if not isinstance(model_usage, dict) or len(model_usage) != 1:
                raise RunnerProtocolError("claude must report exactly one model identity")
            if permission_denials != []:
                raise RunnerProtocolError("claude reported a tool permission denial")
            reported_model = next(iter(model_usage))
            if not isinstance(reported_model, str) or not reported_model.strip():
                raise RunnerProtocolError("claude reported model is missing")
            document = json.dumps(
                structured, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8", errors="strict")
            if len(document) > MAX_RESULT_BYTES:
                raise RunnerOutputLimitError("claude structured result exceeds the result cap")
            parse_review_result(document)
            return ReviewProcessResult(
                exit_code=0,
                structured_outputs=(document,),
                harness=self.harness,
                harness_version=harness_version,
                requested_model=self.requested_model,
                reported_model=reported_model,
                session_id=session_id,
                started_at=started_at,
                ended_at=ended_at,
            )
