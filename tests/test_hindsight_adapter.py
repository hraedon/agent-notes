"""Tests for the HindsightEngine adapter (Plan 020 WI-2.1).

Mocks urllib.request.urlopen to simulate Hindsight API responses.
No live HTTP — all calls are intercepted.
"""

from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from agent_notes.core.hindsight_adapter import HindsightEngine, _bank_id
from agent_notes.core.memory_engine import (
    EngineCapability,
    EngineHealthState,
    ForgetSelector,
    IngestBatch,
    IngestItem,
    MemoryScope,
    OperationState,
    OriginClass,
    RecallQuery,
)

_SCOPE = MemoryScope(
    project_slug="agent-suite",
    workspace_slug="default",
    user_id="user-1",
    agent_id="agent-1",
)


class _FakeResponse:
    def __init__(self, body: dict | str, status: int = 200) -> None:
        if isinstance(body, str):
            self._raw = body.encode("utf-8")
        else:
            self._raw = json.dumps(body).encode("utf-8")
        self.status = status

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        pass


def _make_engine(url: str = "https://hindsight.example.com") -> HindsightEngine:
    return HindsightEngine(url=url, tenant="default", api_key=None, timeout=5)


# ---------------------------------------------------------------------------
# from_config / env-var resolution
# ---------------------------------------------------------------------------


class TestFromConfig:
    def test_reads_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HINDSIGHT_URL", "https://hindsight.example.com")
        monkeypatch.setenv("HINDSIGHT_TENANT", "mytenant")
        monkeypatch.setenv("HINDSIGHT_API_KEY", "secret123")
        monkeypatch.setenv("HINDSIGHT_TIMEOUT", "60")

        engine = HindsightEngine.from_config()
        assert engine._url == "https://hindsight.example.com"
        assert engine._tenant == "mytenant"
        assert engine._api_key == "secret123"
        assert engine._timeout == 60

    def test_defaults_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in ("HINDSIGHT_URL", "HINDSIGHT_TENANT", "HINDSIGHT_API_KEY", "HINDSIGHT_TIMEOUT"):
            monkeypatch.delenv(var, raising=False)

        engine = HindsightEngine.from_config()
        assert engine._url is None
        assert engine._tenant == "default"
        assert engine._api_key is None
        assert engine._timeout == 30

    def test_reads_config_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        for var in ("HINDSIGHT_URL", "HINDSIGHT_TENANT", "HINDSIGHT_API_KEY", "HINDSIGHT_TIMEOUT"):
            monkeypatch.delenv(var, raising=False)

        cfg = tmp_path / "config.json"
        cfg.write_text(
            json.dumps(
                {
                    "hindsight": {
                        "url": "https://hindsight-file.example.com",
                        "tenant": "filetenant",
                        "api_key": "filekey",
                        "timeout": 45,
                    }
                }
            )
        )
        monkeypatch.setenv("AGENT_NOTES_CONFIG", str(cfg))

        engine = HindsightEngine.from_config()
        assert engine._url == "https://hindsight-file.example.com"
        assert engine._tenant == "filetenant"
        assert engine._api_key == "filekey"
        assert engine._timeout == 45

    def test_env_overrides_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        cfg = tmp_path / "config.json"
        cfg.write_text(
            json.dumps(
                {
                    "hindsight": {
                        "url": "https://hindsight-file.example.com",
                        "tenant": "filetenant",
                    }
                }
            )
        )
        monkeypatch.setenv("AGENT_NOTES_CONFIG", str(cfg))
        monkeypatch.setenv("HINDSIGHT_URL", "https://hindsight-env.example.com")
        monkeypatch.setenv("HINDSIGHT_TENANT", "envtenant")

        engine = HindsightEngine.from_config()
        assert engine._url == "https://hindsight-env.example.com"
        assert engine._tenant == "envtenant"


# ---------------------------------------------------------------------------
# engine_name
# ---------------------------------------------------------------------------


class TestEngineName:
    def test_returns_hindsight(self) -> None:
        assert _make_engine().engine_name == "hindsight"


# ---------------------------------------------------------------------------
# describe()
# ---------------------------------------------------------------------------


