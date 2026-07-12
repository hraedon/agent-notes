"""Tests for federated search --federated flag (Plan 020 WI-1.2).

Mocks the memory engine and the exact search to verify the federated seam
degrades honestly and never blocks. Uses the ephemeral_db fixture and the
same cmd_search_all + argparse.Namespace pattern as test_orient_recall.py.
"""

from __future__ import annotations

import argparse
import json
from typing import Any
from unittest.mock import patch

import pytest

from agent_notes.cli.search import cmd_search_all
from agent_notes.core import db as coredb
from agent_notes.core.memory_engine import (
    OriginClass,
    RecallResponse,
    RecallResult,
)
from tests.conftest import ephemeral_db  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")


class _MockEngine:
    """Minimal engine mock for federated search tests."""

    def __init__(
        self,
        engine_name: str,
        recall_response: RecallResponse | None = None,
        recall_raises: Exception | None = None,
    ) -> None:
        self._engine_name = engine_name
        self._recall_response = recall_response
        self._recall_raises = recall_raises

    @property
    def engine_name(self) -> str:
        return self._engine_name

    def recall(self, query: Any) -> RecallResponse:
        if self._recall_raises is not None:
            raise self._recall_raises
        assert self._recall_response is not None
        return self._recall_response


@pytest.fixture
def default_project():
    ws = coredb.get_or_create_workspace("fs-ws", "Federated Search WS")
    return coredb.get_or_create_project(
        ws.id,
        slug="fs-proj",
        name="Federated Search Proj",
        repo_root="/projects/fs-proj",
    )


_FAKE_VEC = [0.0] * 768

_FAKE_EXACT_ROWS = [
    {
        "kind": "memory",
        "workspace_id": 1,
        "project_id": 1,
        "identifier": "mem-001",
        "title": "PostgreSQL connection pooling",
        "score": 0.95,
        "updated_at": "2026-01-01T00:00:00",
    },
    {
        "kind": "breadcrumb",
        "workspace_id": 1,
        "project_id": 1,
        "identifier": "BC-001",
        "title": "Embedding model cold load",
        "score": 0.87,
        "updated_at": "2026-01-02T00:00:00",
    },
]


class _FakeEmbed:
    """Fake embedding object with tolist()."""

    def tolist(self) -> list[float]:
        return _FAKE_VEC


