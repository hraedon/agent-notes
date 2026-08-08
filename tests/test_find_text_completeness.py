"""Regression tests for `work-item find --text` completeness (WI-052).

The Plan 020 qualification filed 12 + 5 items titled "Lane C: ..." and
`find --text "Lane C"` silently returned 8/12 and 3/5, which read as index
staleness. The actual cause (measured against the live tracker: all items had
embeddings; the missing ones ranked 13th and 16th by cosine distance) is that
the text search was a pure top-k nearest-neighbour query with a silent default
k of 10 — a similarity *cut*, not a filter, indistinguishable from a complete
answer.

Two behaviours under test:

- items whose title literally contains the query are guaranteed a slot,
  independent of embedding rank (and even with no embedding at all);
- the CLI reports when the semantic cut is full, so a truncated answer no
  longer looks identical to a complete one.

Embeddings here are synthetic unit vectors (no model load): the query vector
points at the "filler" items, so pre-fix the fillers monopolize the top-k and
the literal matches fall below the cut — reproducing the qualification shape.
The ephemeral database is session-scoped and op_ids are content hashes, so
every test gets its own project and identifier prefix.
"""

from __future__ import annotations

import argparse
import json
import uuid
from types import SimpleNamespace

import numpy as np
import pytest

from agent_notes.cli import work_items as cli_work_items
from agent_notes.core import db as coredb
from agent_notes.core.work_item_model import WorkItemModel
from tests.conftest import ephemeral_db  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")

DIM = 768


def _unit(axis: int) -> list[float]:
    vec = [0.0] * DIM
    vec[axis] = 1.0
    return vec


@pytest.fixture
def find_project():
    """A fresh project per test (the session DB accumulates rows)."""
    ws = coredb.get_or_create_workspace("default", "Default Workspace")
    slug = f"findproj-{uuid.uuid4().hex[:8]}"
    return coredb.get_or_create_project(ws.id, slug=slug, name=slug, repo_root=f"/projects/{slug}")


def _file(project_id: int, identifier: str, title: str, embedding) -> None:
    WorkItemModel.file_work_item(
        project_id=project_id,
        identifier=identifier,
        title=title,
        body="body",
        kind="bug",
        status="open",
        embedding=embedding,
    )


@pytest.fixture
def lane_c_corpus(find_project):
    """5 literal matches far from the query vector, 8 near-misses on it."""
    lane_c = [f"LC-{i}" for i in range(5)]
    fillers = [f"FILL-{i}" for i in range(8)]
    for i, ident in enumerate(lane_c):
        _file(find_project.id, ident, f"Lane C: finding number {i}", _unit(1))
    for i, ident in enumerate(fillers):
        _file(find_project.id, ident, f"unrelated filler {i}", _unit(0))
    return SimpleNamespace(project=find_project, lane_c=lane_c, fillers=fillers)


def test_exact_title_matches_survive_the_semantic_cut(lane_c_corpus):
    """Pre-fix: query aimed at the fillers returns 8 fillers + 2 of 5 matches."""
    rows = WorkItemModel.find_work_items(
        query_vec=_unit(0),
        project_id=lane_c_corpus.project.id,
        limit=10,
        query_text="Lane C",
    )
    idents = [r["identifier"] for r in rows]
    assert set(lane_c_corpus.lane_c) <= set(idents)
    assert len(rows) == 10  # the semantic leg still fills the remaining slots
    by_ident = {r["identifier"]: r for r in rows}
    assert all(by_ident[i]["match"] == "title" for i in lane_c_corpus.lane_c)
    filler_rows = [r for r in rows if r["identifier"] in set(lane_c_corpus.fillers)]
    assert len(filler_rows) == 5
    assert all(r["match"] == "semantic" for r in filler_rows)


def test_lexical_match_needs_no_embedding(find_project):
    """A row the embedding pipeline missed is still findable by its title."""
    _file(find_project.id, "NOEMB-1", "Lane C: item without embedding", None)
    rows = WorkItemModel.find_work_items(
        query_vec=_unit(0),
        project_id=find_project.id,
        limit=10,
        query_text="Lane C",
    )
    row = next(r for r in rows if r["identifier"] == "NOEMB-1")
    assert row["match"] == "title"
    assert row["distance"] is None


def test_duplicate_across_both_legs_appears_once(find_project):
    """An item that matches lexically AND ranks in the top-k is not repeated."""
    _file(find_project.id, "BOTH-1", "Lane C: also semantically near", _unit(0))
    rows = WorkItemModel.find_work_items(
        query_vec=_unit(0),
        project_id=find_project.id,
        limit=10,
        query_text="Lane C",
    )
    assert [r["identifier"] for r in rows].count("BOTH-1") == 1


def test_without_query_text_stays_pure_semantic(lane_c_corpus):
    rows = WorkItemModel.find_work_items(
        query_vec=_unit(0),
        project_id=lane_c_corpus.project.id,
        limit=5,
    )
    assert len(rows) == 5
    assert all(r["match"] == "semantic" for r in rows)
    assert set(r["identifier"] for r in rows) <= set(lane_c_corpus.fillers)


def test_like_metacharacters_match_literally(find_project):
    _file(find_project.id, "PCT-1", "restore 100% coverage", _unit(1))
    _file(find_project.id, "PCT-2", "restore 100 items", _unit(1))
    rows = WorkItemModel.find_work_items(
        query_vec=_unit(0),
        project_id=find_project.id,
        limit=10,
        query_text="100%",
    )
    titles = [r["identifier"] for r in rows if r["match"] == "title"]
    assert titles == ["PCT-1"]


class TestCliCutVisibility:
    """The CLI must distinguish a full (possibly truncated) cut from a
    complete answer, in both output modes."""

    @pytest.fixture(autouse=True)
    def _stub_embed(self, monkeypatch):
        monkeypatch.setattr(
            "agent_notes.core.embed.embed",
            lambda text, task="query": np.array(_unit(0)),
        )

    def _args(self, use_json: bool, limit: int | None = None) -> argparse.Namespace:
        return argparse.Namespace(
            json=use_json,
            scope="global",
            text="Lane C",
            limit=limit,
            status=None,
            type=None,
            workspace=None,
            project=None,
            path=None,
        )

    def test_json_reports_a_full_cut_as_incomplete(self, lane_c_corpus, capsys):
        rc = cli_work_items.cmd_wi_find(self._args(use_json=True))
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert len(payload["work_items"]) >= 10
        assert payload["search"]["limit"] == 10
        assert payload["search"]["complete"] is False

    def test_json_reports_an_underfull_result_as_complete(self, find_project, capsys):
        _file(find_project.id, "ONLY-1", "Lane C: a single item", _unit(1))
        args = self._args(use_json=True, limit=50)
        args.scope = "project"
        args.project = find_project.slug
        args.workspace = "default"
        rc = cli_work_items.cmd_wi_find(args)
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert len(payload["work_items"]) < 50
        assert payload["search"]["complete"] is True

    def test_human_output_warns_about_the_cut(self, lane_c_corpus, capsys):
        rc = cli_work_items.cmd_wi_find(self._args(use_json=False))
        assert rc == 0
        out = capsys.readouterr().out
        assert "raise --limit" in out
