"""Plan 027 Phase 1: write-time dedup, contradictions, reflection ranking, size
guidance and the hygiene report — against real Postgres."""

from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import psycopg
import pytest

from agent_notes.cli.memory import (
    cmd_mem_add,
    cmd_mem_delete,
    cmd_mem_get,
    cmd_mem_hygiene,
    cmd_mem_link,
    cmd_mem_recall,
    cmd_mem_search,
    cmd_mem_unlink,
    cmd_mem_update,
)
from agent_notes.core import db as coredb
from agent_notes.core.memory_hygiene import hygiene_report
from agent_notes.core.memory_model import (
    DUPLICATE_GATE_SIMILARITY,
    FACT_SIZE_GUIDANCE_CHARS,
    REFLECTION_RANK_PENALTY,
    add_memory,
)
from agent_notes.core.secret_patterns import SECRET_PATTERNS, redact_if_secret, scan_secrets
from tests.conftest import ephemeral_db  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")

_ROOT = os.path.abspath(tempfile.mkdtemp(prefix="hygiene-"))
VM = "hygvm"
GITHUB_TOKEN = "ghp_" + "A1b2C3d4E5" * 4  # token-shaped, fake
HEX40 = "0123456789abcdef0123456789abcdef01234567"  # legacy NetBox shape, fake
AWS_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"  # fake
SLACK_TOKEN = "xoxb-" + "1234567890-abcdef"  # fake


class _Vec:
    def __init__(self, values: list[float]) -> None:
        self._values = values

    def tolist(self) -> list[float]:
        return list(self._values)


def _vec(*weights: float) -> _Vec:
    values = [0.0] * 768
    for i, w in enumerate(weights):
        values[i] = w
    return _Vec(values)


# Body prefix -> embedding. Axis i is a topic; "dup" bodies sit on one axis.
_EMBEDDINGS = {
    "deploy": _vec(1.0),
    "dup-deploy": _vec(1.0, 0.05),  # ~0.999 to "deploy"
    "near-deploy": _vec(1.0, 0.8),  # ~0.78 to "deploy": a candidate, not blocking
    "cache": _vec(0.0, 0.0, 1.0),
    "q-topic": _vec(0.0, 0.0, 0.0, 1.0),
    "fact-topic": _vec(0.0, 0.0, 0.0, 1.0, 0.30),  # query sim ~0.958
    "refl-topic": _vec(0.0, 0.0, 0.0, 1.0, 0.20),  # query sim ~0.981: wins on raw score
    "secret": _vec(0.0, 0.0, 0.0, 0.0, 0.0, 1.0),
    "stale": _vec(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0),
    "big": _vec(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0),
    # Gate boundary: cosine to "edge-a" just below / just above 0.85.
    "edge-a": _vec(*[0.0] * 9, 1.0),
    "edge-lo": _vec(*[0.0] * 9, 0.84996, math.sqrt(1 - 0.84996**2)),
    "edge-hi": _vec(*[0.0] * 9, 0.85004, 0.0, math.sqrt(1 - 0.85004**2)),
}


def _fake_embed(text: str, task: str = "document") -> _Vec:
    for prefix, vec in _EMBEDDINGS.items():
        if text.startswith(prefix):
            return vec
    # Unknown bodies get a unique axis so they never collide.
    return _vec(*([0.0] * (20 + abs(hash(text)) % 700)), 1.0)


@pytest.fixture(autouse=True)
def _vm_env(monkeypatch):
    monkeypatch.setenv("AGENT_NOTES_VM", VM)


_counter = iter(range(10_000))


@pytest.fixture
def ws(ephemeral_db):  # noqa: F811
    """A fresh workspace + repo per test, so findings never leak across tests."""
    n = next(_counter)
    slug, repo = f"hyg-ws-{n}", f"hyg-repo-{n}"
    w = coredb.get_or_create_workspace(slug, f"Hygiene {n}")
    for t in ("project", "reference", "user", "feedback", "reflection", "note"):
        coredb.add_vocabulary(w.id, "memory_type", t)
    proj = coredb.get_or_create_project(w.id, repo, repo, repo_root=os.path.join(_ROOT, repo))
    return {"dsn": ephemeral_db, "ws": w, "slug": slug, "repo": repo, "proj": proj}


