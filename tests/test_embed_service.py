"""The warm embedding service and the client path in core.embed.

No model is loaded: ``embed_local`` and ``_get_model`` are replaced with fakes,
so these tests cover the transport, fallback and drift guards only.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from types import SimpleNamespace

import numpy as np
import pytest

from agent_notes import embed_server
from agent_notes.core import embed


def _fake_local(text: str, task: str = "document") -> np.ndarray:
    rng = np.random.default_rng(abs(hash((text, task))) % (2**32))
    vec = rng.standard_normal(embed._EMBED_DIM).astype(np.float32)
    return vec / np.linalg.norm(vec)


@pytest.fixture
def fake_model(monkeypatch):
    calls: list[tuple[str, str]] = []

    def local(text, task="document"):
        calls.append((text, task))
        return _fake_local(text, task)

    monkeypatch.setattr(embed, "embed_local", local)
    monkeypatch.setattr(embed, "_get_model", lambda: SimpleNamespace(device="cpu"))
    monkeypatch.setattr(embed, "_service_down", False)
    monkeypatch.setattr("agent_notes.core.suite_env.load_suite_env", lambda: {})
    return calls


@pytest.fixture
def service(fake_model):
    server = embed_server.build_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def _post(url: str, body: bytes) -> tuple[int, dict]:
    req = urllib.request.Request(
        url + "/embed", data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_embed_uses_service_and_matches_local(service, fake_model, monkeypatch):
    monkeypatch.setenv("AGENT_NOTES_EMBED_URL", service)
    vec = embed.embed("postgres tuning", task="query")
    assert vec.dtype == np.float32
    assert np.array_equal(vec, _fake_local("postgres tuning", "query"))
    # The server did the encode; it went through embed_local exactly once.
    assert fake_model == [("postgres tuning", "query")]


def test_health_reports_model_and_dim(service):
    data = embed.request_service(service, "/health")
    assert data == {"model": embed._MODEL_NAME, "dim": embed._EMBED_DIM, "device": "cpu"}


def test_unset_url_encodes_in_process(fake_model, monkeypatch):
    monkeypatch.delenv("AGENT_NOTES_EMBED_URL", raising=False)
    embed.embed("hello", task="document")
    assert fake_model == [("hello", "document")]


def test_suite_env_url_is_used(service, fake_model, monkeypatch):
    monkeypatch.delenv("AGENT_NOTES_EMBED_URL", raising=False)
    monkeypatch.setattr(
        "agent_notes.core.suite_env.load_suite_env", lambda: {"AGENT_NOTES_EMBED_URL": service}
    )
    assert embed.service_url() == service


def test_unreachable_service_falls_back_once(fake_model, monkeypatch, caplog):
    # Bind and close a socket to get a port with nothing listening.
    server = embed_server.build_server("127.0.0.1", 0)
    port = server.server_address[1]
    server.server_close()
    monkeypatch.setenv("AGENT_NOTES_EMBED_URL", f"http://127.0.0.1:{port}")
    requests = []
    real = embed.request_service
    monkeypatch.setattr(
        embed, "request_service", lambda *a, **k: requests.append(a) or real(*a, **k)
    )

    with caplog.at_level(logging.WARNING, logger="agent_notes.core.embed"):
        embed.embed("one", task="query")
        embed.embed("two", task="query")

    assert fake_model == [("one", "query"), ("two", "query")]
    assert len(requests) == 1  # the second call doesn't wait on the dead service
    assert "loading the model in-process" in caplog.text


def test_model_mismatch_fails_instead_of_mixing(fake_model, monkeypatch):
    monkeypatch.setenv("AGENT_NOTES_EMBED_URL", "http://127.0.0.1:1")
    monkeypatch.setattr(
        embed,
        "request_service",
        lambda *a, **k: {"model": "other/model", "dim": embed._EMBED_DIM, "vectors": [[0.0]]},
    )
    with pytest.raises(embed.EmbedServiceError, match="other/model"):
        embed.embed("x")
    assert fake_model == []


def test_service_error_is_raised_not_swallowed(service, fake_model, monkeypatch):
    def boom(text, task="document"):
        raise RuntimeError("model broke")

    monkeypatch.setattr(embed, "embed_local", boom)
    monkeypatch.setenv("AGENT_NOTES_EMBED_URL", service)
    with pytest.raises(embed.EmbedServiceError, match="HTTP 500"):
        embed.embed("x")


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        json.dumps({"texts": "one string"}).encode(),
        json.dumps({"texts": []}).encode(),
        json.dumps({"texts": [1]}).encode(),
        json.dumps({"texts": ["a"], "task": "classify"}).encode(),
    ],
)
def test_bad_requests_are_400(service, body):
    status, data = _post(service, body)
    assert status == 400
    assert "error" in data


def test_batch_returns_one_vector_per_text(service):
    status, data = _post(service, json.dumps({"texts": ["a", "b"], "task": "query"}).encode())
    assert status == 200
    assert len(data["vectors"]) == 2
    assert len(data["vectors"][0]) == embed._EMBED_DIM


@pytest.fixture
def token_service(fake_model):
    server = embed_server.build_server("127.0.0.1", 0, token="s3cret")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_token_is_sent_and_accepted(token_service, fake_model, monkeypatch):
    monkeypatch.setenv("AGENT_NOTES_EMBED_URL", token_service)
    monkeypatch.setenv("AGENT_NOTES_EMBED_TOKEN", "s3cret")
    embed.embed("x", task="query")
    assert fake_model == [("x", "query")]


@pytest.mark.parametrize("token", [None, "wrong"])
def test_missing_or_wrong_token_fails_without_fallback(
    token_service, fake_model, monkeypatch, token
):
    monkeypatch.setenv("AGENT_NOTES_EMBED_URL", token_service)
    if token:
        monkeypatch.setenv("AGENT_NOTES_EMBED_TOKEN", token)
    else:
        monkeypatch.delenv("AGENT_NOTES_EMBED_TOKEN", raising=False)
    with pytest.raises(embed.EmbedServiceError, match="HTTP 401"):
        embed.embed("x")
    assert fake_model == []


def test_health_needs_no_token(token_service, monkeypatch):
    monkeypatch.delenv("AGENT_NOTES_EMBED_TOKEN", raising=False)
    assert embed.request_service(token_service, "/health")["dim"] == embed._EMBED_DIM


def test_gateway_error_falls_back(fake_model, monkeypatch):
    """A 503 from an ingress with no ready pod is 'unreachable', not a hard failure."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Gateway(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.send_response(503)
            self.send_header("Content-Length", "11")
            self.end_headers()
            self.wfile.write(b"no backend!")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("AGENT_NOTES_EMBED_URL", f"http://127.0.0.1:{server.server_address[1]}")
        embed.embed("x", task="query")
    finally:
        server.shutdown()
        server.server_close()
    assert fake_model == [("x", "query")]


def test_rejects_non_http_url(fake_model, monkeypatch):
    monkeypatch.setenv("AGENT_NOTES_EMBED_URL", "ftp://example.test")
    with pytest.raises(embed.EmbedServiceError, match="http:// or https://"):
        embed.embed("x")