class TestDescribe:
    def test_healthy(self) -> None:
        engine = _make_engine()

        responses = iter(
            [
                _FakeResponse({"status": "ok"}),
                _FakeResponse({"api_version": "0.8.4", "features": {}}),
            ]
        )
        with patch("urllib.request.OpenerDirector.open") as mock_open:
            mock_open.side_effect = lambda req, timeout=None: next(responses)
            health = engine.describe()

        assert health.state == EngineHealthState.HEALTHY
        assert health.engine_name == "hindsight"
        assert health.protocol_version == "1.0"
        assert health.version == "0.8.4"
        assert EngineCapability.INGEST in health.capabilities
        assert EngineCapability.RECALL in health.capabilities
        assert EngineCapability.FORGET in health.capabilities
        assert "hindsight v0.8.4" in health.detail

    def test_unreachable(self) -> None:
        engine = _make_engine()

        with patch("urllib.request.OpenerDirector.open") as mock_open:
            mock_open.side_effect = urllib.error.URLError("connection refused")
            health = engine.describe()

        assert health.state == EngineHealthState.UNREACHABLE
        assert "unreachable" in health.detail

    def test_not_configured(self) -> None:
        engine = HindsightEngine(url=None)
        health = engine.describe()
        assert health.state == EngineHealthState.NOT_CONFIGURED
        assert health.capabilities == frozenset()


# ---------------------------------------------------------------------------
# ingest()
# ---------------------------------------------------------------------------


class TestIngest:
    def test_returns_pending_with_operation_id(self) -> None:
        engine = _make_engine()

        responses = iter(
            [
                _FakeResponse({"success": True}, status=200),
                _FakeResponse(
                    {"success": True, "items_count": 2, "async": True, "operation_id": "op-123"}
                ),
            ]
        )
        with patch("urllib.request.OpenerDirector.open") as mock_open:
            mock_open.side_effect = lambda req, timeout=None: next(responses)

            batch = IngestBatch(
                items=[
                    IngestItem(source_id="doc-1", content="First memory.", memory_type="note"),
                    IngestItem(source_id="doc-2", content="Second memory.", memory_type="note"),
                ],
                scope=_SCOPE,
            )
            result = engine.ingest(batch)

        assert result.state == OperationState.PENDING
        assert result.operation_id == "op-123"
        assert result.items_count == 2
        assert result.error is None

    def test_sync_returns_indexed(self) -> None:
        engine = _make_engine()

        responses = iter(
            [
                _FakeResponse({"success": True}, status=200),
                _FakeResponse({"success": True, "items_count": 1, "async": False}),
            ]
        )
        with patch("urllib.request.OpenerDirector.open") as mock_open:
            mock_open.side_effect = lambda req, timeout=None: next(responses)

            batch = IngestBatch(
                items=[IngestItem(source_id="doc-1", content="Sync memory.", memory_type="note")],
                scope=_SCOPE,
            )
            result = engine.ingest(batch)

        assert result.state == OperationState.INDEXED
        assert result.operation_id is None

    def test_http_error_returns_failed(self) -> None:
        engine = _make_engine()

        with patch("urllib.request.OpenerDirector.open") as mock_open:
            mock_open.side_effect = urllib.error.HTTPError(
                url="https://hindsight.example.com/v1/default/banks/agent_suite/memories",
                code=500,
                msg="Internal Server Error",
                hdrs=MagicMock(),
                fp=io.BytesIO(b'{"error": "server error"}'),
            )

            batch = IngestBatch(
                items=[IngestItem(source_id="doc-1", content="fail", memory_type="note")],
                scope=_SCOPE,
            )
            result = engine.ingest(batch)

        assert result.state == OperationState.FAILED
        assert result.error is not None

    def test_not_configured(self) -> None:
        engine = HindsightEngine(url=None)
        result = engine.ingest(
            IngestBatch(
                items=[IngestItem(source_id="x", content="y", memory_type="note")],
                scope=_SCOPE,
            )
        )
        assert result.state == OperationState.FAILED
        assert "not configured" in (result.error or "")


# ---------------------------------------------------------------------------
# operation_status()
# ---------------------------------------------------------------------------


class TestOperationStatus:
    @pytest.mark.parametrize(
        ("hind_status", "expected"),
        [
            ("pending", OperationState.PENDING),
            ("processing", OperationState.PENDING),
            ("completed", OperationState.INDEXED),
            ("failed", OperationState.FAILED),
            ("cancelled", OperationState.CANCELLED),
        ],
    )
    def test_maps_status(self, hind_status: str, expected: OperationState) -> None:
        engine = _make_engine()

        with patch("urllib.request.OpenerDirector.open") as mock_open:
            mock_open.side_effect = lambda req, timeout=None: _FakeResponse(
                {"operation_id": "op-1", "status": hind_status}
            )
            status = engine.operation_status("op-1")

        assert status.state == expected
        assert status.operation_id == "op-1"


# ---------------------------------------------------------------------------
# recall()
# ---------------------------------------------------------------------------


