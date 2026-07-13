"""Public, read-only knowledge-provider contract for suite consumers.

This module is the supported boundary for dossier and other human surfaces.
Consumers must not query agent-notes tables or import private model helpers.
Exact note text is kept distinct from the optional vector search projection:
lexical search never loads an embedding model; semantic search does so lazily.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Literal, Mapping, Protocol, Sequence, cast, runtime_checkable
from uuid import UUID

SCHEMA_VERSION = "agent-notes.knowledge.v1"
PROVIDER_NAME = "agent-notes"


class KnowledgeState(Enum):
    """Closed provider/result state vocabulary shared with human surfaces."""

    CURRENT = "current"
    STALE = "stale"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class SearchMode(Enum):
    """Supported search implementations.

    ``LEXICAL`` is deterministic PostgreSQL substring matching over exact note
    text. ``SEMANTIC`` uses the optional/native vector index and may initialize
    the configured embedding model.
    """

    LEXICAL = "lexical"
    SEMANTIC = "semantic"


@dataclass(frozen=True)
class KnowledgeScope:
    workspace: str
    project: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {"workspace": self.workspace, "project": self.project}


@dataclass(frozen=True)
class ProviderStatus:
    state: KnowledgeState
    observed_at: str
    source: str = PROVIDER_NAME
    detail: str = ""
    findings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "observed_at": self.observed_at,
            "source": self.source,
            "detail": self.detail,
            "findings": list(self.findings),
        }


@dataclass(frozen=True)
class KnowledgeItem:
    """One exact knowledge entity or a browse/search summary of it."""

    name: str
    workspace: str
    project: str
    memory_type: str
    note_subtype: str
    body: str | None
    body_preview: str
    attributes: Mapping[str, Any]
    entity_ref: str | None
    authority: str
    authority_state: KnowledgeState
    created_at: str | None
    updated_at: str | None
    score: float | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "workspace": self.workspace,
            "project": self.project,
            "memory_type": self.memory_type,
            "note_subtype": self.note_subtype,
            "body": self.body,
            "body_preview": self.body_preview,
            "attributes": _json_safe(dict(self.attributes)),
            "entity_ref": self.entity_ref,
            "authority": self.authority,
            "authority_state": self.authority_state.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "score": self.score,
        }


@dataclass(frozen=True)
class KnowledgeLink:
    kind: str
    workspace: str | None
    project: str | None
    identifier: str
    relationship: str
    direction: str
    depth: int
    title: str | None = None
    item_state: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "workspace": self.workspace,
            "project": self.project,
            "identifier": self.identifier,
            "relationship": self.relationship,
            "direction": self.direction,
            "depth": self.depth,
            "title": self.title,
            "item_state": self.item_state,
        }


@dataclass(frozen=True)
class IndexPosture:
    exact_records: int
    vector_indexed: int
    vector_missing: int
    pending_sync: int
    signed_records: int
    native_records: int
    latest_update: str | None
    semantic_search: KnowledgeState

    def to_dict(self) -> dict[str, object]:
        return {
            "exact_records": self.exact_records,
            "vector_indexed": self.vector_indexed,
            "vector_missing": self.vector_missing,
            "pending_sync": self.pending_sync,
            "signed_records": self.signed_records,
            "native_records": self.native_records,
            "latest_update": self.latest_update,
            "semantic_search": self.semantic_search.value,
        }


@dataclass(frozen=True)
class KnowledgeResponse:
    status: ProviderStatus
    scope: KnowledgeScope
    items: tuple[KnowledgeItem, ...] = ()
    item: KnowledgeItem | None = None
    links: tuple[KnowledgeLink, ...] = ()
    index: IndexPosture | None = None
    search_mode: SearchMode | None = None
    next_cursor: str | None = None
    schema_version: str = field(default=SCHEMA_VERSION, init=False)

    def to_dict(self) -> dict[str, object]:
        """Return a versioned shape accepted by ``json.dumps`` without hooks."""
        return {
            "schema_version": self.schema_version,
            "status": self.status.to_dict(),
            "scope": self.scope.to_dict(),
            "items": [item.to_dict() for item in self.items],
            "item": self.item.to_dict() if self.item is not None else None,
            "links": [link.to_dict() for link in self.links],
            "index": self.index.to_dict() if self.index is not None else None,
            "search_mode": self.search_mode.value if self.search_mode is not None else None,
            "next_cursor": self.next_cursor,
        }


@runtime_checkable
class KnowledgeProvider(Protocol):
    """Stable read-only provider used by suite faces.

    This protocol does not authenticate callers or enforce project ACLs. The
    caller/transport must authorize the complete scope before every invocation
    and separately authorize any cross-scope link target it exposes.
    """

    def browse(
        self,
        scope: KnowledgeScope,
        *,
        memory_type: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> KnowledgeResponse: ...

    def detail(self, scope: KnowledgeScope, name: str) -> KnowledgeResponse: ...

    def search(
        self,
        scope: KnowledgeScope,
        query: str,
        *,
        mode: SearchMode = SearchMode.LEXICAL,
        memory_type: str | None = None,
        limit: int = 20,
    ) -> KnowledgeResponse: ...

    def links(
        self,
        scope: KnowledgeScope,
        name: str,
        *,
        direction: str = "dependencies",
        max_depth: int = 1,
    ) -> KnowledgeResponse: ...

    def index_health(self, scope: KnowledgeScope) -> KnowledgeResponse: ...


class AgentNotesKnowledgeProvider:
    """Read-only adapter over agent-notes' public model functions."""

    def browse(
        self,
        scope: KnowledgeScope,
        *,
        memory_type: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> KnowledgeResponse:
        resolved = self._resolve(scope)
        if isinstance(resolved, KnowledgeResponse):
            return resolved
        workspace_id, project_id, projects = resolved
        try:
            offset = _parse_cursor(cursor)
        except ValueError:
            return self._response(scope, KnowledgeState.UNKNOWN, "Invalid browse cursor")
        page_size = min(max(limit, 1), 100)
        try:
            from agent_notes.core.memory_model import browse_knowledge

            rows = browse_knowledge(
                workspace_id,
                project_id=project_id,
                memory_type=memory_type,
                limit=page_size + 1,
                offset=offset,
            )
            items, invalid = _items_from_rows(rows[:page_size], scope.workspace, projects)
        except Exception:
            return self._response(scope, KnowledgeState.UNAVAILABLE, "Knowledge browse unavailable")
        next_cursor = str(offset + page_size) if len(rows) > page_size else None
        state, findings = _aggregate_items(items, invalid)
        return KnowledgeResponse(
            status=_status(state, "Exact knowledge browse", findings),
            scope=scope,
            items=tuple(items),
            next_cursor=next_cursor,
        )

    def detail(self, scope: KnowledgeScope, name: str) -> KnowledgeResponse:
        resolved = self._resolve(scope, require_project=True)
        if isinstance(resolved, KnowledgeResponse):
            return resolved
        workspace_id, project_id, projects = resolved
        assert project_id is not None
        try:
            from agent_notes.core.memory_model import get_knowledge_memory

            row = get_knowledge_memory(workspace_id, project_id, name)
        except Exception:
            return self._response(scope, KnowledgeState.UNAVAILABLE, "Knowledge detail unavailable")
        if row is None:
            return self._response(scope, KnowledgeState.CURRENT, "Knowledge item not found")
        try:
            item = _item_from_row(row, scope.workspace, projects, include_body=True)
        except (KeyError, TypeError, ValueError):
            return self._response(scope, KnowledgeState.UNKNOWN, "Knowledge item shape is unknown")
        return KnowledgeResponse(
            status=_status(item.authority_state, "Exact knowledge detail"),
            scope=scope,
            item=item,
        )

    def search(
        self,
        scope: KnowledgeScope,
        query: str,
        *,
        mode: SearchMode = SearchMode.LEXICAL,
        memory_type: str | None = None,
        limit: int = 20,
    ) -> KnowledgeResponse:
        resolved = self._resolve(scope)
        if isinstance(resolved, KnowledgeResponse):
            return resolved
        workspace_id, project_id, projects = resolved
        if not query.strip():
            return self._response(scope, KnowledgeState.UNKNOWN, "Search query is empty", mode)
        result_limit = min(max(limit, 1), 100)
        try:
            if mode is SearchMode.LEXICAL:
                from agent_notes.core.memory_model import search_memories_exact

                rows = search_memories_exact(
                    workspace_id,
                    query,
                    project_id=project_id,
                    memory_type=memory_type,
                    limit=result_limit,
                )
            elif mode is SearchMode.SEMANTIC:
                # Deliberately imported only in this branch: browse, detail,
                # links, health, and lexical search never initialize the model.
                from agent_notes.core.embed import embed
                from agent_notes.core.memory_model import search_knowledge_semantic

                query_vec = embed(query, task="query").tolist()
                rows = search_knowledge_semantic(
                    workspace_id,
                    query_vec,
                    project_id=project_id,
                    memory_type=memory_type,
                    limit=result_limit,
                )
            else:
                return self._response(
                    scope, KnowledgeState.UNSUPPORTED, "Search mode is unsupported"
                )
            items, invalid = _items_from_rows(rows, scope.workspace, projects, include_body=False)
        except (ImportError, ModuleNotFoundError):
            return self._response(
                scope,
                KnowledgeState.UNSUPPORTED,
                "Semantic search dependencies are unavailable",
                mode,
            )
        except Exception:
            return self._response(
                scope, KnowledgeState.UNAVAILABLE, "Knowledge search unavailable", mode
            )
        state, findings = _aggregate_items(items, invalid)
        if mode is SearchMode.SEMANTIC:
            try:
                health = self.index_health(scope)
                if health.index is not None and health.index.vector_missing:
                    findings = (*findings, "Some exact records are absent from the vector index")
                    if state is KnowledgeState.CURRENT:
                        state = KnowledgeState.PARTIAL
            except Exception:
                findings = (*findings, "Vector-index coverage could not be established")
                state = KnowledgeState.PARTIAL
        return KnowledgeResponse(
            status=_status(state, "Knowledge search", findings),
            scope=scope,
            items=tuple(items),
            search_mode=mode,
        )

    def links(
        self,
        scope: KnowledgeScope,
        name: str,
        *,
        direction: str = "dependencies",
        max_depth: int = 1,
    ) -> KnowledgeResponse:
        resolved = self._resolve(scope, require_project=True)
        if isinstance(resolved, KnowledgeResponse):
            return resolved
        workspace_id, project_id, _projects = resolved
        assert project_id is not None
        if direction not in {"dependencies", "dependents"} or not 1 <= max_depth <= 10:
            return self._response(scope, KnowledgeState.UNKNOWN, "Invalid link traversal request")
        link_direction = cast(Literal["dependencies", "dependents"], direction)
        try:
            from agent_notes.core.search import trace_graph_all

            nodes = trace_graph_all(
                "memory",
                workspace_id,
                project_id,
                name,
                direction=link_direction,
                max_depth=max_depth,
            )
            workspace_names, project_names = _scope_name_maps()
        except Exception:
            return self._response(scope, KnowledgeState.UNAVAILABLE, "Knowledge links unavailable")
        links = tuple(
            KnowledgeLink(
                kind=node.kind,
                workspace=workspace_names.get(node.workspace_id),
                project=project_names.get(node.project_id),
                identifier=node.identifier,
                relationship=node.relationship,
                direction=direction,
                depth=node.depth,
                title=node.title,
                item_state=node.status,
            )
            for node in nodes
        )
        unresolved = sum(link.workspace is None or link.project is None for link in links)
        link_state = KnowledgeState.PARTIAL if unresolved else KnowledgeState.CURRENT
        findings = (
            (f"{unresolved} link target scope(s) could not be resolved",) if unresolved else ()
        )
        return KnowledgeResponse(
            status=_status(link_state, "Knowledge links", findings), scope=scope, links=links
        )

    def index_health(self, scope: KnowledgeScope) -> KnowledgeResponse:
        resolved = self._resolve(scope)
        if isinstance(resolved, KnowledgeResponse):
            return resolved
        workspace_id, project_id, _projects = resolved
        try:
            from agent_notes.core.memory_model import knowledge_index_health

            raw = knowledge_index_health(workspace_id, project_id)
            total = int(raw.get("total") or 0)
            indexed = int(raw.get("vector_indexed") or 0)
            pending = int(raw.get("pending_sync") or 0)
            signed = int(raw.get("signed") or 0)
            missing = max(total - indexed, 0)
            semantic_state = KnowledgeState.CURRENT if missing == 0 else KnowledgeState.PARTIAL
            state = semantic_state
            findings: list[str] = []
            if missing:
                findings.append(f"{missing} exact record(s) are not vector indexed")
            if pending:
                findings.append(f"{pending} record(s) await authority synchronization")
                state = (
                    KnowledgeState.STALE if pending == total and total else KnowledgeState.PARTIAL
                )
            posture = IndexPosture(
                exact_records=total,
                vector_indexed=indexed,
                vector_missing=missing,
                pending_sync=pending,
                signed_records=signed,
                native_records=max(total - signed, 0),
                latest_update=_iso(raw.get("latest_update")),
                semantic_search=semantic_state,
            )
        except Exception:
            return self._response(
                scope, KnowledgeState.UNAVAILABLE, "Knowledge index health unavailable"
            )
        return KnowledgeResponse(
            status=_status(state, "Knowledge index posture", tuple(findings)),
            scope=scope,
            index=posture,
        )

    def _resolve(
        self, scope: KnowledgeScope, *, require_project: bool = False
    ) -> tuple[int, int | None, Mapping[int, str]] | KnowledgeResponse:
        try:
            from agent_notes.core.db import list_projects, list_workspaces

            workspace = next((w for w in list_workspaces() if w.slug == scope.workspace), None)
            if workspace is None:
                return self._response(scope, KnowledgeState.UNAVAILABLE, "Workspace is unavailable")
            projects = list_projects(workspace_id=workspace.id)
            project_map = {project.id: project.slug for project in projects}
            if scope.project is None:
                if require_project:
                    return self._response(
                        scope, KnowledgeState.UNSUPPORTED, "This operation requires a project scope"
                    )
                return workspace.id, None, project_map
            project = next((p for p in projects if p.slug == scope.project), None)
            if project is None:
                return self._response(scope, KnowledgeState.UNAVAILABLE, "Project is unavailable")
            return workspace.id, project.id, project_map
        except Exception:
            return self._response(scope, KnowledgeState.UNAVAILABLE, "Knowledge scope unavailable")

    @staticmethod
    def _response(
        scope: KnowledgeScope,
        state: KnowledgeState,
        detail: str,
        mode: SearchMode | None = None,
    ) -> KnowledgeResponse:
        return KnowledgeResponse(status=_status(state, detail), scope=scope, search_mode=mode)


def get_knowledge_provider() -> KnowledgeProvider:
    """Return the supported agent-notes knowledge provider."""
    return AgentNotesKnowledgeProvider()


def _items_from_rows(
    rows: Sequence[Mapping[str, Any]],
    workspace: str,
    projects: Mapping[int, str],
    *,
    include_body: bool = False,
) -> tuple[list[KnowledgeItem], int]:
    items: list[KnowledgeItem] = []
    invalid = 0
    for row in rows:
        try:
            items.append(_item_from_row(row, workspace, projects, include_body=include_body))
        except (KeyError, TypeError, ValueError):
            invalid += 1
    return items, invalid


def _scope_name_maps() -> tuple[dict[int, str], dict[int, str]]:
    """Resolve component-local IDs before values cross the public boundary."""
    from agent_notes.core.db import list_projects, list_workspaces

    workspaces = {workspace.id: workspace.slug for workspace in list_workspaces()}
    projects = {project.id: project.slug for project in list_projects()}
    return workspaces, projects


def _item_from_row(
    row: Mapping[str, Any],
    workspace: str,
    projects: Mapping[int, str],
    *,
    include_body: bool,
) -> KnowledgeItem:
    name = str(row["name"])
    project_id = int(row["project_id"])
    project = projects[project_id]
    memory_type = str(row["memory_type"])
    body_value = row.get("body")
    body = str(body_value) if include_body and body_value is not None else None
    preview_value = row.get("body_preview")
    if preview_value is None and body_value is not None:
        preview_value = str(body_value)[:240]
    entity_ref_value = row.get("regista_note_id")
    entity_ref = str(entity_ref_value) if entity_ref_value is not None else None
    pending = bool(row.get("pending_sync", False))
    if pending:
        authority_state = KnowledgeState.STALE
    elif entity_ref is not None:
        # The local row identifies a signed regista entity, but this read did
        # not replay/verify that entity's event chain. Do not turn projection
        # presence into a proof claim.
        authority_state = KnowledgeState.UNKNOWN
    else:
        authority_state = KnowledgeState.CURRENT
    return KnowledgeItem(
        name=name,
        workspace=workspace,
        project=project,
        memory_type=memory_type,
        note_subtype="reflection" if memory_type == "reflection" else "memory",
        body=body,
        body_preview=str(preview_value or ""),
        attributes=dict(row.get("attributes") or {}),
        entity_ref=entity_ref,
        authority="regista" if entity_ref is not None else "agent-notes-native",
        authority_state=authority_state,
        created_at=_iso(row.get("created_at")),
        updated_at=_iso(row.get("updated_at")),
        score=float(row["score"]) if row.get("score") is not None else None,
    )


def _aggregate_items(
    items: Sequence[KnowledgeItem], invalid: int
) -> tuple[KnowledgeState, tuple[str, ...]]:
    findings: list[str] = []
    if invalid:
        findings.append(f"{invalid} record(s) had an unknown shape and were omitted")
    stale = sum(item.authority_state is KnowledgeState.STALE for item in items)
    if stale:
        findings.append(f"{stale} record(s) await authority synchronization")
    unknown = sum(item.authority_state is KnowledgeState.UNKNOWN for item in items)
    if unknown:
        findings.append(f"{unknown} signed record(s) were read from an unverified projection")
    if invalid and not items:
        return KnowledgeState.UNKNOWN, tuple(findings)
    if unknown == len(items) and items and not stale and not invalid:
        return KnowledgeState.UNKNOWN, tuple(findings)
    if invalid or unknown or (stale and stale < len(items)):
        return KnowledgeState.PARTIAL, tuple(findings)
    if stale:
        return KnowledgeState.STALE, tuple(findings)
    return KnowledgeState.CURRENT, tuple(findings)


def _status(
    state: KnowledgeState,
    detail: str,
    findings: tuple[str, ...] = (),
) -> ProviderStatus:
    return ProviderStatus(
        state=state,
        observed_at=datetime.now(timezone.utc).isoformat(),
        detail=detail,
        findings=findings,
    )


def _parse_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    offset = int(cursor)
    if offset < 0:
        raise ValueError("negative cursor")
    return offset


def _iso(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    return str(value)
