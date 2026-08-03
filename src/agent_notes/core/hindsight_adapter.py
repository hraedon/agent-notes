"""Hindsight memory engine adapter (Plan 020 WI-2.1).

Connects agent-notes to a self-hosted Hindsight instance via its HTTP API.
Implements the ``MemoryEngine`` protocol using stdlib ``urllib`` only — no
external HTTP library in the core, matching the family convention.

Security: redirects are NOT followed (SSRF guard), URL scheme must be
http/https, and every engine method returns error results rather than raising.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from agent_notes.core.memory_engine import (
    EngineCapability,
    EngineHealth,
    EngineHealthState,
    ForgetResult,
    ForgetSelector,
    IngestBatch,
    IngestResult,
    MemoryScope,
    OperationState,
    OperationStatus,
    OriginClass,
    RecallQuery,
    RecallResponse,
    RecallResult,
)

_CAPABILITIES = frozenset(
    {
        EngineCapability.INGEST,
        EngineCapability.RECALL,
        EngineCapability.FORGET,
        EngineCapability.EXACT_SOURCE,
    }
)

_PROTOCOL_VERSION = "1.0"
_DEFAULT_TENANT = "default"
_DEFAULT_TIMEOUT = 30

_BANK_ID_RE = re.compile(r"^[a-z0-9_]+$")


def _bank_id(project_slug: str) -> str:
    sanitized = project_slug.lower().replace("-", "_")
    if not _BANK_ID_RE.match(sanitized):
        sanitized = re.sub(r"[^a-z0-9_]", "_", sanitized)
    return sanitized


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def _build_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_NoRedirectHandler)


def _http_request(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    api_key: str | None = None,
    timeout: int = _DEFAULT_TIMEOUT,
) -> tuple[int | None, dict[str, Any] | str | None, str | None]:
    """Execute an HTTP request with SSRF guards. Returns (status, parsed_body, error)."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None, None, f"refusing non-http(s) URL scheme '{parsed.scheme}'"

    data: bytes | None = None
    headers: dict[str, str] = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    opener = _build_opener()
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            status = resp.status
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308):
            return exc.code, None, f"redirect refused from {url} (SSRF guard)"
        try:
            raw = exc.read().decode("utf-8")
        except Exception:
            raw = ""
        return exc.code, _safe_json(raw), f"HTTP {exc.code} from {url}"
    except urllib.error.URLError as exc:
        return None, None, f"unreachable: {url} ({exc.reason})"
    except Exception as exc:
        return None, None, f"error requesting {url}: {exc}"

    return status, _safe_json(raw), None


def _safe_json(raw: str) -> dict[str, Any] | str | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw
    return parsed if isinstance(parsed, dict) else raw


def _build_tags(scope: MemoryScope) -> list[str]:
    tags = [
        f"project:{scope.project_slug}",
        f"workspace:{scope.workspace_slug}",
    ]
    if scope.user_id:
        tags.append(f"user:{scope.user_id}")
    if scope.agent_id:
        tags.append(f"agent:{scope.agent_id}")
    return tags


def _map_recall_origin(hindsight_type: str | None) -> OriginClass:
    match hindsight_type:
        case "world" | "experience":
            return OriginClass.EXTRACTED
        case "observation":
            return OriginClass.DERIVED
        case "mental_model" | "mental-model":
            return OriginClass.DERIVED
        case None | "":
            return OriginClass.EXTRACTED
        case _:
            return OriginClass.EXTRACTED


def _safe_int(val: Any, default: int = 0) -> int:
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _safe_str(val: Any) -> str | None:
    if val is None:
        return None
    return str(val) if isinstance(val, (str, int, float)) else None


def _config_from_file() -> dict[str, Any]:
    try:
        from agent_notes.core.config import config_path

        path = config_path()
        if not path.is_file():
            return {}
        data = json.loads(path.read_text())
        block = data.get("hindsight")
        return block if isinstance(block, dict) else {}
    except Exception:
        return {}


