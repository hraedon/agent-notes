"""Memory engine protocol — provider-neutral learned-memory interface.

Plan 020 WI-0.1: defines typed provider-neutral requests/results for the
learned-memory engine. The agent-notes pgvector search is
``NativeMemoryEngine``, the only engine: Plan 026 D6 retired the external
engine adapter. A future learned engine would implement the same protocol
(Plan 027 decides whether one comes back).

Design principles (Plan 020 §Decisions):
- One authority per content class: exact knowledge is regista-signed notes;
  learned engines produce derived context only.
- Separate ports: ``KnowledgeRepository`` owns exact records;
  ``MemoryEngine`` owns learned ingestion and recall.
- Provider-owned retrieval: an external engine owns its embeddings,
  extraction, and ranking. Agent-notes does not pre-embed its requests.
- Capability negotiation: unsupported operations are named, not silent no-ops.
- Honest consistency: ingestion returns an operation reference; pending,
  indexed, failed, cancelled, and stale are distinct states.
- Source-aware results: results identify provider, source reference, scope,
  source time, and origin class.
- No silent fallback: a configured external engine outage is unhealthy;
  native results remaining available does not make learned recall green.

stdlib-only core; provider SDKs live behind adapter packages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, assert_never, runtime_checkable

# ---------------------------------------------------------------------------
# Enums (closed sets — assert_never in every dispatch)
# ---------------------------------------------------------------------------


class EngineCapability(Enum):
    """Capability names a memory engine may declare.

    ``assert_never`` is used over this enum so a newly added capability
    can't be silently unhandled in the factory or doctor.
    """

    INGEST = "ingest"
    RECALL = "recall"
    FORGET = "forget"
    SYNTHESIZE = "synthesize"
    EXACT_SOURCE = "exact_source"
    UPDATE = "update"
    EXPORT = "export"


class OperationState(Enum):
    """Consistency state for an async ingestion operation.

    ``assert_never`` is used over this enum so a newly added state can't
    be silently unhandled in the status-checking logic.
    """

    PENDING = "pending"
    INDEXED = "indexed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    STALE = "stale"


class OriginClass(Enum):
    """Origin/provenance class for a recalled result.

    ``assert_never`` is used over this enum so a newly added class can't
    be silently unhandled in result rendering or validation.
    """

    RAW = "raw"
    EXTRACTED = "extracted"
    DERIVED = "derived"
    SYNTHESIZED = "synthesized"


class EngineHealthState(Enum):
    """Health state for a memory engine.

    ``assert_never`` is used over this enum so a newly added state can't
    be silently unhandled in doctor aggregation.
    """

    HEALTHY = "healthy"
    UNREACHABLE = "unreachable"
    DEGRADED = "degraded"
    NOT_CONFIGURED = "not_configured"
    UNAVAILABLE = "unavailable"


class SupportLevel(Enum):
    """Qualification level for a memory provider (Plan 012 WI-0.2).

    ``assert_never`` is used over this enum so a newly added level can't
    be silently unhandled in lock or doctor logic.
    """

    EXPERIMENTAL = "experimental"
    QUALIFIED = "qualified"
    RECOMMENDED = "recommended"
    UNSUPPORTED = "unsupported"


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MemoryScope:
    """Provider-neutral scope for ingestion and recall.

    Maps agent-notes' project/workspace/user/agent/session identifiers to
    provider-native scope concepts (e.g., per-tenant banks or user IDs).

    ``project_slug`` and ``workspace_slug`` are the agent-notes identifiers;
    the adapter maps them to provider-native isolation boundaries.
    """

    project_slug: str
    workspace_slug: str
    user_id: str | None = None
    agent_id: str | None = None
    session_id: str | None = None


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IngestItem:
    """One source document to ingest into the learned-memory engine.

    ``source_id`` is the stable identifier of the exact signed note
    (the regista entity_id). ``content`` is the signed body text.
    ``metadata`` carries signed actor/session/time/link context.
    """

    source_id: str
    content: str
    memory_type: str = "memory"
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: str | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True)
class IngestBatch:
    """A batch of items to ingest, with scope and idempotency."""

    items: list[IngestItem]
    scope: MemoryScope
    idempotency_key: str | None = None


@dataclass(frozen=True)
class IngestResult:
    """Result of an ingestion operation.

    For sync engines, ``operation_id`` is None and ``state`` is INDEXED.
    For async engines, ``operation_id`` is the provider's operation reference
    and ``state`` is PENDING until polling says otherwise.
    """

    operation_id: str | None
    state: OperationState
    items_count: int
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "state": self.state.value,
            "items_count": self.items_count,
            "error": self.error,
        }


@dataclass(frozen=True)
class OperationStatus:
    """Status of an async ingestion operation."""

    operation_id: str
    state: OperationState
    created_at: str | None = None
    updated_at: str | None = None
    completed_at: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "state": self.state.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Recall
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecallQuery:
    """A recall request.

    ``query`` is the natural-language search text.
    ``budget`` controls per-method retrieval limits ("low"/"mid"/"high").
    ``max_tokens`` bounds the total response size.
    ``memory_types`` filters by memory type (e.g., "reflection", "memory").
    """

    query: str
    scope: MemoryScope
    budget: str = "mid"
    max_tokens: int = 4096
    memory_types: list[str] | None = None


@dataclass(frozen=True)
class RecallResult:
    """One recalled item from the learned-memory engine.

    ``origin`` classifies the result:
    - RAW: verbatim source text (exact-source retrieval)
    - EXTRACTED: a fact extracted from source content
    - DERIVED: a derived observation or mental model
    - SYNTHESIZED: LLM-generated synthesis (if supported)

    ``source_ref`` links back to the exact signed note when one exists.
    """

    text: str
    origin: OriginClass
    score: float = 0.0
    source_ref: str | None = None
    memory_type: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "origin": self.origin.value,
            "score": self.score,
            "source_ref": self.source_ref,
            "memory_type": self.memory_type,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class RecallResponse:
    """The full recall response from the engine."""

    results: list[RecallResult]
    engine: str
    usage: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "results": [r.to_dict() for r in self.results],
            "engine": self.engine,
            "usage": self.usage,
        }


# ---------------------------------------------------------------------------
# Forget (deletion)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ForgetSelector:
    """Selector for what to forget.

    Either ``source_id`` (delete one source document and its derived
    artifacts) or ``scope`` (delete everything in a scope — use sparingly).
    """

    source_id: str | None = None
    scope: MemoryScope | None = None


@dataclass(frozen=True)
class ForgetResult:
    """Result of a forget operation.

    ``deleted_count`` is the number of source documents deleted.
    ``cascade_count`` is the number of derived artifacts (extracted facts,
    observations, etc.) that were also removed. ``receipt`` is a
    provider-native deletion receipt for audit.
    """

    deleted_count: int
    cascade_count: int = 0
    receipt: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "deleted_count": self.deleted_count,
            "cascade_count": self.cascade_count,
            "receipt": self.receipt,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Health and description
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EngineHealth:
    """Health and description of a memory engine.

    ``state`` is the overall health state.
    ``capabilities`` lists what the engine supports.
    ``version`` is the provider/adapter version.
    ``protocol_version`` is the memory-engine protocol version.
    ``indexing_backlog`` is the count of pending operations (None if N/A).
    ``indexing_freshness`` is the timestamp of the last successful indexing.
    ``detail`` is a human-readable status message.
    """

    state: EngineHealthState
    capabilities: frozenset[EngineCapability]
    version: str | None = None
    protocol_version: str = "1.0"
    engine_name: str = "native"
    indexing_backlog: int | None = None
    indexing_freshness: str | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "capabilities": sorted(c.value for c in self.capabilities),
            "version": self.version,
            "protocol_version": self.protocol_version,
            "engine_name": self.engine_name,
            "indexing_backlog": self.indexing_backlog,
            "indexing_freshness": self.indexing_freshness,
            "detail": self.detail,
        }


# ---------------------------------------------------------------------------
# Engine protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class MemoryEngine(Protocol):
    """Provider-neutral learned-memory engine.

    The native pgvector implementation implements this protocol; so would
    any future external adapter. Agent-notes' CLI and skills
    consume this interface, not concrete implementations.
    """

    @property
    def engine_name(self) -> str:
        """Stable identifier for this engine (e.g., 'native')."""
        ...

    def describe(self) -> EngineHealth:
        """Return the engine's health, capabilities, and version info."""
        ...

    def ingest(self, batch: IngestBatch) -> IngestResult:
        """Ingest a batch of source documents.

        Returns an operation reference. For sync engines, the state is
        immediately INDEXED. For async engines, the state is PENDING and
        the caller polls ``operation_status``.
        """
        ...

    def operation_status(self, operation_id: str) -> OperationStatus:
        """Check the status of an async ingestion operation."""
        ...

    def recall(self, query: RecallQuery) -> RecallResponse:
        """Recall learned context for a query.

        Results are source-labelled and treated as untrusted input to the
        agent. Derived/synthesized results cannot satisfy an exact-note
        assertion.
        """
        ...

    def forget(self, selector: ForgetSelector) -> ForgetResult:
        """Delete source document(s) and verify provider-reported cascade.

        Exact regista deletion/supersession policy remains independently
        enforced — this only affects the learned engine's derived state.
        """
        ...


