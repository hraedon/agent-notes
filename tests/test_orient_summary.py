"""Orientation presentation contracts; database semantics are tested separately."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agent_notes.cli import orient
from agent_notes.core import change_log, config, db, memory_model, messaging
from agent_notes.core.work_item_model import WorkItemModel


@pytest.fixture
def summary_reads(monkeypatch):
    now = datetime.now(timezone.utc)
    work_items = [
        {
            "identifier": f"WI-{i:03}",
            "title": f"Task {i}",
            "status": "open",
            "severity": "medium",
            "updated_at": now - timedelta(days=60),
        }
        for i in range(20)
    ]
    memories = [
        {
            "name": f"decision-{i}",
            "memory_type": "reference",
            "updated_at": now - timedelta(days=2),
            "body_preview": "Body content must not enter orientation",
        }
        for i in range(20)
    ]
    reflection = {
        "name": "reflection-older-handoff",
        "memory_type": "reflection",
        "updated_at": now - timedelta(days=90),
    }
    changes = [
        SimpleNamespace(kind="memory", identifier=f"decision-{i}", event="updated", changed_at=now)
        for i in range(20)
    ]
    calls = []

    def list_memories(**kwargs):
        calls.append(kwargs)
        rows = [reflection] if kwargs.get("memory_type") == "reflection" else memories
        return rows[: kwargs["limit"]]

    monkeypatch.setattr(orient, "_resolve", lambda *_: (1, 2, "test-workspace", "test-project"))
    monkeypatch.setattr(config, "resolve_vm_id", lambda *_: "fixture-vm")
    monkeypatch.setattr(
        db,
        "find_scope_project",
        lambda ws_id, scope, vm_id=None: SimpleNamespace(id=1) if scope == "global" else None,
    )

    def unexpected_database_read():
        pytest.fail("orientation presentation tests must not open a database connection")

    monkeypatch.setattr(db, "_conn", unexpected_database_read)
    monkeypatch.setattr(
        WorkItemModel, "query_work_items", lambda **kwargs: work_items[: kwargs["limit"]]
    )
    monkeypatch.setattr(memory_model, "list_memories", list_memories)
    monkeypatch.setattr(messaging, "live_notices", lambda **_: [])
    monkeypatch.setattr(
        change_log, "changes_since", lambda *_, **kwargs: changes[: kwargs["limit"]]
    )
    return SimpleNamespace(work_items=work_items, memories=memories, changes=changes, calls=calls)


def _args(*flags):
    parser = argparse.ArgumentParser()
    orient.register_orient_parser(parser.add_subparsers(dest="command"))
    return parser.parse_args(
        ["orient", "--workspace", "test-workspace", "--project", "test-project", *flags]
    )


def test_default_is_bounded_with_honest_freshness_and_independent_handoff(summary_reads, capsys):
    assert orient.cmd_orient(_args("--json")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert (
        len(payload["open_work_items"])
        == len(payload["memories"])
        == len(payload["recent_changes"])
        == 5
    )
    more = payload["has_more"]
    assert all(more[k] for k in ("open_work_items", "recent_changes", "memories"))
    item = payload["open_work_items"][0]
    assert item["age_days"] == 60 and item["freshness"] == "aged"
    assert item["updated_at"] and payload["generated_at"]
    assert payload["memories"][0]["freshness"] == "recent"
    assert payload["latest_reflection"]["name"] == "reflection-older-handoff"
    assert payload["latest_reflection"]["age_days"] == 90
    assert "body_preview" not in payload["memories"][0]
    # Project reads are all for project 2; the only other reads are the global
    # tier's (id 1 is the seeded 'global' row), never another project.
    ids = {call["project_id"] for call in summary_reads.calls}
    assert 2 in ids and ids <= {1, 2}


def test_full_and_explicit_limits_preserve_drill_down(summary_reads, capsys):
    orient.cmd_orient(_args("--full", "--json"))
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["open_work_items"]) == 15
    orient.cmd_orient(_args("--full", "--limit", "2", "--stale-after-days", "120", "--json"))
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["memories"]) == 2
    assert payload["open_work_items"][0]["freshness"] == "recent"


def test_empty_sections_are_complete_and_have_no_invented_handoff(
    summary_reads, monkeypatch, capsys
):
    summary_reads.work_items.clear()
    summary_reads.memories.clear()
    summary_reads.changes.clear()
    monkeypatch.setattr(memory_model, "list_memories", lambda **_: [])
    orient.cmd_orient(_args("--json"))
    payload = json.loads(capsys.readouterr().out)
    assert not any(payload["has_more"].values())
    assert payload["latest_reflection"] is None


def test_exact_limit_is_not_mislabeled_as_truncated(summary_reads, capsys):
    del summary_reads.work_items[5:]
    orient.cmd_orient(_args("--json"))
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["open_work_items"]) == 5
    assert payload["has_more"]["open_work_items"] is False


def test_text_only_expands_change_details_on_request(summary_reads, capsys):
    orient.cmd_orient(_args())
    compact = capsys.readouterr().out
    orient.cmd_orient(_args("--full"))
    full = capsys.readouterr().out
    assert len(compact) < len(full)
    assert "[memory] decision-0 updated" not in compact
    assert "[memory] decision-0 updated" in full
    assert "more available" in compact and "aged" in compact


@pytest.mark.parametrize("value", [None, "not a timestamp", "2026-10-01T00:00:00", 0])
def test_unknown_timestamp_is_never_presented_as_fresh(value):
    now = datetime(2026, 10, 15, tzinfo=timezone.utc)
    assert orient._freshness({"updated_at": value}, now, 30) == {
        "updated_at": None,
        "age_days": None,
        "freshness": "unknown",
    }


def test_age_threshold_timezone_and_future_timestamps():
    now = datetime(2026, 10, 15, tzinfo=timezone.utc)
    assert (
        orient._freshness({"updated_at": now - timedelta(days=30)}, now, 30)["freshness"] == "aged"
    )
    assert (
        orient._freshness({"updated_at": now - timedelta(days=30, seconds=-1)}, now, 30)[
            "freshness"
        ]
        == "recent"
    )
    assert orient._freshness({"updated_at": "2026-10-14T17:00:00-07:00"}, now, 30)["age_days"] == 0
    future = orient._freshness({"updated_at": now + timedelta(days=1)}, now, 30)
    assert future["freshness"] == "future" and future["age_days"] is None


@pytest.mark.parametrize(
    "flags", [("--limit", "0"), ("--limit", "101"), ("--days", "-1"), ("--stale-after-days", "0")]
)
def test_invalid_bounds_are_usage_errors(flags):
    with pytest.raises(SystemExit) as exc:
        _args(*flags)
    assert exc.value.code == 2