class HindsightEngine:
    """MemoryEngine backed by a self-hosted Hindsight HTTP API instance.

    Async by default: ``ingest`` returns PENDING with an operation_id;
    the caller polls ``operation_status`` until INDEXED/FAILED/CANCELLED.
    """

    def __init__(
        self,
        url: str | None,
        tenant: str = _DEFAULT_TENANT,
        api_key: str | None = None,
        timeout: int = _DEFAULT_TIMEOUT,
    ) -> None:
        if url:
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme not in ("http", "https"):
                raise ValueError(f"Invalid URL scheme '{parsed.scheme}'; must be http or https")
            self._url = url.rstrip("/")
        else:
            self._url = None
        self._tenant = tenant
        self._api_key = api_key
        self._timeout = timeout

    @classmethod
    def from_config(cls) -> HindsightEngine:
        import os

        file_cfg = _config_from_file()

        try:
            from agent_notes.core.suite_env import load_suite_env

            suite = load_suite_env()
        except Exception:
            suite = {}

        def _env_or_suite(env_key: str) -> str | None:
            val = os.environ.get(env_key)
            if val:
                return val
            val = suite.get(env_key)
            return val if isinstance(val, str) and val else None

        url = _env_or_suite("HINDSIGHT_URL") or (
            file_cfg.get("url") if isinstance(file_cfg.get("url"), str) else None
        )
        tenant = (
            _env_or_suite("HINDSIGHT_TENANT")
            or (file_cfg.get("tenant") if isinstance(file_cfg.get("tenant"), str) else None)
            or _DEFAULT_TENANT
        )
        api_key = _env_or_suite("HINDSIGHT_API_KEY") or (
            file_cfg.get("api_key") if isinstance(file_cfg.get("api_key"), str) else None
        )
        timeout_raw = _env_or_suite("HINDSIGHT_TIMEOUT") or (
            file_cfg.get("timeout") if isinstance(file_cfg.get("timeout"), (int, float)) else None
        )
        try:
            timeout = int(timeout_raw) if timeout_raw is not None else _DEFAULT_TIMEOUT
        except (ValueError, TypeError):
            timeout = _DEFAULT_TIMEOUT

        return cls(url=url, tenant=tenant, api_key=api_key, timeout=timeout)

    @property
    def engine_name(self) -> str:
        return "hindsight"

    def _api_base(self) -> str:
        return f"{self._url}/v1/{self._tenant}"

    def describe(self) -> EngineHealth:
        if not self._url:
            return EngineHealth(
                state=EngineHealthState.NOT_CONFIGURED,
                capabilities=frozenset(),
                engine_name="hindsight",
                detail="hindsight engine not configured (set HINDSIGHT_URL)",
            )

        _, body, error = _http_request(
            f"{self._url}/health",
            method="GET",
            api_key=self._api_key,
            timeout=self._timeout,
        )
        if error is not None:
            return EngineHealth(
                state=EngineHealthState.UNREACHABLE,
                capabilities=_CAPABILITIES,
                engine_name="hindsight",
                detail=f"hindsight unreachable at {self._url}: {error}",
            )

        _, v_body, v_error = _http_request(
            f"{self._url}/version",
            method="GET",
            api_key=self._api_key,
            timeout=self._timeout,
        )
        if v_error is not None:
            return EngineHealth(
                state=EngineHealthState.UNREACHABLE,
                capabilities=_CAPABILITIES,
                engine_name="hindsight",
                detail=f"hindsight version check failed at {self._url}: {v_error}",
            )

        version: str | None = None
        if isinstance(v_body, dict):
            version = str(v_body.get("api_version") or v_body.get("version") or "unknown")

        indexing_backlog: int | None = None
        indexing_freshness: str | None = None

        return EngineHealth(
            state=EngineHealthState.HEALTHY,
            capabilities=_CAPABILITIES,
            version=version,
            protocol_version=_PROTOCOL_VERSION,
            engine_name="hindsight",
            indexing_backlog=indexing_backlog,
            indexing_freshness=indexing_freshness,
            detail=f"hindsight v{version} at {self._url}",
        )

    def ingest(self, batch: IngestBatch) -> IngestResult:
        if not self._url:
            return IngestResult(
                operation_id=None,
                state=OperationState.FAILED,
                items_count=0,
                error="hindsight engine not configured (set HINDSIGHT_URL)",
            )

        bank_id = _bank_id(batch.scope.project_slug)

        _, _, put_error = _http_request(
            f"{self._api_base()}/banks/{bank_id}",
            method="PUT",
            body={},
            api_key=self._api_key,
            timeout=self._timeout,
        )
        if put_error is not None:
            return IngestResult(
                operation_id=None,
                state=OperationState.FAILED,
                items_count=0,
                error=f"failed to ensure bank '{bank_id}': {put_error}",
            )

        tags = _build_tags(batch.scope)
        items: list[dict[str, Any]] = []
        for item in batch.items:
            metadata = dict(item.metadata)
            entry: dict[str, Any] = {
                "content": item.content,
                "document_id": item.source_id,
                "tags": list(tags),
                "metadata": metadata,
            }
            if item.timestamp:
                metadata["timestamp"] = item.timestamp
            if item.idempotency_key:
                metadata["idempotency_key"] = item.idempotency_key
            items.append(entry)

        post_body = {"items": items, "async": True}

        _, resp, error = _http_request(
            f"{self._api_base()}/banks/{bank_id}/memories",
            method="POST",
            body=post_body,
            api_key=self._api_key,
            timeout=self._timeout,
        )
        if error is not None:
            return IngestResult(
                operation_id=None,
                state=OperationState.FAILED,
                items_count=0,
                error=f"ingest failed: {error}",
            )

        if isinstance(resp, dict):
            operation_id = resp.get("operation_id")
            is_async = resp.get("async", False)
            count = _safe_int(resp.get("items_count", len(items)))
            state = OperationState.PENDING if is_async else OperationState.INDEXED
            return IngestResult(
                operation_id=operation_id,
                state=state,
                items_count=count,
            )

        return IngestResult(
            operation_id=None,
            state=OperationState.PENDING,
            items_count=len(items),
        )

    def operation_status(self, operation_id: str) -> OperationStatus:
        if not self._url:
            return OperationStatus(
                operation_id=operation_id,
                state=OperationState.FAILED,
                error="hindsight engine not configured",
            )

        status, resp, error = _http_request(
            f"{self._api_base()}/banks/_/operations/{urllib.parse.quote(operation_id, safe='')}",
            method="GET",
            api_key=self._api_key,
            timeout=self._timeout,
        )
        if error is not None:
            return OperationStatus(
                operation_id=operation_id,
                state=OperationState.FAILED,
                error=error,
            )

        if isinstance(resp, dict):
            raw_status = resp.get("status", "pending")
            match raw_status:
                case "pending" | "processing":
                    state = OperationState.PENDING
                case "completed":
                    state = OperationState.INDEXED
                case "failed":
                    state = OperationState.FAILED
                case "cancelled":
                    state = OperationState.CANCELLED
                case "stale":
                    state = OperationState.STALE
                case _:
                    state = OperationState.FAILED
            return OperationStatus(
                operation_id=operation_id,
                state=state,
                created_at=resp.get("created_at"),
                updated_at=resp.get("updated_at"),
                completed_at=resp.get("completed_at"),
                error=resp.get("error"),
            )

        return OperationStatus(
            operation_id=operation_id,
            state=OperationState.FAILED,
            error="unexpected non-JSON response from operation status",
        )

    def recall(self, query: RecallQuery) -> RecallResponse:
        if not self._url:
            return RecallResponse(
                results=[],
                engine="hindsight",
                usage={"error": "hindsight engine not configured (set HINDSIGHT_URL)"},
            )

        bank_id = _bank_id(query.scope.project_slug)
        tags = _build_tags(query.scope)
        if query.memory_types:
            tags.extend(query.memory_types)
        body: dict[str, Any] = {
            "query": query.query,
            "budget": query.budget,
            "max_tokens": query.max_tokens,
            "tags": tags,
        }

        _, resp, error = _http_request(
            f"{self._api_base()}/banks/{bank_id}/memories/recall",
            method="POST",
            body=body,
            api_key=self._api_key,
            timeout=self._timeout,
        )
        if error is not None:
            return RecallResponse(
                results=[],
                engine="hindsight",
                usage={"error": error},
            )

        results: list[RecallResult] = []
        if isinstance(resp, dict):
            raw_results = resp.get("results", [])
            if isinstance(raw_results, list):
                for item in raw_results:
                    if not isinstance(item, dict):
                        continue
                    text = str(item.get("text", ""))
                    hindsight_type = item.get("type")
                    origin = _map_recall_origin(
                        hindsight_type if isinstance(hindsight_type, str) else None
                    )
                    doc_id = item.get("document_id")
                    if isinstance(doc_id, str) and doc_id:
                        origin = OriginClass.RAW
                    chunk_id = item.get("chunk_id")
                    source_ref = doc_id if isinstance(doc_id, str) else _safe_str(chunk_id)
                    results.append(
                        RecallResult(
                            text=text,
                            origin=origin,
                            score=_safe_float(item.get("score", 0.0)),
                            source_ref=source_ref,
                            memory_type=hindsight_type if isinstance(hindsight_type, str) else None,
                            metadata={
                                k: v
                                for k, v in item.items()
                                if k not in ("text", "type", "score", "document_id", "chunk_id")
                            },
                        )
                    )

        return RecallResponse(
            results=results,
            engine="hindsight",
            usage={"count": len(results)},
        )

    def forget(self, selector: ForgetSelector) -> ForgetResult:
        if not self._url:
            return ForgetResult(
                deleted_count=0,
                error="hindsight engine not configured (set HINDSIGHT_URL)",
            )

        if selector.source_id is not None:
            if selector.scope is None:
                return ForgetResult(
                    deleted_count=0,
                    error="scope required for source_id deletion",
                )
            bank_id = _bank_id(selector.scope.project_slug)
            encoded_id = urllib.parse.quote(selector.source_id, safe="")
            _, resp, error = _http_request(
                f"{self._api_base()}/banks/{bank_id}/documents/{encoded_id}",
                method="DELETE",
                api_key=self._api_key,
                timeout=self._timeout,
            )
            if error is not None:
                return ForgetResult(
                    deleted_count=0,
                    error=f"forget failed: {error}",
                )
            cascade = _safe_int(resp.get("cascade_count", 0)) if isinstance(resp, dict) else 0
            receipt = f"deleted:{selector.source_id}"
            return ForgetResult(
                deleted_count=1,
                cascade_count=cascade,
                receipt=receipt,
            )

        if selector.scope is not None:
            bank_id = _bank_id(selector.scope.project_slug)
            _, resp, error = _http_request(
                f"{self._api_base()}/banks/{bank_id}",
                method="DELETE",
                api_key=self._api_key,
                timeout=self._timeout,
            )
            if error is not None:
                return ForgetResult(
                    deleted_count=0,
                    error=f"forget (bank delete) failed: {error}",
                )
            count = _safe_int(resp.get("deleted_count", 0)) if isinstance(resp, dict) else 0
            return ForgetResult(
                deleted_count=count,
                receipt=f"bank_deleted:{bank_id}",
            )

        return ForgetResult(
            deleted_count=0,
            error="no selector provided (source_id or scope required)",
        )
