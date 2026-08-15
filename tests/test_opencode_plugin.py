"""Opencode plugin concurrency contract tests (WI-067).

The plugin runs inside a long-lived opencode server process that hosts
concurrent sessions. WI-067 requires the harness session id to be threaded
**per-spawn** (into each ``agent-notes`` child's environment) and never stashed
on the shared ``process.env`` — a global mutation would leak one session's id
into every other session's tool subprocesses.

The primary contract test is a deterministic source-inspection test (no node
dependency, runs in CI). A node runtime test additionally exercises the plugin
against a real event handler when ``node`` is available, asserting
``process.env.OPENCODE_SESSION_ID`` is never written.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_PATH = REPO_ROOT / "integrations" / "opencode" / "index.js"

_SOURCE = PLUGIN_PATH.read_text(encoding="utf-8")


def test_plugin_never_mutates_process_env_session_globally():
    """The plugin must not write OPENCODE_SESSION_ID to process.env.

    A concurrent-session leak: two sessions in one server process, each firing
    `session.created`, would clobber each other's id in the shared process env,
    and tool subprocesses spawned by the *other* session would inherit the
    wrong session's identity. The id must only reach a child via the per-spawn
    env object in ``invokeAgentNotes``.
    """
    assert "process.env.OPENCODE_SESSION_ID" not in _SOURCE
    assert "process.env[" not in _SOURCE.split("invokeAgentNotes")[0]


def test_plugin_threads_session_id_per_spawn():
    """The per-spawn env carries OPENCODE_SESSION_ID for the child only."""
    # invokeAgentNotes builds a fresh env object and copies the session id into
    # it — never into process.env.
    assert "const env = { ...process.env };" in _SOURCE
    assert "env.OPENCODE_SESSION_ID" in _SOURCE
    # The session.created handler only records the mapping; the only mutation
    # of the global env object anywhere in the plugin would be an assignment to
    # process.env.<name> — which must not exist.
    import re

    assignments = re.findall(r"process\.env\s*\.\s*[A-Za-z_][A-Za-z0-9_]*\s*=", _SOURCE)
    assert assignments == [], f"plugin mutates process.env: {assignments}"


def test_plugin_source_is_valid_javascript():
    """Syntax gate via node --check when node is available."""
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    result = subprocess.run(
        ["node", "--check", str(PLUGIN_PATH)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_plugin_does_not_clobber_process_env_across_sessions(tmp_path):
    """Runtime: two sessions in one process must not leak ids into process.env.

    Imports the plugin as an ES module, fires ``session.created`` for two
    different sessions, and asserts ``process.env.OPENCODE_SESSION_ID`` is
    never set afterward.
    """
    script = tmp_path / "run-plugin-test.mjs"
    script.write_text(
        "\n".join(
            [
                'import assert from "node:assert";',
                'import { spawn } from "node:child_process";',
                f'const mod = await import("{PLUGIN_PATH.as_uri()}");',
                "const ctx = { client: { app: { log: () => {} } } };",
                "const plugin = await mod.default(ctx);",
                "const fire = (sessionID) => plugin.event({",
                '  event: { type: "session.created",',
                "    properties: { sessionID, info: { directory: '/tmp' } },",
                "  },",
                "});",
                'await fire("session-A");',
                'await fire("session-B");',
                "assert.ok(",
                '  !("OPENCODE_SESSION_ID" in process.env),',
                '  "process.env.OPENCODE_SESSION_ID must never be set globally",',
                ");",
                'console.log("PLUGIN_NO_CLOBBER_OK");',
            ]
        ),
        encoding="utf-8",
    )
    # Resolve the node interpreter the test is actually running under (from
    # PATH) rather than hardcoding a host-specific install path.
    node_bin = Path(shutil.which("node") or "node").resolve()
    node_env = dict(os.environ)
    node_env["PATH"] = os.pathsep.join([str(node_bin.parent), node_env.get("PATH", os.defpath)])
    result = subprocess.run(
        [str(node_bin), str(script)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=node_env,
    )
    assert result.returncode == 0, result.stderr
    assert "PLUGIN_NO_CLOBBER_OK" in result.stdout
