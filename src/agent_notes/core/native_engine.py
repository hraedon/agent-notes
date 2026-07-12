"""Native memory engine — wraps the existing pgvector-based memory search
in the MemoryEngine protocol (Plan 020 WI-1.1).

Behavior-preserving adapter: ``memory_model`` and ``embed`` are unchanged.
The adapter exposes them through the provider-neutral ``MemoryEngine``
protocol so the CLI and skills can target any engine implementation.
"""

from __future__ import annotations

from typing import cast

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

_CAPABILITIES = frozenset({
    EngineCapability.INGEST,
    EngineCapability.RECALL,
    EngineCapability.FORGET,
    EngineCapability.EXACT_SOURCE,
})

_PROTOCOL_VERSION = "1.0"

_BUDGET_LIMITS: dict[str, int] = {
    "low": 3,
    "mid": 10,
    "high": 20,
}


def _package_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("agent-notes")
    except PackageNotFoundError:
        return "0.0.0"


def _resolve_scope(scope: MemoryScope) -> tuple[int, int]:
    """Resolve workspace_id and project_id from MemoryScope slugs (read-only).

    Raises ValueError if the workspace or project does not exist.
    """
    from agent_notes.core.db import list_projects, list_workspaces

    ws = next((w for w in list_workspaces() if w.slug == scope.workspace_slug), None)
    if ws is None:
        raise ValueError(f"Workspace '{scope.workspace_slug}' not found")

    proj = next(
        (p for p in list_projects(workspace_id=ws.id) if p.slug == scope.project_slug),
        None,
    )
    if proj is None:
        raise ValueError(
            f"Project '{scope.project_slug}' not found in workspace "
            f"'{scope.workspace_slug}'"
        )
    return ws.id, proj.id


def _embed_to_list(text: str, task: str) -> list[float]:
    from agent_notes.core.embed import embed

    vec = embed(text, task=task)
    if hasattr(vec, "tolist"):
        return cast(list[float], vec.tolist())
    return cast(list[float], list(vec))


class NativeMemoryEngine:
    """MemoryEngine backed by the local pgvector database.

    Wraps the existing ``memory_model`` CRUD/search and ``embed`` functions
    without modifying them. Synchronous: ``ingest`` returns INDEXED
    immediately and ``operation_status`` always returns INDEXED.
    """

    @property
    def engine_name(self) -> str:
        return "native"

    def describe(self) -> EngineHealth:
        try:
            from agent_notes.core.db import _conn

            with _conn() as conn:  # type: ignore[no-untyped-call]
                conn.execute("SELECT 1")
            state = EngineHealthState.HEALTHY
            detail = "native pgvector engine — operational"
        except Exception:
            state = EngineHealthState.UNREACHABLE
            detail = "native engine unreachable: database connection failed"

        return EngineHealth(
            state=state,
            capabilities=_CAPABILITIES,
            version=_package_version(),
            protocol_version=_PROTOCOL_VERSION,
            engine_name="native",
            indexing_backlog=0,
            indexing_freshness=None,
            detail=detail,
        )

    def ingest(self, batch: IngestBatch) -> IngestResult:
        from agent_notes.core import memory_model

        try:
            workspace_id, project_id = _resolve_scope(batch.scope)
        except ValueError as exc:
            return IngestResult(
                operation_id=None,
                state=OperationState.FAILED,
                items_count=0,
                error=str(exc),
            )

        count = 0
        for item in batch.items:
            embedding: list[float] | None = None
            try:
                embedding = _embed_to_list(item.content, task="document")
            except Exception:
                pass

            try:
                memory_model.add_memory(
                    workspace_id=workspace_id,
                    project_id=project_id,
                    name=item.source_id,
                    memory_type=item.memory_type,
                    body=item.content,
                    attributes=item.metadata or None,
                    embedding=embedding,
                )
                count += 1
            except Exception as exc:
                return IngestResult(
                    operation_id=None,
                    state=OperationState.FAILED,
                    items_count=count,
                    error=f"Failed at item '{item.source_id}': {exc}",
                )

        return IngestResult(
            operation_id=None,
            state=OperationState.INDEXED,
            items_count=count,
        )

    def operation_status(self, operation_id: str) -> OperationStatus:
        return OperationStatus(
            operation_id=operation_id,
            state=OperationState.INDEXED,
        )

    def recall(self, query: RecallQuery) -> RecallResponse:
        try:
            workspace_id, project_id = _resolve_scope(query.scope)
        except ValueError as exc:
            return RecallResponse(
                results=[],
                engine="native",
                usage={"error": str(exc)},
            )

        try:
            query_vec = _embed_to_list(query.query, task="query")
        except Exception as exc:
            return RecallResponse(
                results=[],
                engine="native",
                usage={"error": f"embedding failed: {exc}"},
            )

        from agent_notes.core.memory_model import search_memory_with_body

        limit = _BUDGET_LIMITS.get(query.budget, 10)
        memory_type: str | None = None
        if query.memory_types and len(query.memory_types) == 1:
            memory_type = query.memory_types[0]

        try:
            rows = search_memory_with_body(
                workspace_id=workspace_id,
                query_vec=query_vec,
                project_id=project_id,
                memory_type=memory_type,
                limit=limit,
            )
        except Exception:
            return RecallResponse(
                results=[],
                engine="native",
                usage={"error": "database query failed"},
            )

        results = [
            RecallResult(
                text=row.get("body", ""),
                origin=OriginClass.RAW,
                score=float(row.get("score", 0.0)) if row.get("score") is not None else 0.0,
                source_ref=row.get("name"),
                memory_type=row.get("memory_type"),
            )
            for row in rows
        ]

        return RecallResponse(
            results=results,
            engine="native",
            usage={"count": len(results)},
        )

    def forget(self, selector: ForgetSelector) -> ForgetResult:
        if selector.source_id is not None:
            if selector.scope is None:
                return ForgetResult(
                    deleted_count=0,
                    error="scope required to resolve workspace/project for source_id deletion",
                )
            try:
                workspace_id, project_id = _resolve_scope(selector.scope)
            except ValueError as exc:
                return ForgetResult(
                    deleted_count=0,
                    error=str(exc),
                )

            from agent_notes.core.memory_model import delete_memory

            try:
                result = delete_memory(workspace_id, project_id, selector.source_id)
            except Exception:
                return ForgetResult(
                    deleted_count=0,
                    error="database delete failed",
                )
            if result is None:
                return ForgetResult(
                    deleted_count=0,
                    error=f"memory '{selector.source_id}' not found",
                )
            return ForgetResult(
                deleted_count=1,
                cascade_count=0,
            )

        if selector.scope is not None:
            return ForgetResult(
                deleted_count=0,
                error="native engine does not support scope-wide deletion",
            )

        return ForgetResult(
            deleted_count=0,
            error="no selector provided (source_id or scope required)",
        )
