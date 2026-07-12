"""Tests for orient --recall (Plan 020 WI-3.2).

Mocks the memory engine to verify the learned-context seam degrades honestly
and never blocks orient. Uses the ephemeral_db fixture and the same
cmd_orient + argparse.Namespace pattern as test_p3_projection_sync.py.
"""

from __future__ import annotations

import argparse
import json
from typing import Any
from unittest.mock import patch

import pytest

from agent_notes.cli.orient import cmd_orient
from agent_notes.core import db as coredb
from agent_notes.core.memory_engine import (
    EngineCapability,
    EngineHealth,
    EngineHealthState,
    OriginClass,
    RecallResponse,
    RecallResult,
)
from tests.conftest import ephemeral_db  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")

_CAPS = frozenset({EngineCapability.RECALL})


class _MockEngine:
    """Minimal engine mock for orient recall tests."""

    def __init__(
        self,
        engine_name: str,
        health_state: EngineHealthState,
        recall_response: RecallResponse | None = None,
        recall_raises: Exception | None = None,
    ) -> None:
        self._engine_name = engine_name
        self._health_state = health_state
        self._recall_response = recall_response
        self._recall_raises = recall_raises

    @property
    def engine_name(self) -> str:
        return self._engine_name

    def describe(self) -> EngineHealth:
        return EngineHealth(
            state=self._health_state,
            capabilities=_CAPS,
            engine_name=self._engine_name,
            detail="mock",
        )

    def recall(self, query: Any) -> RecallResponse:
        if self._recall_raises is not None:
            raise self._recall_raises
        assert self._recall_response is not None
        return self._recall_response


@pytest.fixture
def default_project():
    ws = coredb.get_or_create_workspace("or-ws", "Orient Recall WS")
    return coredb.get_or_create_project(
        ws.id,
        slug="or-proj",
        name="Orient Recall Proj",
        repo_root="/projects/or-proj",
    )


