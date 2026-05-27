"""In-process lazy singleton embedding (decision 2 / decision 22).

Thread-safe via double-checked locking. First call loads the ~270MB model
and keeps it resident for the process lifetime. Reads AGENT_NOTES_EMBED_MODEL
and AGENT_NOTES_EMBED_DIM env vars so operators can swap models (MiniLM-384,
BGE-large-1024) without code changes.

Call order: always embed() before opening a DB transaction (decision 26).
Dim-vs-schema validation is deferred to Phase 2a when kind tables with vector
columns exist; see the TODO comment in embed().
"""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING

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

_model: SentenceTransformer | None = None
_lock = threading.Lock()


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                from sentence_transformers import SentenceTransformer

                _model = SentenceTransformer(_MODEL_NAME, trust_remote_code=_TRUST_REMOTE_CODE)
    return _model


def embed(text: str, task: str = "document") -> np.ndarray:
    """Return a normalized embedding vector.

    task='document' prefixes with 'search_document:' (for stored notes).
    task='query'    prefixes with 'search_query:' (for search queries).

    Validates the returned dimension against AGENT_NOTES_EMBED_DIM on every
    encode to protect the pgvector column from silent dimension drift.
    """
    prefix = "search_query" if task == "query" else "search_document"
    model = _get_model()
    vec: np.ndarray = model.encode(f"{prefix}: {text}", normalize_embeddings=True)
    if vec.shape[0] != _EMBED_DIM:
        raise RuntimeError(
            f"Embedding dimension mismatch: model returned {vec.shape[0]}, "
            f"expected {_EMBED_DIM} (AGENT_NOTES_EMBED_DIM). "
            f"Recreate the pgvector column or set AGENT_NOTES_EMBED_DIM={vec.shape[0]}."
        )
    return vec
