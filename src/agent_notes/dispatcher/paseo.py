"""The local Paseo daemon, as the dispatcher sees it (Plan 025 §6, L3).

Only two CLI calls are used, both against the local daemon: ``paseo inspect
<id> --json`` and ``paseo send <id> --no-wait --behavior steer --prompt-file
<file>``. No
``--host`` is ever passed (D2), and nothing here logs the environment, which
carries ``PASEO_PASSWORD`` on a Paseo machine.

Sessions adopted with ``paseo import`` don't know their own Paseo agent ID, so
:func:`find_agent_for_session` maps a harness session ID to one by reading the
daemon's agent records (``<PASEO_HOME>/agents/*/*.json``,
``persistence.sessionId``), per the Phase 0 spike (b).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

PASEO_BIN_ENV = "AGENT_NOTES_PASEO_BIN"
PASEO_HOME_ENV = "PASEO_HOME"
CALL_TIMEOUT = 30.0

# AgentLifecycleStatus in Paseo 0.13: initializing | idle | running | error | closed.
STATUS_IDLE = "idle"
BUSY_STATUSES = frozenset({"running", "initializing"})

_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_PLUGIN_PREFIX = "plugin:"

_log = logging.getLogger("agent_notes.dispatcher")


class PaseoError(RuntimeError):
    """A ``paseo`` call failed or returned something unusable."""


@dataclass(frozen=True)
class AgentState:
    status: str | None
    archived: bool = False

    @property
    def idle(self) -> bool:
        return self.status == STATUS_IDLE and not self.archived

    @property
    def busy(self) -> bool:
        return self.status in BUSY_STATUSES and not self.archived


def find_paseo(env: Mapping[str, str] | None = None) -> str | None:
    """``AGENT_NOTES_PASEO_BIN``, then ``paseo`` on PATH (``paseo.cmd`` on Windows)."""
    env = os.environ if env is None else env
    explicit = (env.get(PASEO_BIN_ENV) or "").strip()
    if explicit:
        if os.path.isfile(explicit):
            return explicit
        return shutil.which(explicit)
    return shutil.which("paseo", path=env.get("PATH"))


def paseo_home(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    configured = (env.get(PASEO_HOME_ENV) or "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".paseo"


def valid_agent_id(agent_id: str) -> bool:
    return bool(_AGENT_ID_RE.match(agent_id or ""))


Runner = Callable[..., subprocess.CompletedProcess]


class PaseoClient:
    """A thin wrapper over the ``paseo`` CLI for one local daemon."""

    def __init__(
        self, executable: str, *, timeout: float = CALL_TIMEOUT, runner: Runner = subprocess.run
    ) -> None:
        self.executable = executable
        self.timeout = timeout
        self._run = runner

    def _call(self, args: list[str]) -> subprocess.CompletedProcess:
        try:
            proc = self._run(
                [self.executable, *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.DEVNULL,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PaseoError(f"paseo {args[0]} failed: {type(exc).__name__}") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            raise PaseoError(
                f"paseo {args[0]} exited {proc.returncode}: {detail[0][:200] if detail else ''}"
            )
        return proc

    def inspect(self, agent_id: str) -> AgentState:
        if not valid_agent_id(agent_id):
            raise PaseoError(f"not a Paseo agent id: {agent_id!r}")
        proc = self._call(["inspect", agent_id, "--json"])
        try:
            data = json.loads(proc.stdout)
        except ValueError as exc:
            raise PaseoError("paseo inspect returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise PaseoError("paseo inspect returned a non-object")
        status = data.get("Status", data.get("status"))
        archived = data.get("Archived", data.get("archived"))
        return AgentState(
            status=str(status).lower() if status is not None else None,
            archived=bool(archived),
        )

    def send(self, agent_id: str, prompt: str) -> None:
        """Deliver ``prompt`` as a new turn, without waiting for it.

        ``--behavior steer`` is always passed. The caller checks that the agent
        is idle first, but a human can start a turn in between; Paseo's
        fallback for a running agent would otherwise be an interrupt.
        """
        if not valid_agent_id(agent_id):
            raise PaseoError(f"not a Paseo agent id: {agent_id!r}")
        try:
            fd, path = tempfile.mkstemp(prefix="agent-notes-wake-", suffix=".txt")
        except OSError as exc:
            raise PaseoError(f"could not create the prompt file: {type(exc).__name__}") from exc
        try:
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(prompt)
            except OSError as exc:
                raise PaseoError(f"could not write the prompt file: {type(exc).__name__}") from exc
            self._call(
                ["send", agent_id, "--no-wait", "--behavior", "steer", "--prompt-file", path]
            )
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Paseo's local agent records (spike b)
# ---------------------------------------------------------------------------


def _record_session_ids(record: Mapping[str, Any]) -> set[str]:
    persistence = record.get("persistence")
    if not isinstance(persistence, dict):
        return set()
    ids: set[str] = set()
    for key in ("sessionId", "nativeHandle"):
        value = persistence.get(key)
        if isinstance(value, str) and value:
            if value.startswith(_PLUGIN_PREFIX):
                # agy: plugin:{"version":1,"data":{"conversationId":"..."}}
                try:
                    plugin = json.loads(value[len(_PLUGIN_PREFIX) :])
                    conversation = (plugin.get("data") or {}).get("conversationId")
                except (ValueError, AttributeError):
                    conversation = None
                if isinstance(conversation, str) and conversation:
                    ids.add(conversation)
            else:
                ids.add(value)
    metadata = persistence.get("metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("threadId"), str):
        ids.add(metadata["threadId"])
    return ids


def find_agent_for_session(session_id: str, *, home: Path) -> str | None:
    """Return the Paseo agent hosting ``session_id``, or ``None``.

    When several records match (the same thread imported twice), the most
    recently written one wins.
    """
    agents = home / "agents"
    if not session_id or not agents.is_dir():
        return None
    best: tuple[float, str] | None = None
    for path in agents.glob("*/*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            mtime = path.stat().st_mtime
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict) or session_id not in _record_session_ids(record):
            continue
        agent_id = record.get("id") if isinstance(record.get("id"), str) else path.stem
        if not valid_agent_id(agent_id):
            continue
        if best is None or mtime > best[0]:
            best = (mtime, agent_id)
    return best[1] if best else None
