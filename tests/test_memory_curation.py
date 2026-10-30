"""Plan 027 Phase 2 (WI-2.1): memory-curation notices, against real Postgres."""

from __future__ import annotations

import argparse
import json
import os
import tempfile

import psycopg
import pytest

from agent_notes.cli.memory import cmd_mem_hygiene
from agent_notes.core import db as coredb
from agent_notes.core import messaging
from agent_notes.core.memory_curation import (
    CURATION_SENDER,
    MIN_REFLECTIONS,
    NOTICE_MAX_BYTES,
    render_body,
)
from agent_notes.core.memory_model import add_memory, delete_memory
from tests.conftest import ephemeral_db  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")

_ROOT = os.path.abspath(tempfile.mkdtemp(prefix="curation-"))
GITHUB_TOKEN = "ghp_" + "Z9y8X7w6V5" * 4  # token-shaped, fake
_counter = iter(range(10_000))


def _vec(*weights: float) -> list[float]:
    values = [0.0] * 768
    for i, w in enumerate(weights):
        values[i] = w
    return values


@pytest.fixture(autouse=True)
def _vm_and_cleanup(monkeypatch, ephemeral_db):  # noqa: F811
    """A VM ID for the sender, and no curation notice left for other modules.

    ``--all`` runs see every workspace in the shared test database, so they
    post to other modules' projects too; those notices are removed here.
    """
    monkeypatch.setenv("AGENT_NOTES_VM", "curvm")
    yield
    with psycopg.connect(ephemeral_db) as conn:
        conn.execute(
            "DELETE FROM agent_messages WHERE kind = 'notice' AND payload->>'sender' = %s",
            (CURATION_SENDER,),
        )


@pytest.fixture
def ws(ephemeral_db):  # noqa: F811
    """A fresh workspace with a project that has findings and one that has none."""
    n = next(_counter)
    slug = f"cur-ws-{n}"
    w = coredb.get_or_create_workspace(slug, f"Curation {n}")
    for t in ("project", "reference", "user", "feedback", "reflection"):
        coredb.add_vocabulary(w.id, "memory_type", t)
    busy = coredb.get_or_create_project(w.id, "busy", "busy", repo_root=f"{_ROOT}/{n}/busy")
    clean = coredb.get_or_create_project(w.id, "clean", "clean", repo_root=f"{_ROOT}/{n}/clean")
    add_memory(w.id, clean.id, "small-fact", "project", "a short, healthy fact")
    return {"dsn": ephemeral_db, "ws": w, "slug": slug, "busy": busy, "clean": clean}


def _seed_busy(ws) -> None:
    """Two near-duplicates, one oversized fact and a possible secret: 3 items."""
    wid, pid = ws["ws"].id, ws["busy"].id
    add_memory(wid, pid, "deploy-steps", "project", "deploy with make", embedding=_vec(1.0))
    add_memory(wid, pid, "deploy-howto", "project", "deploy: make", embedding=_vec(1.0, 0.05))
    add_memory(wid, pid, "big-fact", "project", "big " + "x" * 2500)
    add_memory(wid, pid, "leaky", "reference", f"the token is {GITHUB_TOKEN}")


def _hygiene(ws, capsys, **kw) -> tuple[int, dict]:
    base = {
        "workspace": None,
        "project": None,
        "path": None,
        "all": True,
        "stale_days": 180,
        "notify": True,
        "dry_run": False,
        "json": True,
    }
    base.update(kw)
    code = cmd_mem_hygiene(argparse.Namespace(**base))
    out = capsys.readouterr().out
    return code, (json.loads(out) if out.strip().startswith("{") else {"text": out})


def _notices(ws, project: str, *, live_only: bool = False) -> list[dict]:
    sql = (
        "SELECT id, subject, body, payload, expires_at > now() AS live FROM agent_messages "
        "WHERE kind = 'notice' AND workspace_id = %s AND to_project = %s"
    )
    if live_only:
        sql += " AND expires_at > now()"
    with psycopg.connect(ws["dsn"], row_factory=psycopg.rows.dict_row) as conn:
        return conn.execute(sql + " ORDER BY id", (ws["ws"].id, project)).fetchall()


