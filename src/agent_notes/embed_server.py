"""agent-notes-embed-server: keep the embedding model warm for CLI calls.

Each ``agent-notes`` call is a fresh process, and loading torch and
sentence-transformers costs ~9s while the encode itself costs ~15ms. This
server loads the model once and answers ``core.embed`` over HTTP. Point a
machine's CLI at it with ``AGENT_NOTES_EMBED_URL=http://127.0.0.1:8766`` (in
suite.env or the environment).

Endpoints:

- ``GET /health`` -> ``{"model", "dim", "device"}``
- ``POST /embed`` with ``{"texts": [...], "task": "query"|"document"}``
  -> ``{"model", "dim", "vectors": [[...], ...]}``

Vectors come from ``core.embed.embed_local``, so they are the same vectors an
in-process call would store. It binds 127.0.0.1 by default. When
``AGENT_NOTES_EMBED_TOKEN`` is set in the server's environment, ``/embed``
requires it as a bearer token (``/health`` stays open for probes). The server
speaks plain HTTP: anything beyond loopback needs TLS in front of it, such as
the k8s ingress in ``deploy/k8s/embed``.
"""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
# The largest memory body is ~95k characters; leave plenty of room for a batch.
MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_TEXTS = 64

_log = logging.getLogger("agent_notes.embed_server")
# One encode at a time: each takes milliseconds, and torch on CPU already
# uses every core for a single encode.
_encode_lock = threading.Lock()


class _Handler(BaseHTTPRequestHandler):
    server_version = "agent-notes-embed"

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        from agent_notes.core import embed

        if self.path != "/health":
            self._send(404, {"error": "not found"})
            return
        self._send(
            200,
            {
                "model": embed._MODEL_NAME,
                "dim": embed._EMBED_DIM,
                "device": str(embed._get_model().device),
            },
        )

    def do_POST(self) -> None:  # noqa: N802 (http.server naming)
        from agent_notes.core import embed

        if self.path != "/embed":
            self._send(404, {"error": "not found"})
            return
        token = self.server.token
        if token:
            sent = self.headers.get("Authorization", "")
            if not hmac.compare_digest(sent.encode(), f"Bearer {token}".encode()):
                self._send(401, {"error": "missing or wrong bearer token"})
                return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            self._send(413, {"error": f"body must be 0..{MAX_BODY_BYTES} bytes"})
            return
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._send(400, {"error": "body is not JSON"})
            return
        texts = req.get("texts") if isinstance(req, dict) else None
        task = req.get("task", "document") if isinstance(req, dict) else None
        if (
            not isinstance(texts, list)
            or not 0 < len(texts) <= MAX_TEXTS
            or not all(isinstance(t, str) for t in texts)
        ):
            self._send(400, {"error": f"texts must be a list of 1..{MAX_TEXTS} strings"})
            return
        if task not in ("query", "document"):
            self._send(400, {"error": "task must be 'query' or 'document'"})
            return
        try:
            with _encode_lock:
                vectors = [embed.embed_local(t, task=task).tolist() for t in texts]
        except Exception as exc:
            _log.exception("encode failed")
            self._send(500, {"error": f"encode failed: {type(exc).__name__}"})
            return
        self._send(200, {"model": embed._MODEL_NAME, "dim": embed._EMBED_DIM, "vectors": vectors})

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        # Request lines would be noise in the service log; errors go via _log.
        pass


def build_server(host: str, port: int, token: str | None = None) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), _Handler)
    server.daemon_threads = True
    server.token = token or None
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent-notes-embed-server",
        description="Serve agent-notes embeddings from a warm model.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("AGENT_NOTES_EMBED_HOST", DEFAULT_HOST),
        help=f"address to bind (default {DEFAULT_HOST}; anything else exposes it unauthenticated)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("AGENT_NOTES_EMBED_PORT", DEFAULT_PORT)),
        help=f"port to bind (default {DEFAULT_PORT})",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # The model load asks the HF Hub for file lists; one INFO line per request.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    from agent_notes.core import embed

    token = os.environ.get("AGENT_NOTES_EMBED_TOKEN", "").strip()
    try:
        server = build_server(args.host, args.port, token)
    except OSError as exc:
        _log.error("can't bind %s:%s: %s", args.host, args.port, exc)
        return 1
    embed.embed_local("warm-up", task="query")
    _log.info(
        "serving %s (dim %s, %s) on http://%s:%s, %s",
        embed._MODEL_NAME,
        embed._EMBED_DIM,
        embed._get_model().device,
        args.host,
        args.port,
        "bearer token required" if token else "no token",
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
