"""Our hook through a real hermes-agent install (Plan 025 Phase 7). Opt-in.

Set ``AGENT_NOTES_HERMES_PYTHON`` to the interpreter of a throwaway venv with
``pip install hermes-agent``. The test installs our config into a temp HOME,
points ``HERMES_HOME`` there and fires Hermes' own ``invoke_hook("pre_llm_call")``
through ``agent/shell_hooks.py``. No model, no API key, no real ``~/.hermes``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from agent_notes.cli.harness import _install_harness_one
from agent_notes.core import db as coredb
from agent_notes.core import messaging as m

HERMES_PYTHON = os.environ.get("AGENT_NOTES_HERMES_PYTHON")

pytestmark = pytest.mark.skipif(
    not HERMES_PYTHON or sys.platform == "win32",
    reason="set AGENT_NOTES_HERMES_PYTHON to a venv with hermes-agent (POSIX only)",
)

_DRIVER = """
import json, sys
from hermes_cli.config import load_config
from agent.shell_hooks import register_from_config
from hermes_cli.plugins import invoke_hook

specs = register_from_config(load_config(), accept_hooks=False)
out = {"registered": [[s.event, s.command] for s in specs]}
for name, first in (("first", True), ("second", False)):
    out[name] = invoke_hook(
        "pre_llm_call",
        session_id=sys.argv[1],
        user_message="hello there",
        conversation_history=[] if first else [{"role": "user", "content": "x"}],
        is_first_turn=first,
        model="x/y",
        platform="cli",
        sender_id="",
    )
    if first:
        # A peer message arrives between the turns (written by the test).
        print(json.dumps(out), flush=True)
        sys.stdin.readline()
print(json.dumps(out))
"""


def test_real_hermes_dispatches_the_installed_hook(ephemeral_db, tmp_path, monkeypatch):
    vm = f"realvm-{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("AGENT_NOTES_VM", vm)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    sid = f"20261029_101500_{uuid.uuid4().hex[:6]}"
    key = f"{vm}/hermes/{sid}"
    ws = coredb.get_or_create_workspace("default", "Default Workspace").id

    result, _ = _install_harness_one("hermes", False, None, None, None, home=tmp_path)
    assert result["status"] == "installed", result["warnings"]

    wrapper = tmp_path / "bin" / "agent-notes-hook"
    wrapper.parent.mkdir()
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m agent_notes.hooks "$@"\n')
    wrapper.chmod(0o755)
    env = {
        **os.environ,
        "HERMES_HOME": str(tmp_path / ".hermes"),
        "HERMES_ACCEPT_HOOKS": "1",
        "PATH": f"{wrapper.parent}{os.pathsep}{os.environ['PATH']}",
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "AGENT_NOTES_HOOK_LOG": str(tmp_path / "hooks.log"),
    }
    proc = subprocess.Popen(
        [HERMES_PYTHON, "-c", _DRIVER, sid],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=tmp_path,
        env=env,
        text=True,
    )
    first = json.loads(proc.stdout.readline())
    m.send_message(
        workspace_id=ws,
        from_vm="peervm",
        from_session="peervm/codex/p1",
        to_session=key,
        body="please review",
    )
    proc.stdin.write("\n")
    proc.stdin.flush()
    out, err = proc.communicate(timeout=120)
    final = json.loads(out.strip().splitlines()[-1])

    assert final["registered"] == [["pre_llm_call", "agent-notes-hook hermes pre-llm-call"]], err
    (ctx,) = first["first"]
    assert key in ctx["context"] and "agent-notes session key" in ctx["context"]
    (later,) = final["second"]
    assert (
        later["context"].startswith(m.PEER_MESSAGE_MARKER) and "please review" in later["context"]
    )
    assert m.get_session(key)["cwd"] == str(tmp_path)