def _make_args(**kwargs: Any) -> argparse.Namespace:
    defaults: dict[str, Any] = {
        "json": True,
        "workspace": None,
        "project": None,
        "path": "/projects/or-proj",
        "days": 7,
        "limit": 15,
        "reconcile": False,
        "recall": False,
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


class TestOrientRecallNative:
    """Native engine: recall is skipped (memories already listed)."""

    def test_native_skip_no_learned_context(self, default_project, capsys):
        engine = _MockEngine(
            engine_name="native",
            health_state=EngineHealthState.HEALTHY,
            recall_response=RecallResponse(
                results=[_recall_result("should not appear")],
                engine="native",
                usage={"count": 1},
            ),
        )
        with patch(
            "agent_notes.core.memory_engine.get_engine", return_value=engine
        ):
            code = cmd_orient(_make_args(recall=True))
        captured = capsys.readouterr()
        assert code == 0
        payload = json.loads(captured.out)
        assert "learned_context" not in payload

    def test_native_skip_text_note(self, default_project, capsys):
        engine = _MockEngine(
            engine_name="native",
            health_state=EngineHealthState.HEALTHY,
        )
        with patch(
            "agent_notes.core.memory_engine.get_engine", return_value=engine
        ):
            code = cmd_orient(_make_args(recall=True, json=False))
        captured = capsys.readouterr()
        assert code == 0
        assert "recall: skipped (native engine" in captured.out


class TestOrientRecallHindsight:
    """External engine: recall returns learned context."""

    def test_hindsight_returns_learned_context_json(self, default_project, capsys):
        results = [
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
        ]
        engine = _MockEngine(
            engine_name="hindsight",
            health_state=EngineHealthState.HEALTHY,
            recall_response=RecallResponse(
                results=results,
                engine="hindsight",
                usage={"count": 2},
            ),
        )
        with patch(
            "agent_notes.core.memory_engine.get_engine", return_value=engine
        ):
            code = cmd_orient(_make_args(recall=True))
        captured = capsys.readouterr()
        assert code == 0
        payload = json.loads(captured.out)
        lc = payload["learned_context"]
        assert lc["engine"] == "hindsight"
        assert lc["error"] is None
        assert len(lc["results"]) == 2
        assert lc["results"][0]["text"] == "Decided to use pgvector for memory search"
        assert lc["results"][0]["origin"] == "extracted"
        assert lc["results"][1]["origin"] == "derived"
        assert lc["usage"]["count"] == 2

    def test_hindsight_text_output_origin_labels(self, default_project, capsys):
        results = [
            _recall_result("verbatim source text", origin=OriginClass.RAW),
            _recall_result("extracted fact text", origin=OriginClass.EXTRACTED),
            _recall_result("derived observation text", origin=OriginClass.DERIVED),
            _recall_result(
                "synthesized text", origin=OriginClass.SYNTHESIZED
            ),
        ]
        engine = _MockEngine(
            engine_name="hindsight",
            health_state=EngineHealthState.HEALTHY,
            recall_response=RecallResponse(
                results=results,
                engine="hindsight",
                usage={"count": 4},
            ),
        )
        with patch(
            "agent_notes.core.memory_engine.get_engine", return_value=engine
        ):
            code = cmd_orient(_make_args(recall=True, json=False))
        captured = capsys.readouterr()
        assert code == 0
        assert "Learned context (hindsight):" in captured.out
        assert "[exact source (verbatim)]" in captured.out
        assert "[extracted fact]" in captured.out
        assert "[derived observation]" in captured.out
        assert "[synthesized (LLM-generated — untrusted)]" in captured.out


class TestOrientRecallDegradation:
    """Honest degradation: recall failures never block orient."""

    def test_hindsight_unreachable_degrades(self, default_project, capsys):
        engine = _MockEngine(
            engine_name="hindsight",
            health_state=EngineHealthState.UNREACHABLE,
        )
        with patch(
            "agent_notes.core.memory_engine.get_engine", return_value=engine
        ):
            code = cmd_orient(_make_args(recall=True))
        captured = capsys.readouterr()
        assert code == 0
        payload = json.loads(captured.out)
        assert "learned_context" not in payload

    def test_hindsight_unreachable_text_message(self, default_project, capsys):
        engine = _MockEngine(
            engine_name="hindsight",
            health_state=EngineHealthState.UNREACHABLE,
        )
        with patch(
            "agent_notes.core.memory_engine.get_engine", return_value=engine
        ):
            code = cmd_orient(_make_args(recall=True, json=False))
        captured = capsys.readouterr()
        assert code == 0
        assert "recall: engine unreachable" in captured.out

    def test_hindsight_not_configured_skips_silently(self, default_project, capsys):
        engine = _MockEngine(
            engine_name="hindsight",
            health_state=EngineHealthState.NOT_CONFIGURED,
        )
        with patch(
            "agent_notes.core.memory_engine.get_engine", return_value=engine
        ):
            code = cmd_orient(_make_args(recall=True, json=False))
        captured = capsys.readouterr()
        assert code == 0
        assert "recall:" not in captured.out

    def test_hindsight_empty_results(self, default_project, capsys):
        engine = _MockEngine(
            engine_name="hindsight",
            health_state=EngineHealthState.HEALTHY,
            recall_response=RecallResponse(
                results=[],
                engine="hindsight",
                usage={"count": 0},
            ),
        )
        with patch(
            "agent_notes.core.memory_engine.get_engine", return_value=engine
        ):
            code = cmd_orient(_make_args(recall=True, json=False))
        captured = capsys.readouterr()
        assert code == 0
        assert "recall: no learned context found" in captured.out

    def test_hindsight_usage_error_degrades(self, default_project, capsys):
        engine = _MockEngine(
            engine_name="hindsight",
            health_state=EngineHealthState.HEALTHY,
            recall_response=RecallResponse(
                results=[],
                engine="hindsight",
                usage={"error": "index timeout"},
            ),
        )
        with patch(
            "agent_notes.core.memory_engine.get_engine", return_value=engine
        ):
            code = cmd_orient(_make_args(recall=True, json=False))
        captured = capsys.readouterr()
        assert code == 0
        assert "recall: degraded — index timeout" in captured.out

    def test_recall_raises_exception_degrades(self, default_project, capsys):
        engine = _MockEngine(
            engine_name="hindsight",
            health_state=EngineHealthState.HEALTHY,
            recall_raises=RuntimeError("connection reset"),
        )
        with patch(
            "agent_notes.core.memory_engine.get_engine", return_value=engine
        ):
            code = cmd_orient(_make_args(recall=True, json=False))
        captured = capsys.readouterr()
        assert code == 0
        assert "recall: failed — RuntimeError" in captured.out

    def test_get_engine_value_error_degrades(self, default_project, capsys):
        with patch(
            "agent_notes.core.memory_engine.get_engine",
            side_effect=ValueError("Unknown memory engine 'foo'"),
        ):
            code = cmd_orient(_make_args(recall=True, json=False))
        captured = capsys.readouterr()
        assert code == 0
        assert "recall: skipped (unknown engine configuration)" in captured.out


class TestOrientNoRecall:
    """Without --recall, the payload is unchanged (no learned_context)."""

    def test_no_recall_no_learned_context_json(self, default_project, capsys):
        code = cmd_orient(_make_args(recall=False))
        captured = capsys.readouterr()
        assert code == 0
        payload = json.loads(captured.out)
        assert "learned_context" not in payload

    def test_no_recall_no_learned_context_text(self, default_project, capsys):
        code = cmd_orient(_make_args(recall=False, json=False))
        captured = capsys.readouterr()
        assert code == 0
        assert "Learned context" not in captured.out
        assert "recall:" not in captured.out

    def test_default_args_no_recall_attr(self, default_project, capsys):
        args = argparse.Namespace(
            json=True,
            workspace=None,
            project=None,
            path="/projects/or-proj",
            days=7,
            limit=15,
            reconcile=False,
        )
        code = cmd_orient(args)
        captured = capsys.readouterr()
        assert code == 0
        payload = json.loads(captured.out)
        assert "learned_context" not in payload
