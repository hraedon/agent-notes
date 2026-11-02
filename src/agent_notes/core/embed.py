"""Embedding: a warm embedding service when configured, else in-process.

In-process lazy singleton (decision 2 / decision 22): thread-safe via
double-checked locking. The first call loads the ~270MB model and keeps it
resident for the process lifetime. Reads AGENT_NOTES_EMBED_MODEL and
AGENT_NOTES_EMBED_DIM env vars so operators can swap models (MiniLM-384,
BGE-large-1024) without code changes.

A CLI call is a fresh process, so the in-process path pays the torch +
sentence-transformers import (~9s) on every search or write while the encode
itself takes ~15ms. When ``AGENT_NOTES_EMBED_URL`` is set (process env, then
suite.env), ``embed()`` asks a long-running ``agent-notes-embed-server`` for
the vector instead, sending ``AGENT_NOTES_EMBED_TOKEN`` as a bearer token when
set. The server runs this same module's in-process path, so the
vectors are identical. If the service is unreachable the call falls back to
in-process with a warning (slower, same result); if it serves a different
model or dimension the call fails rather than mixing vector spaces.

Call order: always embed() before opening a DB transaction (decision 26).
The embed() function validates output dimension against AGENT_NOTES_EMBED_DIM
on every call to protect the pgvector column from silent drift.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import threading
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import numpy as np

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

_MODEL_NAME = os.environ.get("AGENT_NOTES_EMBED_MODEL", "nomic-ai/nomic-embed-text-v1.5")
_EMBED_DIM = int(os.environ.get("AGENT_NOTES_EMBED_DIM", "768"))
_TRUST_REMOTE_CODE = os.environ.get("AGENT_NOTES_TRUST_REMOTE_CODE", "false").lower() in (
    "1",
    "true",
    "yes",
)

_URL_ENV = "AGENT_NOTES_EMBED_URL"
# Bearer token for a service that requires one (the shared k8s deployment).
_TOKEN_ENV = "AGENT_NOTES_EMBED_TOKEN"
# A host that is down never answers the SYN, so connect gets a short timeout of
# its own; a long document can take a few seconds to encode on CPU.
_CONNECT_TIMEOUT = 2.0
_READ_TIMEOUT = 60.0

# Suppress HF Hub warnings and progress bars at module load so they're
# effective before sentence_transformers is imported on the first call.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

# Suppress sentence-transformers logging during model load.
logging.getLogger("sentence_transformers").setLevel(logging.WARNING)
logging.getLogger("huggingface").setLevel(logging.WARNING)

_log = logging.getLogger(__name__)

_model: SentenceTransformer | None = None
_lock = threading.Lock()
# Set after the configured service fails once, so a process that falls back
# does not wait on the dead service again for every later embed() call.
_service_down = False


class EmbedServiceError(RuntimeError):
    """The embedding service answered, but its vectors can't be used here."""


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                from sentence_transformers import SentenceTransformer

                _model = SentenceTransformer(_MODEL_NAME, trust_remote_code=_TRUST_REMOTE_CODE)
    return _model


def _setting(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        from agent_notes.core.suite_env import load_suite_env

        value = load_suite_env().get(name)
    value = (value or "").strip()
    return value or None


def service_url() -> str | None:
    """Return the configured embedding service URL (env > suite.env), or None."""
    return _setting(_URL_ENV)


def _check_dim(vec: np.ndarray) -> np.ndarray:
    if vec.shape[0] != _EMBED_DIM:
        raise RuntimeError(
            f"Embedding dimension mismatch: model returned {vec.shape[0]}, "
            f"expected {_EMBED_DIM} (AGENT_NOTES_EMBED_DIM). "
            f"Recreate the pgvector column or set AGENT_NOTES_EMBED_DIM={vec.shape[0]}."
        )
    return vec


def embed_local(text: str, task: str = "document") -> np.ndarray:
    """Encode in this process, loading the model on first use."""
    prefix = "search_query" if task == "query" else "search_document"
    model = _get_model()
    vec: np.ndarray = model.encode(f"{prefix}: {text}", normalize_embeddings=True)
    return _check_dim(vec)


def request_service(url: str, path: str, body: dict | None = None) -> dict:
    """Send one request to the embedding service and return its JSON reply.

    Raises ``OSError`` (including timeouts) or ``http.client.HTTPException``
    when the service can't be reached, and :class:`EmbedServiceError` when it
    answers with an error.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise EmbedServiceError(f"{_URL_ENV} must be an http:// or https:// URL")
    if parts.scheme == "https":
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(
            parts.hostname, parts.port or 443, timeout=_CONNECT_TIMEOUT
        )
    else:
        conn = http.client.HTTPConnection(
            parts.hostname, parts.port or 80, timeout=_CONNECT_TIMEOUT
        )
    headers = {}
    token = _setting(_TOKEN_ENV)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        conn.connect()
        conn.sock.settimeout(_READ_TIMEOUT)
        base = parts.path.rstrip("/")
        if body is None:
            conn.request("GET", base + path, headers=headers)
        else:
            headers["Content-Type"] = "application/json"
            conn.request(
                "POST", base + path, body=json.dumps(body).encode("utf-8"), headers=headers
            )
        resp = conn.getresponse()
        payload = resp.read()
    finally:
        conn.close()
    if resp.status in (502, 503, 504):
        # An ingress in front of the service answers for it while no pod is ready.
        raise ConnectionError(f"embedding service unavailable (HTTP {resp.status})")
    try:
        data = json.loads(payload)
    except ValueError as exc:
        raise EmbedServiceError(f"embedding service sent HTTP {resp.status} with no JSON") from exc
    if resp.status != 200:
        raise EmbedServiceError(
            f"embedding service error (HTTP {resp.status}): {data.get('error', 'unknown')}"
        )
    return data


def _embed_remote(url: str, text: str, task: str) -> np.ndarray:
    data = request_service(url, "/embed", {"texts": [text], "task": task})
    if data.get("model") != _MODEL_NAME:
        raise EmbedServiceError(
            f"embedding service at {url} serves {data.get('model')!r}, but this process "
            f"expects {_MODEL_NAME!r} (AGENT_NOTES_EMBED_MODEL). Vectors from different "
            "models can't share the pgvector column; point both at the same model."
        )
    return _check_dim(np.asarray(data["vectors"][0], dtype=np.float32))


def embed(text: str, task: str = "document") -> np.ndarray:
    """Return a normalized embedding vector.

    task='document' prefixes with 'search_document:' (for stored notes).
    task='query'    prefixes with 'search_query:' (for search queries).

    Uses the embedding service when ``AGENT_NOTES_EMBED_URL`` is set and
    reachable, else encodes in-process. Validates the returned dimension
    against AGENT_NOTES_EMBED_DIM on every encode to protect the pgvector
    column from silent dimension drift.
    """
    global _service_down
    url = None if _service_down else service_url()
    if url:
        try:
            return _embed_remote(url, text, task)
        except (OSError, http.client.HTTPException) as exc:
            _service_down = True
            _log.warning(
                "embedding service at %s unreachable (%s); loading the model in-process",
                url,
                type(exc).__name__,
            )
    return embed_local(text, task)
