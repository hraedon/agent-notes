"""The documented review invocation, end to end through the CLI (WI-079).

The adversarial-review skill tells a reviewer to set identity through the
environment only (``AGENT_NOTES_ACTOR_ID`` plus ``AGENT_NOTES_MODEL``) and run
``work-item review pass|accept <id> --note ...``. These tests run exactly that
as a hermetic CLI subprocess, so a skew between the skill, AGENTS.md and the
CLI shows up as a failure here: the invocation either succeeds for a distinct
reviewer or fails for the documented gate reason, never for configuration.
"""

from __future__ import annotations

import json
import uuid

import pytest

from agent_notes.core import db as coredb
from agent_notes.core import messaging
from agent_notes.core.work_item import _independence
from agent_notes.core.work_item_model import WorkItemModel
from tests.cli_harness import run_cli
from tests.conftest import ephemeral_db  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")

IMPLEMENTER = ("agent:implementer", "claude-opus-5-5")

# Identity the CLI must not inherit from the host session running the tests.
_STRIP = (
    "AGENT_NOTES_ACTOR_ID",
    "REGISTA_PRINCIPAL_ID",
    *_independence.MODEL_ENVS,
    *_independence.HARNESS_ENVS,
    messaging.SESSION_ENV,
    *(var for var, _ in messaging.HARNESS_ENV_VARS),
)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    for name in _STRIP:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def item(monkeypatch) -> tuple[str, str]:
    """File an item as the implementer and drive it into review; return (project, id)."""
    ws = coredb.get_or_create_workspace("default", "Default Workspace")
    slug = f"wi079-{uuid.uuid4().hex[:8]}"
    proj = coredb.get_or_create_project(ws.id, slug=slug, name=slug, repo_root=f"/projects/{slug}")
    identifier = "WI-1"
    monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", IMPLEMENTER[0])
    monkeypatch.setenv("AGENT_NOTES_MODEL", IMPLEMENTER[1])
    WorkItemModel.file_work_item(
        project_id=proj.id,
        identifier=identifier,
        title="review identity contract",
        status="open",
        embedding=[0.0] * 768,
    )
    WorkItemModel.update_work_item(project_id=proj.id, identifier=identifier, status="in_progress")
    WorkItemModel.update_work_item(project_id=proj.id, identifier=identifier, status="in_review")
    for name in ("AGENT_NOTES_ACTOR_ID", "AGENT_NOTES_MODEL"):
        monkeypatch.delenv(name)
    return slug, identifier


def _review(
    verdict: str, slug: str, identifier: str, actor: str, model: str | None, *extra: str
) -> tuple[int, dict]:
    env = {"AGENT_NOTES_ACTOR_ID": actor}
    if model is not None:
        env["AGENT_NOTES_MODEL"] = model
    result = run_cli(
        "work-item",
        "review",
        verdict,
        identifier,
        "--workspace",
        "default",
        "--project",
        slug,
        "--note",
        "Checked the change against its tests; sound.",
        *extra,
        "--json",
        env=env,
        check=False,
        strip_keys=_STRIP,
    )
    out = result.stdout.strip() or result.stderr.strip()
    return result.returncode, json.loads(out) if out.startswith("{") else {"raw": out}


def test_env_only_cross_lineage_review_passes_and_accepts(item):
    slug, identifier = item
    code, out = _review("pass", slug, identifier, "agent:reviewer", "gpt-6.1-sol")
    assert code == 0, out
    assert out["work_item"]["status"] == "in_human_review"

    code, out = _review("accept", slug, identifier, "agent:accepter", "gemini-4-argon-medium")
    assert code == 0, out
    assert out["work_item"]["status"] == "done"


def test_env_only_same_lineage_review_fails_for_the_gate_reason(item):
    slug, identifier = item
    code, out = _review("pass", slug, identifier, "agent:reviewer", "claude-sonnet-5-5")
    assert code != 0
    assert out["error"]["code"] == "VALIDATION_FAILED", out
    message = out["error"]["message"]
    assert "lineage" in message and "--same-lineage-acknowledged" in message, message


def test_env_only_unknown_model_fails_for_the_gate_reason(item):
    slug, identifier = item
    code, out = _review("pass", slug, identifier, "agent:reviewer", None)
    assert code != 0
    assert out["error"]["code"] == "VALIDATION_FAILED", out
    assert "unknown" in out["error"]["message"], out


def test_same_lineage_acknowledgement_is_the_documented_override(item):
    slug, identifier = item
    code, out = _review(
        "pass",
        slug,
        identifier,
        "agent:reviewer",
        "claude-sonnet-5-5",
        "--same-lineage-acknowledged",
        "trivial doc change; no other model available",
    )
    assert code == 0, out
    assert out["work_item"]["status"] == "in_human_review"


@pytest.mark.parametrize("flag", ["--actor-id", "--model-lineage"])
def test_there_are_no_per_command_identity_overrides(item, flag):
    slug, identifier = item
    code, out = _review("pass", slug, identifier, "agent:reviewer", "gpt-6.1-sol", flag, "x")
    assert code == 2, out
    assert "unrecognized arguments" in out.get("raw", ""), out