def _mine(result: dict, key: str, ws) -> list[dict]:
    return [n for n in result["notify"][key] if n["workspace"] == ws["slug"]]


def test_project_with_findings_gets_one_notice_and_clean_project_none(ws, capsys):
    _seed_busy(ws)
    code, out = _hygiene(ws, capsys)
    assert code == 0

    posted = _mine(out, "posted", ws)
    assert [n["project"] for n in posted] == ["busy"]
    rows = _notices(ws, "busy")
    assert len(rows) == 1
    notice = rows[0]
    assert notice["subject"] == "memory curation: 3 items"
    assert len(notice["body"].encode("utf-8")) <= NOTICE_MAX_BYTES
    assert notice["body"].endswith(
        f"agent-notes memory hygiene --workspace {ws['slug']} --project busy"
    )
    assert "deploy-howto ~ deploy-steps" in notice["body"] or (
        "deploy-steps ~ deploy-howto" in notice["body"]
    )
    assert "big-fact (2,504 chars)" in notice["body"]
    assert "Possible secrets" in notice["body"]
    assert GITHUB_TOKEN not in json.dumps(out) and GITHUB_TOKEN not in notice["body"]
    assert notice["payload"]["sender"] == CURATION_SENDER
    assert notice["payload"]["curation"]["items"] == 3
    assert notice["payload"]["curation"]["digest"] == posted[0]["digest"]
    assert _notices(ws, "clean") == []

    # It reaches the project's inbox through the ordinary read path, unclaimed.
    inbox = messaging.inbox(None, project="busy", workspace_id=ws["ws"].id, vm="anyvm")
    assert [m["id"] for m in inbox] == [notice["id"]]
    assert inbox[0]["kind"] == "notice"


def test_unchanged_report_does_not_repost(ws, capsys):
    _seed_busy(ws)
    _hygiene(ws, capsys)
    first = _notices(ws, "busy")

    code, out = _hygiene(ws, capsys)
    assert code == 0
    assert _mine(out, "posted", ws) == []
    assert [n["project"] for n in _mine(out, "unchanged", ws)] == ["busy"]
    assert _notices(ws, "busy") == first

    # Not a change: revising a still-oversized fact, or a run for one project.
    add_memory(ws["ws"].id, ws["busy"].id, "big-fact", "project", "big " + "y" * 3000)
    code, out = _hygiene(ws, capsys, all=False, workspace=ws["slug"], project="busy")
    assert code == 0 and out["notify"]["posted"] == []
    assert len(_notices(ws, "busy")) == 1

    # Not a repost after the notice expires either: the digest is still known.
    with psycopg.connect(ws["dsn"]) as conn:
        conn.execute(
            "UPDATE agent_messages SET expires_at = now() - interval '1 second' WHERE id = %s",
            (first[0]["id"],),
        )
    _code, out = _hygiene(ws, capsys)
    assert _mine(out, "posted", ws) == []
    assert len(_notices(ws, "busy")) == 1


def test_changed_report_posts_again_and_retires_the_old_notice(ws, capsys):
    _seed_busy(ws)
    _hygiene(ws, capsys)
    old = _notices(ws, "busy")[0]

    add_memory(ws["ws"].id, ws["busy"].id, "big-two", "feedback", "big " + "z" * 2100)
    code, out = _hygiene(ws, capsys)
    assert code == 0
    assert [n["project"] for n in _mine(out, "posted", ws)] == ["busy"]
    rows = _notices(ws, "busy")
    assert len(rows) == 2
    assert rows[1]["subject"] == "memory curation: 4 items"
    assert rows[0]["id"] == old["id"] and rows[0]["live"] is False
    assert rows[0]["payload"]["curation"]["retired"] is True
    inbox = messaging.inbox(None, project="busy", workspace_id=ws["ws"].id, vm="anyvm")
    assert [m["id"] for m in inbox] == [rows[1]["id"]]