def _make_args(**kwargs: Any) -> argparse.Namespace:
    defaults: dict[str, Any] = {
        "json": True,
        "workspace": None,
        "project": None,
        "path": "/projects/fs-proj",
        "query": "connection pooling",
        "limit": 20,
        "federated": False,
        "kinds": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _recall_result(
    text: str,
    origin: OriginClass = OriginClass.EXTRACTED,
    score: float = 0.9,
    source_ref: str | None = "src-1",
    memory_type: str = "world",
) -> RecallResult:
    return RecallResult(
        text=text,
        origin=origin,
        score=score,
        source_ref=source_ref,
        memory_type=memory_type,
    )


def _patch_exact_search(rows: list[dict] | None = None):
    """Patch embed and search_all_notes to avoid the real model/DB."""
    if rows is None:
        rows = _FAKE_EXACT_ROWS
    embed_patch = patch(
        "agent_notes.core.embed.embed",
        return_value=_FakeEmbed(),
    )
    search_patch = patch(
        "agent_notes.core.search.search_all_notes",
        return_value=rows,
    )
    return embed_patch, search_patch


class TestFederatedNativeEngine:
    """--federated with native engine returns exact + native recall results."""

    def test_native_returns_exact_and_learned_json(self, default_project, capsys):
        engine = _MockEngine(
            engine_name="native",
            recall_response=RecallResponse(
                results=[
                    _recall_result(
                        "pgvector is the search backend",
                        origin=OriginClass.EXTRACTED,
                        score=0.91,
                        source_ref="note-001",
                    )
                ],
                engine="native",
                usage={"count": 1},
            ),
        )
        embed_patch, search_patch = _patch_exact_search()
        with embed_patch, search_patch, patch(
            "agent_notes.core.memory_engine.get_engine", return_value=engine
        ):
            code = cmd_search_all(_make_args(federated=True))
        captured = capsys.readouterr()
        assert code == 0
        payload = json.loads(captured.out)
        assert payload["exact_count"] == 2
        assert payload["learned_count"] == 1
        assert payload["engine"] == "native"
        results = payload["results"]
        assert results[0]["source"] == "exact"
        assert results[0]["kind"] == "memory"
        assert results[2]["source"] == "learned"
        assert results[2]["engine"] == "native"
        assert results[2]["origin"] == "extracted"


class TestFederatedHindsightEngine:
    """--federated with hindsight engine returns exact + learned results."""

    def test_hindsight_returns_exact_and_learned_json(self, default_project, capsys):
        engine = _MockEngine(
            engine_name="hindsight",
            recall_response=RecallResponse(
                results=[
                    _recall_result(
                        "Decided to use pgvector for memory search",
                        origin=OriginClass.EXTRACTED,
                        score=0.92,
                        source_ref="note-001",
                        memory_type="world",
                    ),
                    _recall_result(
                        "The embedding model cold-loads on first use",
                        origin=OriginClass.DERIVED,
                        score=0.78,
                        source_ref=None,
                        memory_type="observation",
                    ),
                ],
                engine="hindsight",
                usage={"count": 2},
            ),
        )
        embed_patch, search_patch = _patch_exact_search()
        with embed_patch, search_patch, patch(
            "agent_notes.core.memory_engine.get_engine", return_value=engine
        ):
            code = cmd_search_all(_make_args(federated=True))
        captured = capsys.readouterr()
        assert code == 0
        payload = json.loads(captured.out)
        assert payload["exact_count"] == 2
        assert payload["learned_count"] == 2
        assert payload["engine"] == "hindsight"
        results = payload["results"]
        assert results[0]["source"] == "exact"
        assert results[1]["source"] == "exact"
        assert results[2]["source"] == "learned"
        assert results[2]["origin"] == "extracted"
        assert results[3]["source"] == "learned"
        assert results[3]["origin"] == "derived"

    def test_hindsight_text_output_source_labels(self, default_project, capsys):
        engine = _MockEngine(
            engine_name="hindsight",
            recall_response=RecallResponse(
                results=[
                    _recall_result(
                        "extracted fact text",
                        origin=OriginClass.EXTRACTED,
                        score=0.85,
                    ),
                    _recall_result(
                        "derived observation text",
                        origin=OriginClass.DERIVED,
                        score=0.72,
                    ),
                ],
                engine="hindsight",
                usage={"count": 2},
            ),
        )
        embed_patch, search_patch = _patch_exact_search()
        with embed_patch, search_patch, patch(
            "agent_notes.core.memory_engine.get_engine", return_value=engine
        ):
            code = cmd_search_all(_make_args(federated=True, json=False))
        captured = capsys.readouterr()
        assert code == 0
        assert "2 exact + 2 learned results:" in captured.out
        assert "[exact]" in captured.out
        assert "[learned:extracted]" in captured.out
        assert "[learned:derived]" in captured.out

    def test_exact_results_come_before_learned(self, default_project, capsys):
        engine = _MockEngine(
            engine_name="hindsight",
            recall_response=RecallResponse(
                results=[
                    _recall_result(
                        "learned result with high score",
                        origin=OriginClass.EXTRACTED,
                        score=0.99,
                    ),
                ],
                engine="hindsight",
                usage={"count": 1},
            ),
        )
        embed_patch, search_patch = _patch_exact_search()
        with embed_patch, search_patch, patch(
            "agent_notes.core.memory_engine.get_engine", return_value=engine
        ):
            code = cmd_search_all(_make_args(federated=True))
        captured = capsys.readouterr()
        assert code == 0
        payload = json.loads(captured.out)
        results = payload["results"]
        sources = [r["source"] for r in results]
        exact_indices = [i for i, s in enumerate(sources) if s == "exact"]
        learned_indices = [i for i, s in enumerate(sources) if s == "learned"]
        assert exact_indices == list(range(len(exact_indices)))
        assert learned_indices == [
            len(exact_indices) + i for i in range(len(learned_indices))
        ]
        assert max(exact_indices) < min(learned_indices)


class TestFederatedDegradation:
    """Honest degradation: learned failures never block search."""

    def test_engine_unreachable_returns_exact_only(self, default_project, capsys):
        engine = _MockEngine(
            engine_name="hindsight",
            recall_raises=ConnectionError("engine down"),
        )
        embed_patch, search_patch = _patch_exact_search()
        with embed_patch, search_patch, patch(
            "agent_notes.core.memory_engine.get_engine", return_value=engine
        ):
            code = cmd_search_all(_make_args(federated=True))
        captured = capsys.readouterr()
        assert code == 0
        payload = json.loads(captured.out)
        assert payload["exact_count"] == 2
        assert payload["learned_count"] == 0
        assert "learned: failed — ConnectionError" in captured.err

    def test_get_engine_value_error_returns_exact_only(self, default_project, capsys):
        embed_patch, search_patch = _patch_exact_search()
        with embed_patch, search_patch, patch(
            "agent_notes.core.memory_engine.get_engine",
            side_effect=ValueError("Unknown memory engine 'foo'"),
        ):
            code = cmd_search_all(_make_args(federated=True))
        captured = capsys.readouterr()
        assert code == 0
        payload = json.loads(captured.out)
        assert payload["exact_count"] == 2
        assert payload["learned_count"] == 0
        assert "learned: skipped (unknown engine configuration)" in captured.err

    def test_engine_empty_results_returns_exact_only(self, default_project, capsys):
        engine = _MockEngine(
            engine_name="hindsight",
            recall_response=RecallResponse(
                results=[],
                engine="hindsight",
                usage={"count": 0},
            ),
        )
        embed_patch, search_patch = _patch_exact_search()
        with embed_patch, search_patch, patch(
            "agent_notes.core.memory_engine.get_engine", return_value=engine
        ):
            code = cmd_search_all(_make_args(federated=True))
        captured = capsys.readouterr()
        assert code == 0
        payload = json.loads(captured.out)
        assert payload["exact_count"] == 2
        assert payload["learned_count"] == 0
        assert "learned: no learned context found" in captured.err

    def test_engine_usage_error_returns_exact_only(self, default_project, capsys):
        engine = _MockEngine(
            engine_name="hindsight",
            recall_response=RecallResponse(
                results=[],
                engine="hindsight",
                usage={"error": "index timeout"},
            ),
        )
        embed_patch, search_patch = _patch_exact_search()
        with embed_patch, search_patch, patch(
            "agent_notes.core.memory_engine.get_engine", return_value=engine
        ):
            code = cmd_search_all(_make_args(federated=True))
        captured = capsys.readouterr()
        assert code == 0
        payload = json.loads(captured.out)
        assert payload["exact_count"] == 2
        assert payload["learned_count"] == 0
        assert "learned: degraded — index timeout" in captured.err

    def test_recall_raises_runtime_error_degrades(self, default_project, capsys):
        engine = _MockEngine(
            engine_name="hindsight",
            recall_raises=RuntimeError("connection reset"),
        )
        embed_patch, search_patch = _patch_exact_search()
        with embed_patch, search_patch, patch(
            "agent_notes.core.memory_engine.get_engine", return_value=engine
        ):
            code = cmd_search_all(_make_args(federated=True))
        captured = capsys.readouterr()
        assert code == 0
        payload = json.loads(captured.out)
        assert payload["exact_count"] == 2
        assert payload["learned_count"] == 0
        assert "learned: failed — RuntimeError" in captured.err


class TestNoFederated:
    """Without --federated, no learned results are returned."""

    def test_no_federated_no_learned_json(self, default_project, capsys):
        embed_patch, search_patch = _patch_exact_search()
        with embed_patch, search_patch:
            code = cmd_search_all(_make_args(federated=False))
        captured = capsys.readouterr()
        assert code == 0
        payload = json.loads(captured.out)
        assert "exact_count" not in payload
        assert "learned_count" not in payload
        assert "engine" not in payload
        assert "results" in payload
        assert len(payload["results"]) == 2

    def test_no_federated_no_learned_text(self, default_project, capsys):
        embed_patch, search_patch = _patch_exact_search()
        with embed_patch, search_patch:
            code = cmd_search_all(_make_args(federated=False, json=False))
        captured = capsys.readouterr()
        assert code == 0
        assert "2 note(s) matched:" in captured.out
        assert "[exact]" not in captured.out
        assert "[learned" not in captured.out
        assert "exact +" not in captured.out

    def test_no_federated_attr_missing_defaults_off(self, default_project, capsys):
        embed_patch, search_patch = _patch_exact_search()
        args = argparse.Namespace(
            json=True,
            workspace=None,
            project=None,
            path="/projects/fs-proj",
            query="connection pooling",
            limit=20,
            kinds=None,
        )
        with embed_patch, search_patch:
            code = cmd_search_all(args)
        captured = capsys.readouterr()
        assert code == 0
        payload = json.loads(captured.out)
        assert "exact_count" not in payload
        assert "learned_count" not in payload