# ---------------------------------------------------------------------------
# Provider factory
# ---------------------------------------------------------------------------


_PROVIDER_ENV = "AGENT_NOTES_MEMORY_ENGINE"


def get_engine() -> MemoryEngine:
    """Return the memory engine: always the native pgvector engine.

    Plan 026 D6 retired the external engine. ``AGENT_NOTES_MEMORY_ENGINE``
    (process env, then suite.env per-user > system, then the config file's
    ``memory_engine`` key) may still be set, but only to ``native``. Any other
    value is a ``ValueError``, not a silent fallback: an operator who asked for
    another engine should learn it is gone rather than get native results
    labelled as something else.
    """
    import os

    engine_name = os.environ.get(_PROVIDER_ENV, "").strip().lower()
    if not engine_name:
        try:
            from agent_notes.core.suite_env import load_suite_env

            engine_name = (load_suite_env().get(_PROVIDER_ENV) or "").strip().lower()
        except Exception:
            pass

    if not engine_name:
        try:
            from agent_notes.core.config import config_path

            path = config_path()
            if path.is_file():
                import json

                data = json.loads(path.read_text())
                if isinstance(data, dict):
                    engine_name = str(data.get("memory_engine") or "").strip().lower()
        except Exception:
            pass

    if engine_name and engine_name != "native":
        raise ValueError(
            f"Memory engine {engine_name!r} is not available: native is the only "
            f"memory engine (Plan 026 D6). Unset {_PROVIDER_ENV} (or the config "
            "file's memory_engine key), or set it to 'native'."
        )

    from agent_notes.core.native_engine import NativeMemoryEngine

    return NativeMemoryEngine()


