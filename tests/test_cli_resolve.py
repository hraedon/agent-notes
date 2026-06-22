"""Tests for CLI workspace/project resolution (common._resolve).

Covers WI-006: --workspace alone (no --project/--path) must resolve to the
workspace's cross-cutting `global` project, so workspace-level memories land
in the global scope instead of silently failing or dropping into a project.
"""

from __future__ import annotations

import argparse
import json
from unittest.mock import patch

import pytest

from agent_notes.cli.common import _resolve
from agent_notes.cli.memory import cmd_mem_add
from agent_notes.core import db as coredb
from tests.conftest import ephemeral_db  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")


class _Vec:
    def tolist(self):
        return [0.0] * 768


def test_workspace_only_resolves_to_global_project():
    ws = coredb.get_or_create_workspace("resolve-ws", "Resolve WS")
    global_proj = coredb.get_or_create_project(ws.id, slug="global", name="Global")
    coredb.get_or_create_project(ws.id, slug="other", name="Other")

    ws_id, proj_id, ws_slug, proj_slug = _resolve("resolve-ws", None, None)

    assert ws_id == ws.id
    assert proj_id == global_proj.id
    assert ws_slug == "resolve-ws"
    assert proj_slug == "global"


def test_workspace_only_without_global_project_errors():
    ws = coredb.get_or_create_workspace("no-global-ws", "No Global WS")
    coredb.get_or_create_project(ws.id, slug="not-global", name="Not Global")

    with pytest.raises(SystemExit):
        _resolve("no-global-ws", None, None)


def test_cmd_mem_add_workspace_only_routes_to_global_project(capsys):
    ws = coredb.get_or_create_workspace("memwire-ws", "Mem Wire WS")
    global_proj = coredb.get_or_create_project(ws.id, slug="global", name="Global")
    coredb.add_vocabulary(ws.id, "memory_type", "note")

    args = argparse.Namespace(
        workspace="memwire-ws",
        project=None,
        path=None,
        name="ws-level-mem",
        body="body text",
        type="note",
        attributes=None,
        json=True,
    )
    with patch("agent_notes.core.embed.embed", return_value=_Vec()):
        code = cmd_mem_add(args)

    assert code == 0
    out = json.loads(capsys.readouterr().out)
    mem = out["memory"]
    assert mem["project_id"] == global_proj.id
    assert mem["workspace_id"] == ws.id