def _args(ws, **kw) -> argparse.Namespace:
    scope = kw.get("scope", "repo")
    base = {
        "workspace": ws["slug"],
        "project": ws["repo"] if scope in ("repo", "composed") else None,
        "path": None,
        "scope": "repo",
        "vm": None,
        "json": True,
        "type": "project",
        "attributes": None,
        "limit": None,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def _run(cmd, capsys, ws, **kw):
    with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
        code = cmd(_args(ws, **kw))
    out = capsys.readouterr().out
    return code, (json.loads(out) if out.strip().startswith("{") else out)


def _add(capsys, ws, name, body, **kw):
    code, out = _run(cmd_mem_add, capsys, ws, name=name, body=body, **kw)
    assert code == 0, out
    return out


def _active(ws, name, project_id=None) -> list[dict]:
    with psycopg.connect(ws["dsn"]) as conn:
        rows = conn.execute(
            "SELECT id, name, active, supersedes, project_id FROM memories "
            "WHERE workspace_id = %s AND name = %s ORDER BY id",
            (ws["ws"].id, name),
        ).fetchall()
    keys = ("id", "name", "active", "supersedes", "project_id")
    return [dict(zip(keys, r, strict=True)) for r in rows]


# ---------------------------------------------------------------------------
# WI-1.1 write-time dedup
# ---------------------------------------------------------------------------


def test_near_duplicate_is_refused_with_machine_readable_candidates(ws, capsys):
    _add(capsys, ws, "deploy-steps", "deploy with make release")
    code, out = _run(cmd_mem_add, capsys, ws, name="deploy-howto", body="dup-deploy: make release")
    assert code == 4
    assert out["ok"] is False
    assert out["error"]["code"] == "POSSIBLE_DUPLICATE"
    detail = json.loads(out["error"]["detail"])
    assert detail["threshold"] == DUPLICATE_GATE_SIMILARITY
    assert [b["name"] for b in detail["blocking"]] == ["deploy-steps"]
    cand = detail["candidates"][0]
    assert cand["name"] == "deploy-steps"
    assert cand["scope"] == "repo" and cand["scope_key"] == ws["repo"]
    assert cand["match"] == ["semantic"] and cand["blocking"] is True
    assert cand["score"] > 0.99
    assert {"memory_type", "body_preview", "chars", "updated_at"} <= set(cand)
    assert _active(ws, "deploy-howto") == []  # nothing written


def test_distinct_related_and_supersedes_resolve_the_gate(ws, capsys):
    _add(capsys, ws, "deploy-steps", "deploy with make release")

    out = _add(capsys, ws, "deploy-a", "dup-deploy: a", distinct=True)
    assert out["dedup"]["distinct"] is True
    assert out["dedup"]["candidates"][0]["name"] == "deploy-steps"

    out = _add(capsys, ws, "deploy-b", "dup-deploy: b", related=["deploy-steps", "deploy-a"])
    assert [r["name"] for r in out["dedup"]["related"]] == ["deploy-steps", "deploy-a"]
    with psycopg.connect(ws["dsn"]) as conn:
        links = conn.execute(
            "SELECT to_identifier FROM links WHERE from_identifier = 'deploy-b' "
            "AND relationship = 'relates_to' AND from_workspace = %s ORDER BY 1",
            (ws["ws"].id,),
        ).fetchall()
    assert [r[0] for r in links] == ["deploy-a", "deploy-steps"]

    # --supersedes replaces one blocking candidate; the other two still block.
    code, out = _run(
        cmd_mem_add, capsys, ws, name="deploy-c", body="dup-deploy: c", supersedes="deploy-steps"
    )
    assert code == 4
    blocking = json.loads(out["error"]["detail"])["blocking"]
    assert {b["name"] for b in blocking} == {"deploy-a", "deploy-b"}

    _add(capsys, ws, "deploy-a", "dup-deploy: a2", distinct=True)  # same name: revision
    out = _add(
        capsys,
        ws,
        "deploy-c",
        "dup-deploy: c",
        supersedes="deploy-steps",
        related=["deploy-a", "deploy-b"],
    )
    old = _active(ws, "deploy-steps")
    assert [r["active"] for r in old] == [False]
    assert out["memory"]["supersedes"] == old[0]["id"]
    with psycopg.connect(ws["dsn"]) as conn:
        events = conn.execute(
            "SELECT event, payload FROM change_log WHERE identifier = 'deploy-steps' "
            "AND workspace_id = %s ORDER BY id",
            (ws["ws"].id,),
        ).fetchall()
    assert events[-1][0] == "superseded"
    assert events[-1][1]["superseded_by"] == "deploy-c"


def test_gate_flag_validation_and_missing_targets(ws, capsys):
    _add(capsys, ws, "deploy-steps", "deploy with make release")
    code, out = _run(
        cmd_mem_add, capsys, ws, name="x", body="dup-deploy", distinct=True, related=["a"]
    )
    assert code == 4 and out["error"]["code"] == "VALIDATION_FAILED"
    code, out = _run(cmd_mem_add, capsys, ws, name="x", body="dup-deploy", supersedes="nope")
    assert code == 2 and out["error"]["code"] == "NOT_FOUND"
    code, out = _run(cmd_mem_add, capsys, ws, name="x", body="dup-deploy", related=["nope"])
    assert code == 2 and out["error"]["code"] == "NOT_FOUND"
    assert _active(ws, "x") == []


def test_dissimilar_fact_passes_and_lists_candidates_below_threshold(ws, capsys):
    _add(capsys, ws, "deploy-steps", "deploy with make release")
    out = _add(capsys, ws, "deploy-related", "near-deploy: staging first")
    cands = out["dedup"]["candidates"]
    assert cands[0]["name"] == "deploy-steps"
    assert cands[0]["score"] < DUPLICATE_GATE_SIMILARITY and cands[0]["blocking"] is False
    assert out["dedup"]["checked"] is True


def test_reflections_skip_the_gate_and_are_never_candidates(ws, capsys):
    _add(capsys, ws, "reflection-1", "deploy notes", type="reflection")
    out = _add(capsys, ws, "reflection-2", "deploy notes again", type="reflection")
    assert out["dedup"]["checked"] is False and out["dedup"]["candidates"] == []
    # A fact identical to a reflection is not gated by it.
    out = _add(capsys, ws, "deploy-fact", "deploy distilled")
    assert out["dedup"]["candidates"] == []


def test_same_name_in_a_parent_tier_blocks_and_vm_row_is_not_created(ws, capsys):
    _add(capsys, ws, "setup", "cache: prefer uv", scope="global")
    code, out = _run(cmd_mem_add, capsys, ws, name="setup", body="deploy: use make")
    assert code == 4
    cand = json.loads(out["error"]["detail"])["candidates"][0]
    assert cand["name"] == "setup" and cand["scope"] == "global"
    assert "name" in cand["match"] and cand["blocking"] is True
    # A refused vm-tier add must not create the vm tier's project row.
    code, out = _run(cmd_mem_add, capsys, ws, scope="vm", name="setup", body="deploy: x")
    assert code == 4
    assert coredb.find_scope_project(ws["ws"].id, "vm", VM) is None
    out = _add(capsys, ws, "setup", "deploy: use make", distinct=True)
    assert out["memory"]["scope"] == "repo"


def test_human_mode_prints_candidates_to_stderr(ws, capsys):
    _add(capsys, ws, "deploy-steps", "deploy with make release")
    with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
        code = cmd_mem_add(_args(ws, name="d2", body="dup-deploy", json=False))
    captured = capsys.readouterr()
    assert code == 4 and captured.out == ""
    assert "deploy-steps" in captured.err and "--distinct" in captured.err


# ---------------------------------------------------------------------------
# WI-1.4 size guidance
# ---------------------------------------------------------------------------


def test_size_warning_on_add_and_update_never_refuses(ws, capsys):
    big = "big " + "x" * FACT_SIZE_GUIDANCE_CHARS
    out = _add(capsys, ws, "big-fact", big)
    assert any("2000-character guidance" in w for w in out["warnings"])
    out = _add(capsys, ws, "big-ref", big + "y", type="reference", distinct=True)
    assert "warnings" not in out
    code, out = _run(cmd_mem_update, capsys, ws, name="big-fact", body=big + "z")
    assert code == 0 and "warnings" in out
    code, out = _run(cmd_mem_update, capsys, ws, name="big-fact", body="big but small")
    assert code == 0 and "warnings" not in out


# ---------------------------------------------------------------------------
# WI-1.2 contradicts links
# ---------------------------------------------------------------------------


def test_contradiction_warns_in_get_search_recall_until_resolved(ws, capsys):
    _add(capsys, ws, "ci-matrix-old", "q-topic: CI tests 3.12 only")
    _add(capsys, ws, "ci-matrix-new", "fact-topic: CI tests 3.12-3.14", distinct=True)
    code, out = _run(
        cmd_mem_link,
        capsys,
        ws,
        from_name="ci-matrix-new",
        to_name="ci-matrix-old",
        kind="contradicts",
        to_scope=None,
    )
    assert code == 0 and out["linked"] is True

    code, out = _run(cmd_mem_get, capsys, ws, name="ci-matrix-old")
    assert code == 0
    assert out["memory"]["contradictions"][0]["name"] == "ci-matrix-new"
    assert out["memory"]["contradictions"][0]["direction"] == "contradicted_by"
    assert len(out["warnings"]) == 1 and "open contradiction" in out["warnings"][0]

    code, out = _run(cmd_mem_search, capsys, ws, query="q-topic", scope="composed")
    by_name = {m["name"]: m for m in out["memories"]}
    assert by_name["ci-matrix-old"]["contradictions"][0]["name"] == "ci-matrix-new"
    assert by_name["ci-matrix-new"]["contradictions"][0]["name"] == "ci-matrix-old"
    assert len(out["warnings"]) == 2

    code, out = _run(cmd_mem_recall, capsys, ws, query="q-topic", scope="composed", budget="mid")
    assert code == 0
    by_ref = {r["source_ref"]: r for r in out["results"]}
    assert "contradictions" in by_ref["ci-matrix-old"]

    # Unlinking resolves it.
    code, _ = _run(
        cmd_mem_unlink,
        capsys,
        ws,
        from_name="ci-matrix-new",
        to_name="ci-matrix-old",
        kind="contradicts",
        to_scope=None,
    )
    assert code == 0
    code, out = _run(cmd_mem_get, capsys, ws, name="ci-matrix-old")
    assert "contradictions" not in out["memory"] and "warnings" not in out


def test_contradiction_across_tiers_closes_when_one_side_is_deleted(ws, capsys):
    _add(capsys, ws, "pref", "cache: prefer pip", scope="global")
    _add(capsys, ws, "tooling", "deploy: we use uv here")
    code, out = _run(
        cmd_mem_link,
        capsys,
        ws,
        from_name="tooling",
        to_name="pref",
        kind="contradicts",
        to_scope="global",
    )
    assert code == 0, out
    assert out["to"] == {"name": "pref", "scope": "global", "scope_key": "global"}
    code, out = _run(cmd_mem_get, capsys, ws, scope="global", name="pref")
    assert out["memory"]["contradictions"][0]["scope"] == "repo"
    assert hygiene_report(project_id=ws["proj"].id)["summary"]["contradictions"] == 1

    assert _run(cmd_mem_delete, capsys, ws, scope="global", name="pref")[0] == 0
    code, out = _run(cmd_mem_get, capsys, ws, name="tooling")
    assert "contradictions" not in out["memory"]
    assert hygiene_report(project_id=ws["proj"].id)["summary"]["contradictions"] == 0


def test_link_refuses_missing_memories_and_self_links(ws, capsys):
    _add(capsys, ws, "a", "deploy: a")
    code, out = _run(
        cmd_mem_link, capsys, ws, from_name="a", to_name="b", kind="contradicts", to_scope=None
    )
    assert code == 2 and out["error"]["code"] == "NOT_FOUND"
    code, out = _run(
        cmd_mem_link, capsys, ws, from_name="a", to_name="a", kind="contradicts", to_scope=None
    )
    assert code == 4
    code, out = _run(
        cmd_mem_unlink, capsys, ws, from_name="a", to_name="c", kind="contradicts", to_scope=None
    )
    assert code == 2


# ---------------------------------------------------------------------------
# WI-1.3 reflections rank below facts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cmd", [cmd_mem_search, cmd_mem_recall])
def test_reflections_are_demoted_by_default(ws, capsys, cmd):
    _add(capsys, ws, "topic-fact", "fact-topic: the answer")
    _add(capsys, ws, "reflection-x", "refl-topic: a session", type="reflection")

    def names(**kw):
        code, out = _run(cmd, capsys, ws, query="q-topic", scope="composed", budget="mid", **kw)
        assert code == 0, out
        items = out.get("memories") or out.get("results") or []
        return [i.get("name") or i.get("source_ref") for i in items], out

    ranked, out = names()
    assert ranked[:2] == ["topic-fact", "reflection-x"]
    assert out["reflections"] == "demote"
    ranked, _ = names(reflections="mixed")
    assert ranked[:2] == ["reflection-x", "topic-fact"]
    ranked, _ = names(reflections="exclude")
    assert "reflection-x" not in ranked and "topic-fact" in ranked


def test_demotion_is_bounded_a_much_closer_reflection_still_wins(ws, capsys):
    _add(capsys, ws, "far-fact", "near-deploy: far")
    _add(capsys, ws, "reflection-y", "dup-deploy: close", type="reflection")
    code, out = _run(cmd_mem_search, capsys, ws, query="deploy", scope="composed")
    scores = {m["name"]: m["score"] for m in out["memories"]}
    assert scores["reflection-y"] - REFLECTION_RANK_PENALTY > scores["far-fact"]
    assert [m["name"] for m in out["memories"]][0] == "reflection-y"


# ---------------------------------------------------------------------------
# WI-1.5 hygiene report
# ---------------------------------------------------------------------------


def test_secret_scan_reports_labels_not_values():
    text = (
        f"token {GITHUB_TOKEN} and AKIAABCDEFGHIJKLMNOP and Bearer "
        + "abcDEF123456789012345678"
        + " -----BEGIN OPENSSH PRIVATE KEY----- postgresql://app:hunter2secret@db/x"
        + " postgresql://user:password@host/db xoxb-1234567890-abc nbt_0123456789abcdef"
        + " sk-ant-"
        + "x" * 30
    )
    found = scan_secrets(text)
    assert found == {
        "bearer_token": 1,
        "netbox_token": 1,
        "github_token": 1,
        "sk_api_key": 1,
        "slack_token": 1,
        "aws_access_key": 1,
        "private_key": 1,
        "url_credentials": 1,  # the 'password' placeholder is not counted
    }
    assert scan_secrets("see the 1Password vault; ghp_short; risk-free; postgres://u:${PW}@h") == {}


def _seed_hygiene(ws, capsys):
    _add(capsys, ws, "dup-a", "deploy: one")
    _add(capsys, ws, "dup-b", "dup-deploy: two", distinct=True)
    _add(capsys, ws, "big-fact", "big " + "x" * 2500)
    _add(capsys, ws, "setup", "cache: prefer uv", scope="global")
    _add(capsys, ws, "setup", "cache: repo override", distinct=True)
    _add(capsys, ws, "stale-fact", "stale: see /projects/app/run.sh and v1.2.3")
    _add(capsys, ws, "stale-plain", "plain: nothing to rot here")
    _add(capsys, ws, "leaky", f"secret: the token is {GITHUB_TOKEN}")
    _add(capsys, ws, "reflection-z", "refl-topic: notes", type="reflection")
    _run(
        cmd_mem_link,
        capsys,
        ws,
        from_name="dup-a",
        to_name="stale-plain",
        kind="contradicts",
        to_scope=None,
    )


def test_hygiene_report_finds_each_kind_of_problem(ws, capsys):
    _seed_hygiene(ws, capsys)
    with psycopg.connect(ws["dsn"]) as conn:
        before = conn.execute("SELECT count(*) FROM change_log").fetchone()[0]

    later = datetime.now(timezone.utc) + timedelta(days=200)
    report = hygiene_report(project_id=ws["proj"].id, now=later)
    s = report["summary"]

    pair = report["near_duplicates"]
    assert [(d["a"]["name"], d["b"]["name"]) for d in pair] == [("dup-a", "dup-b")]
    assert {r["name"] for r in report["oversized"]} == {"big-fact"}
    assert report["shadowed"][0]["name"] == "setup"
    assert report["shadowed"][0]["shadows"][0]["scope"] == "global"
    assert report["contradictions"][0]["memory"]["name"] == "dup-a"
    stale = {r["name"]: r for r in report["stale"]}
    assert set(stale) == {"stale-fact"}
    assert stale["stale-fact"]["mentions"] == ["path", "version"]
    groups = report["reflections_without_followup"]
    assert groups[0]["names"] == ["reflection-z"] and s["reflections_without_followup"] == 1
    secrets = report["possible_secrets"]
    assert [(r["name"], r["patterns"]) for r in secrets] == [("leaky", {"github_token": 1})]
    assert GITHUB_TOKEN not in json.dumps(report, default=str)

    # Not stale today; a later fact follows up the reflection.
    assert hygiene_report(project_id=ws["proj"].id)["summary"]["stale"] == 0
    _add(capsys, ws, "follow-up", "q-topic: distilled lesson")
    after = hygiene_report(project_id=ws["proj"].id)
    assert after["summary"]["reflections_without_followup"] == 0

    # Linking the pair marks it judged.
    _run(cmd_mem_link, capsys, ws, from_name="dup-b", to_name="dup-a", kind="relates_to")
    assert hygiene_report(project_id=ws["proj"].id)["near_duplicates"] == []

    with psycopg.connect(ws["dsn"]) as conn:
        # Only the follow-up add and the link wrote change_log rows.
        assert conn.execute("SELECT count(*) FROM change_log").fetchone()[0] == before + 2


def test_hygiene_cli_project_and_all_modes(ws, capsys):
    _seed_hygiene(ws, capsys)
    code, out = _run(cmd_mem_hygiene, capsys, ws, all=False, stale_days=180)
    assert code == 0
    assert out["scope"] == {"all": False, "workspace": ws["slug"], "project": ws["repo"]}
    assert out["summary"]["near_duplicates"] == 1
    assert out["summary"]["possible_secrets"] == 1
    assert out["elapsed_seconds"] < 5
    assert GITHUB_TOKEN not in json.dumps(out)

    code, out = _run(cmd_mem_hygiene, capsys, ws, all=True, project=None, stale_days=180)
    assert code == 0 and out["scope"] == {"all": True}
    assert ws["repo"] in {d["a"]["scope_key"] for d in out["near_duplicates"]}

    code, out = _run(cmd_mem_hygiene, capsys, ws, all=True, stale_days=180)
    assert code == 4  # --all with --project

    with patch("agent_notes.core.embed.embed", side_effect=_fake_embed):
        code = cmd_mem_hygiene(_args(ws, all=False, stale_days=1, json=False))
    text = capsys.readouterr().out
    assert code == 0 and "Possible secrets" in text and "github_token" in text
    assert GITHUB_TOKEN not in text


# ---------------------------------------------------------------------------
# Review fixes (GPT-6.1-sol, 2026-10-29): one regression test per finding
# ---------------------------------------------------------------------------


def test_r1_hygiene_never_echoes_a_secret_in_a_name(ws, capsys):
    """Finding 1: a token in a memory *name* is redacted in every section."""
    leaky_name = f"notes-{GITHUB_TOKEN}"
    out = _add(capsys, ws, leaky_name, "big " + "x" * 2500)  # also oversized
    row_id = out["memory"]["id"]
    _add(capsys, ws, "body-leak", f"secret: NETBOX_TOKEN={HEX40}")

    report = hygiene_report(project_id=ws["proj"].id)
    dumped = json.dumps(report, default=str)
    assert GITHUB_TOKEN not in dumped and HEX40 not in dumped
    marker = f"[redacted: memory id {row_id}]"
    assert [r["name"] for r in report["oversized"]] == [marker]
    by_id = {r["id"]: r for r in report["possible_secrets"]}
    assert by_id[row_id]["name"] == marker
    assert by_id[row_id]["patterns"] == {"github_token": 1}
    body_leak = next(r for r in report["possible_secrets"] if r["id"] != row_id)
    assert body_leak["name"] == "body-leak"  # a clean name stays readable
    assert set(body_leak["patterns"]) == {"netbox_token", "secret_assignment"}

    code = cmd_mem_hygiene(_args(ws, all=False, stale_days=180, json=False))
    text = capsys.readouterr().out
    assert code == 0 and marker in text and GITHUB_TOKEN not in text
    assert redact_if_secret("plain-name", row_id=1) == "plain-name"


@pytest.mark.parametrize("policy", ["mixed", "demote", "exclude"])
@pytest.mark.parametrize("cmd", [cmd_mem_search, cmd_mem_recall])
def test_r2_cross_tier_name_collision_obeys_the_reflection_policy(ws, capsys, cmd, policy):
    """Finding 2: a repo reflection named like a global fact, under every policy."""
    _add(capsys, ws, "shared", "q-topic: the global fact", scope="global")
    _add(capsys, ws, "shared", "refl-topic: a repo session", type="reflection")
    code, out = _run(
        cmd, capsys, ws, query="q-topic", scope="composed", budget="mid", reflections=policy
    )
    assert code == 0, out
    items = out.get("memories") or out.get("results") or []
    hits = [i for i in items if (i.get("name") or i.get("source_ref")) == "shared"]
    assert len(hits) == 1
    if policy == "exclude":
        assert hits[0]["scope"] == "global"
        assert hits[0]["memory_type"] != "reflection"
        assert hits[0]["shadows"] == []  # the excluded reflection is not listed either
    else:
        assert hits[0]["scope"] == "repo" and hits[0]["memory_type"] == "reflection"
        assert [sh["scope"] for sh in hits[0]["shadows"]] == ["global"]


def test_r3_same_tier_same_name_add_is_a_plain_revision(ws, capsys):
    """Finding 3: re-adding an existing name is never gated, even when it
    shadows a parent-tier name or sits next to near-duplicates."""
    _add(capsys, ws, "setup", "deploy: global way", scope="global")
    _add(capsys, ws, "setup", "deploy: repo override", distinct=True)
    _add(capsys, ws, "deploy-twin", "dup-deploy: a twin", distinct=True)
    out = _add(capsys, ws, "setup", "deploy: repo override, revised")
    assert out["dedup"]["revision"] is True
    assert out["dedup"]["candidates"]  # still shown...
    assert not any(c["blocking"] for c in out["dedup"]["candidates"])  # ...never blocking
    repo_rows = [r for r in _active(ws, "setup") if r["project_id"] == ws["proj"].id]
    assert [r["active"] for r in repo_rows] == [False, True]
    assert out["memory"]["supersedes"] == repo_rows[0]["id"]


def test_r4_choices_bind_to_one_resolved_candidate(ws, capsys):
    """Finding 4: --related NAME answers only the row it links, not every
    candidate called NAME; TIER:NAME selects another tier."""
    _add(capsys, ws, "shared", "deploy: the global one", scope="global")
    _add(capsys, ws, "shared", "deploy: the repo one", distinct=True)

    code, out = _run(cmd_mem_add, capsys, ws, name="new-x", body="dup-deploy", related=["shared"])
    assert code == 4, out
    blocking = json.loads(out["error"]["detail"])["blocking"]
    assert [(b["name"], b["scope"]) for b in blocking] == [("shared", "global")]
    assert _active(ws, "new-x") == []

    out = _add(capsys, ws, "new-x", "dup-deploy", related=["shared", "global:shared"])
    assert [(r["name"], r["scope"]) for r in out["dedup"]["related"]] == [
        ("shared", "repo"),
        ("shared", "global"),
    ]
    with psycopg.connect(ws["dsn"]) as conn:
        targets = conn.execute(
            "SELECT p.kind FROM links l JOIN projects p ON p.id = l.to_project "
            "WHERE l.from_identifier = 'new-x' AND l.from_workspace = %s ORDER BY 1",
            (ws["ws"].id,),
        ).fetchall()
    assert [t[0] for t in targets] == ["global", "repo"]

    code, out = _run(cmd_mem_add, capsys, ws, name="y", body="dup-deploy", related=["vm:shared"])
    assert code == 2 and out["error"]["code"] == "NOT_FOUND"


def test_r5_revision_plus_supersedes_is_refused_before_any_write(ws, capsys):
    """Finding 5: one predecessor per row; nothing is deactivated on refusal."""
    _add(capsys, ws, "a", "deploy: a")
    _add(capsys, ws, "b", "cache: b")
    code, out = _run(cmd_mem_add, capsys, ws, name="a", body="deploy: a2", supersedes="b")
    assert code == 4 and out["error"]["code"] == "VALIDATION_FAILED"
    assert "revision" in out["error"]["message"]
    assert [r["active"] for r in _active(ws, "a")] == [True]
    assert [r["active"] for r in _active(ws, "b")] == [True]
    # The model refuses it too, before any mutation.
    with pytest.raises(ValueError, match="revision"):
        add_memory(
            workspace_id=ws["ws"].id,
            project_id=ws["proj"].id,
            name="a",
            memory_type="project",
            body="deploy: a3",
            supersedes_name="b",
        )
    assert [r["active"] for r in _active(ws, "a")] == [True]
    assert [r["active"] for r in _active(ws, "b")] == [True]


_FOUND = [
    ("BEARER " + "abcDEF1234567890abcdefXYZ", "bearer_token"),
    ("authorization: bearer " + "abcDEF1234567890abcdefXYZ", "bearer_token"),
    ("NETBOX_TOKEN=" + HEX40, "netbox_token"),
    ("the NetBox API token is " + HEX40, "netbox_token"),
    ("NETBOX_TOKEN=" + HEX40, "secret_assignment"),
    ("DB_PASSWORD=s3cr3tP4ssw0rd", "secret_assignment"),
    ("client_secret: Zx9Yw8Vu7Ts6", "secret_assignment"),
    ('{"api_token": "Zx9Yw8Vu7Ts6Rq5"}', "secret_json"),
    ('{"password": "hunter2hunter2"}', "secret_json"),
]


@pytest.mark.parametrize(("text", "label"), _FOUND)
def test_r6_contextual_secrets_are_found(text, label):
    """Finding 6: case-insensitive bearer, assignments, JSON, legacy NetBox."""
    assert label in scan_secrets(text)


_NOT_SECRETS = [
    # Placeholders and references
    "NETBOX_TOKEN=<token>",
    "NETBOX_TOKEN=${NETBOX_TOKEN}",
    "export GITHUB_TOKEN=$GITHUB_TOKEN",
    "password: ${DB_PASSWORD}",
    "password=changeme",
    "DB_PASSWORD: change-me",
    "API_KEY=your-api-key-here",
    "SECRET_KEY=%SECRET_KEY%",
    '{"token": "<your-token>"}',
    '{"password": "changeme"}',
    '{"api_key": "${API_KEY}"}',
    '{"client_secret": "REDACTED"}',
    "Bearer <token>",
    "Authorization: Bearer $TOKEN",
    # Code
    "token = os.environ['NETBOX_TOKEN']",
    "client_secret = os.environ[CLIENT_SECRET]",
    "password = get_password()",
    "token = load_token(path)",
    "api_key = settings.API_KEY",
    "secret: vault.read(secret/path)",
    # Words, prose and configuration that merely mention credentials
    "token: authentication",
    '{"tokenizer": "cl100k_base"}',
    "max_tokens=4096 and token_budget: 8000",
    "MAX_TOKENS: 128000",
    "token_count=1234567890123",
    "password: required for the admin UI",
    "The token: rotate it monthly, see the runbook.",
    "- **Secret:** stored in Vault under kv/agent-notes",
    "| token | where | rotated |",
    "set `password_file: /etc/app/pw.txt` instead of inline secrets",
    "commit " + HEX40 + " fixed the parser",  # a bare SHA, no NetBox context
    "token: 3e6f06e",  # a short commit SHA
    "see https://example.com/docs?token=abc for the flow",
    "use the 1Password vault; ghp_short; risk-free",
]


@pytest.mark.parametrize("text", _NOT_SECRETS)
def test_r6_placeholders_and_lookalikes_do_not_match(text):
    assert scan_secrets(text) == {}


def test_r7_gate_compares_full_precision_and_rounds_only_for_display(ws, capsys):
    """Finding 7: a raw 0.84996 must not become a blocking 0.85."""
    _add(capsys, ws, "edge", "edge-a: base fact")
    out = _add(capsys, ws, "edge-low", "edge-lo: just below")
    cand = next(c for c in out["dedup"]["candidates"] if c["name"] == "edge")
    assert cand["blocking"] is False
    assert cand["score"] == 0.85  # displayed rounded; compared unrounded
    code, out = _run(cmd_mem_add, capsys, ws, name="edge-high", body="edge-hi: just above")
    assert code == 4
    blocking = json.loads(out["error"]["detail"])["blocking"]
    assert {b["name"] for b in blocking} == {"edge"}


# ---------------------------------------------------------------------------
# Review round 2 (GPT-6.1-sol, 2026-10-29)
# ---------------------------------------------------------------------------


def test_r1b_every_string_field_is_redacted(ephemeral_db, capsys):  # noqa: F811
    """Round-2 finding 1: secrets in memory_type, workspace and project slugs."""
    ws_slug, repo = f"ws-{GITHUB_TOKEN}", f"repo-{AWS_KEY}"
    mtype = f"type-{SLACK_TOKEN}"
    w = coredb.get_or_create_workspace(ws_slug, "Secret slugs")
    coredb.add_vocabulary(w.id, "memory_type", mtype)
    proj = coredb.get_or_create_project(w.id, repo, repo, repo_root=os.path.join(_ROOT, "sec"))
    for name in ("plain-a", "plain-b"):  # a near-duplicate pair carries the type
        add_memory(
            workspace_id=w.id,
            project_id=proj.id,
            name=name,
            memory_type=mtype,
            body=f"{name} body",
            embedding=_vec(1.0).tolist(),
        )
    secrets = (GITHUB_TOKEN, AWS_KEY, SLACK_TOKEN)

    report = hygiene_report(project_id=proj.id)
    pair = report["near_duplicates"][0]
    for end in (pair["a"], pair["b"]):
        assert end["memory_type"] == "[redacted]"
        assert end["workspace"] == "[redacted]"
        assert end["scope_key"] == "[redacted]"
    assert {pair["a"]["name"], pair["b"]["name"]} == {"plain-a", "plain-b"}
    dumped = json.dumps(report, default=str)
    assert not any(s in dumped for s in secrets)

    args = argparse.Namespace(
        workspace=ws_slug, project=repo, path=None, json=True, all=False, stale_days=180
    )
    assert cmd_mem_hygiene(args) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["scope"] == {
        "all": False,
        "workspace": "[redacted]",
        "project": "[redacted]",
    }
    assert not any(s in out for s in secrets)
    args.json = False
    assert cmd_mem_hygiene(args) == 0
    text = capsys.readouterr().out
    assert "Near-duplicate" in text and not any(s in text for s in secrets)


_ADVERSARIAL_UNITS = [
    "a-", "a=", "a:", "a ", "x", '"a":', '"a": "', "token=", "token: a1", '"token": "',
    "bearer ", "netbox ", "netbox 0123456789abcdef", "a://", "a:b@", "sk-", "ghp_",
    "-----BEGIN A ", "0a", "key_", "a.",
]  # fmt: skip


@pytest.mark.parametrize("label", sorted(SECRET_PATTERNS))
def test_r2_patterns_are_linear_on_adversarial_input(label):
    """Round-2 finding 2: each pattern scans 200 KB adversarial inputs fast."""
    pattern = SECRET_PATTERNS[label]
    for unit in _ADVERSARIAL_UNITS:
        text = unit * (200_000 // len(unit))
        started = time.perf_counter()
        for _ in pattern.finditer(text):
            pass
        elapsed = time.perf_counter() - started
        assert elapsed < 0.2, f"{label} took {elapsed:.3f}s on {unit!r} x {len(text)}"


def test_r2_scan_of_the_reviewers_input_is_fast():
    started = time.perf_counter()
    assert scan_secrets("a-" * 4000) == {}
    assert scan_secrets("a-" * 100_000) == {}
    assert time.perf_counter() - started < 0.5


@pytest.mark.parametrize("flag", ["related", "supersedes"])
def test_r4b_colon_names_literal_match_wins_over_a_tier_prefix(ws, capsys, flag):
    """Round-2 finding 4: `global:foo` means the memory literally named that,
    when one exists; otherwise the prefix selects a tier. Same for both flags."""
    _add(capsys, ws, "foo", "cache: the global foo", scope="global")
    _add(capsys, ws, "global:foo", "stale: a repo memory with a colon", distinct=True)
    _add(capsys, ws, "repo:bar", "big a repo memory with a colon", distinct=True)
    _add(capsys, ws, "bar", "secret: the plain repo bar", distinct=True)

    # Literal names win.
    for i, spec in enumerate(("global:foo", "repo:bar")):
        out = _add(
            capsys,
            ws,
            f"new-{flag}-{i}",
            f"zz-{flag}-{i} a fresh body",
            **{flag: _flag(flag, spec)},
        )
        if flag == "related":
            assert out["dedup"]["related"] == [
                {"name": spec, "scope": "repo", "scope_key": ws["repo"]}
            ]
        else:
            assert out["dedup"]["supersedes"] == spec
            assert [r["active"] for r in _active(ws, spec)] == [False]

    # With no literal match, the prefix selects the tier.
    _add(capsys, ws, "baz", "plain: a global baz", scope="global")
    _add(capsys, ws, "qux", "plain: a repo qux", distinct=True)
    if flag == "related":
        out = _add(capsys, ws, "new-r-2", "zz-r-2 a fresh body", related=["global:baz"])
        assert out["dedup"]["related"] == [
            {"name": "baz", "scope": "global", "scope_key": "global"}
        ]
    else:
        out = _add(capsys, ws, "new-s-2", "zz-s-2 a fresh body", supersedes="repo:qux")
        assert out["dedup"]["supersedes"] == "qux"
        assert [r["active"] for r in _active(ws, "qux")] == [False]
        # --supersedes never crosses tiers, prefix or not.
        code, out = _run(
            cmd_mem_add,
            capsys,
            ws,
            name="new-s-3",
            body="zz-s-3 a fresh body",
            supersedes="global:baz",
        )
        assert code == 2 and out["error"]["code"] == "NOT_FOUND"
        assert [r["active"] for r in _active(ws, "baz")] == [True]


def _flag(flag: str, spec: str):
    return [spec] if flag == "related" else spec


# ---------------------------------------------------------------------------
# Round 3 review (GPT-6.1-sol): the stale-path detector must stay linear.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("see /projects/app/run.sh", True),
        ("edit ~/.config/agent-notes/config.json", True),
        ("C:\\projects\\agent-notes", True),
        ("change src/agent_notes/cli/memory.py today", True),
        ("rotate logs/app.py.bak", True),
        ("the docs/notes folder", False),
        ("see https://example.com/a/b.py", False),
        ("and/or either/or", False),
        ("plain prose with no path", False),
    ],
)
def test_r3_mentions_path_cases(text, expected):
    from agent_notes.core.memory_hygiene import _mentions_path

    assert _mentions_path(text) is expected


@pytest.mark.parametrize(
    "shape",
    ["a/" * 100_000 + "a", "/a" * 100_000, "a-/" * 70_000, "x " + "a/" * 100_000, ".." * 100_000],
)
def test_r3_mentions_path_is_linear_on_adversarial_input(shape):
    import time

    from agent_notes.core.memory_hygiene import _mentions_path

    started = time.perf_counter()
    _mentions_path(shape)
    assert time.perf_counter() - started < 0.2