def test_cleared_findings_retire_the_notice_and_a_recurrence_posts_again(ws, capsys):
    wid, pid = ws["ws"].id, ws["busy"].id
    add_memory(wid, pid, "big-fact", "project", "big " + "x" * 2500)
    _hygiene(ws, capsys, all=False, workspace=ws["slug"], project="busy")
    assert len(_notices(ws, "busy", live_only=True)) == 1

    delete_memory(wid, pid, "big-fact")
    # A dry run would retire it, but doesn't.
    _code, out = _hygiene(ws, capsys, dry_run=True)
    assert [r["project"] for r in _mine(out, "retired", ws)] == ["busy"]
    assert len(_notices(ws, "busy", live_only=True)) == 1

    code, out = _hygiene(ws, capsys, all=False, workspace=ws["slug"], project="busy")
    assert code == 0
    assert [r["project"] for r in out["notify"]["retired"]] == ["busy"]
    assert _notices(ws, "busy", live_only=True) == []

    # The same finding returning is news again, not a duplicate.
    add_memory(wid, pid, "big-fact", "project", "big " + "x" * 2500)
    _code, out = _hygiene(ws, capsys)
    assert [n["project"] for n in _mine(out, "posted", ws)] == ["busy"]
    assert len(_notices(ws, "busy", live_only=True)) == 1


def test_dry_run_posts_nothing_but_shows_the_notice(ws, capsys):
    _seed_busy(ws)
    code, out = _hygiene(ws, capsys, dry_run=True)
    assert code == 0 and out["notify"]["dry_run"] is True
    [would] = _mine(out, "posted", ws)
    assert would["subject"] == "memory curation: 3 items"
    assert "message_id" not in would
    assert would["body"].endswith("--project busy")
    assert _notices(ws, "busy") == []

    # Text mode prints each body; a real run afterwards posts it.
    code, text = _hygiene(ws, capsys, dry_run=True, json=False)
    assert code == 0 and "[would post]" in text["text"] and "Oversized facts" in text["text"]
    assert _notices(ws, "busy") == []
    _hygiene(ws, capsys)
    _hygiene(ws, capsys)
    assert len(_notices(ws, "busy")) == 1


def test_dry_run_requires_notify(ws, capsys):
    code, out = _hygiene(ws, capsys, notify=False, dry_run=True)
    assert code == 4 and out["error"]["code"] == "VALIDATION_FAILED"


def test_small_reflection_backlog_is_not_a_notice_but_a_growing_one_stays_unchanged(ws, capsys):
    wid, pid = ws["ws"].id, ws["busy"].id
    for i in range(MIN_REFLECTIONS - 1):
        add_memory(wid, pid, f"reflection-{i}", "reflection", f"session {i}")
    _code, out = _hygiene(ws, capsys)
    assert _mine(out, "posted", ws) == []

    add_memory(wid, pid, "reflection-x", "reflection", "session x")
    _code, out = _hygiene(ws, capsys)
    [posted] = _mine(out, "posted", ws)
    assert posted["subject"] == f"memory curation: {MIN_REFLECTIONS} items"
    assert "Reflections with no follow-up fact" in _notices(ws, "busy")[0]["body"]

    # One more reflection on the same backlog is the same job: no repost.
    add_memory(wid, pid, "reflection-y", "reflection", "session y")
    _code, out = _hygiene(ws, capsys)
    assert _mine(out, "posted", ws) == []
    assert len(_notices(ws, "busy")) == 1


def test_body_is_capped_and_always_ends_with_the_command():
    long = "n" * 80
    sections = {
        "oversized": [(f'["big", "{i}"]', f"{long}-{i} (99,999 chars)", 1) for i in range(60)],
        "near_duplicates": [(f'["dup", "{i}"]', f"{long} ~ {long}", 1) for i in range(60)],
        "stale": [(f'["stale", "{i}"]', f"{long} (400d; path)", 1) for i in range(60)],
    }
    body = render_body("ws", "proj", sections)
    assert len(body.encode("utf-8")) <= NOTICE_MAX_BYTES
    assert body.endswith("agent-notes memory hygiene --workspace ws --project proj")
    assert "Oversized facts (60)" in body

    # A slug long enough to defeat every shortening still keeps the command.
    every = {k: sections["stale"] for k in ("possible_secrets", "contradictions", "shadowed")}
    body = render_body("ws", "p" * 1700, {**sections, **every})
    assert len(body.encode("utf-8")) <= NOTICE_MAX_BYTES
    assert body.endswith("--project " + "p" * 1700)