def engine_label(state: EngineHealthState) -> str:
    """Human-readable label for an EngineHealthState (used in doctor text)."""
    match state:
        case EngineHealthState.HEALTHY:
            return "healthy"
        case EngineHealthState.UNREACHABLE:
            return "unreachable"
        case EngineHealthState.DEGRADED:
            return "degraded"
        case EngineHealthState.NOT_CONFIGURED:
            return "not configured"
        case EngineHealthState.UNAVAILABLE:
            return "unavailable"
        case other:
            assert_never(other)


def capability_label(cap: EngineCapability) -> str:
    """Human-readable label for an EngineCapability."""
    match cap:
        case EngineCapability.INGEST:
            return "ingest"
        case EngineCapability.RECALL:
            return "recall"
        case EngineCapability.FORGET:
            return "forget"
        case EngineCapability.SYNTHESIZE:
            return "synthesize"
        case EngineCapability.EXACT_SOURCE:
            return "exact_source"
        case EngineCapability.UPDATE:
            return "update"
        case EngineCapability.EXPORT:
            return "export"
        case other:
            assert_never(other)


def support_level_label(level: SupportLevel) -> str:
    """Human-readable label for a SupportLevel."""
    match level:
        case SupportLevel.EXPERIMENTAL:
            return "experimental"
        case SupportLevel.QUALIFIED:
            return "qualified"
        case SupportLevel.RECOMMENDED:
            return "recommended"
        case SupportLevel.UNSUPPORTED:
            return "unsupported"
        case other:
            assert_never(other)