class TestRecall:
    def test_maps_results(self) -> None:
        engine = _make_engine()

        resp_body = {
            "results": [
                {
                    "id": "r1",
                    "text": "The agent uses pgvector for search.",
                    "type": "world",
                    "entities": [],
                    "chunk_id": "chunk-1",
                },
                {
                    "id": "r2",
                    "text": "An observation about behavior.",
                    "type": "observation",
                    "chunk_id": "chunk-2",
                },
            ]
        }
        with patch("urllib.request.OpenerDirector.open") as mock_open:
            mock_open.side_effect = lambda req, timeout=None: _FakeResponse(resp_body)

            query = RecallQuery(query="vector search", scope=_SCOPE)
            response = engine.recall(query)

        assert response.engine == "hindsight"
        assert len(response.results) == 2
        assert response.results[0].origin == OriginClass.EXTRACTED
        assert response.results[1].origin == OriginClass.DERIVED
        assert response.results[0].text == "The agent uses pgvector for search."

    def test_error_returns_empty(self) -> None:
        engine = _make_engine()

        with patch("urllib.request.OpenerDirector.open") as mock_open:
            mock_open.side_effect = urllib.error.URLError("timeout")
            response = engine.recall(RecallQuery(query="x", scope=_SCOPE))

        assert response.engine == "hindsight"
        assert len(response.results) == 0
        assert "error" in response.usage

    def test_not_configured(self) -> None:
        engine = HindsightEngine(url=None)
        response = engine.recall(RecallQuery(query="x", scope=_SCOPE))
        assert len(response.results) == 0
        assert "error" in response.usage


# ---------------------------------------------------------------------------
# forget()
# ---------------------------------------------------------------------------


class TestForget:
    def test_by_source_id_sends_delete(self) -> None:
        engine = _make_engine()

        with patch("urllib.request.OpenerDirector.open") as mock_open:
            mock_open.side_effect = lambda req, timeout=None: _FakeResponse(
                {"deleted": True, "cascade_count": 3}
            )
            result = engine.forget(ForgetSelector(source_id="doc-1", scope=_SCOPE))

        assert result.deleted_count == 1
        assert result.cascade_count == 3
        assert result.receipt is not None
        assert result.error is None

        called_req = mock_open.call_args[0][0]
        assert called_req.method == "DELETE"
        assert "documents/doc-1" in called_req.full_url

    def test_by_scope_deletes_bank(self) -> None:
        engine = _make_engine()

        with patch("urllib.request.OpenerDirector.open") as mock_open:
            mock_open.side_effect = lambda req, timeout=None: _FakeResponse(
                {"deleted_count": 42}
            )
            result = engine.forget(ForgetSelector(scope=_SCOPE))

        assert result.deleted_count == 42
        assert result.receipt is not None
        assert "bank_deleted" in result.receipt

        called_req = mock_open.call_args[0][0]
        assert called_req.method == "DELETE"
        assert "banks/agent_suite" in called_req.full_url

    def test_error_returns_failed(self) -> None:
        engine = _make_engine()

        with patch("urllib.request.OpenerDirector.open") as mock_open:
            mock_open.side_effect = urllib.error.URLError("timeout")
            result = engine.forget(ForgetSelector(source_id="doc-1", scope=_SCOPE))

        assert result.deleted_count == 0
        assert result.error is not None

    def test_no_selector(self) -> None:
        engine = _make_engine()
        result = engine.forget(ForgetSelector())
        assert result.deleted_count == 0
        assert result.error is not None


# ---------------------------------------------------------------------------
# bank_id mapping
# ---------------------------------------------------------------------------


class TestBankId:
    def test_hyphens_to_underscores(self) -> None:
        assert _bank_id("agent-suite") == "agent_suite"

    def test_lowercase(self) -> None:
        assert _bank_id("MyProject") == "myproject"

    def test_mixed_special_chars(self) -> None:
        assert _bank_id("my.project-name") == "my_project_name"

    def test_already_valid(self) -> None:
        assert _bank_id("my_proj") == "my_proj"


# ---------------------------------------------------------------------------
# SSRF guard
# ---------------------------------------------------------------------------


class TestSSRFGuard:
    def test_rejects_non_http_scheme(self) -> None:
        with pytest.raises(ValueError, match="Invalid URL scheme"):
            HindsightEngine(url="file:///etc/passwd")

    def test_rejects_ftp_scheme(self) -> None:
        with pytest.raises(ValueError, match="Invalid URL scheme"):
            HindsightEngine(url="ftp://evil.example.com")

    def test_rejects_redirect(self) -> None:
        engine = _make_engine()

        redirect_err = urllib.error.HTTPError(
            url="https://hindsight.example.com/health",
            code=302,
            msg="Found",
            hdrs=MagicMock(),
            fp=io.BytesIO(b""),
        )
        with patch("urllib.request.OpenerDirector.open") as mock_open:
            mock_open.side_effect = redirect_err
            health = engine.describe()

        assert health.state == EngineHealthState.UNREACHABLE
        assert "redirect" in health.detail or "SSRF" in health.detail
