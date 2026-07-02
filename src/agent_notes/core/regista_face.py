"""RegistaFace — agent-notes' sole choke point to regista (Plan 009).

Mirrors dossier's ``RegistaGateway`` (``dossier/src/dossier/gateway.py:27``):
every method takes a server-resolved ``Actor`` and cracks it open internally.
There is deliberately no overload that accepts an ``actor_id`` / ``actor_kind``
string — the actor is trust-rooted in environment (``core/actor.py``) and
threaded through here, which is provenance guarantee G1 (attribution) made
structural rather than conventional.

regista is the AUTHORITY for lifecycle + signed events; the local agent-notes
``work_items`` row is a search/read projection updated by callers after a
successful write (see ``work_item_model.py``). Duck-typed so ``InMemoryRegista``
works in fast tests.
"""

from __future__ import annotations

import re
from typing import Any, Protocol

from agent_notes.core.actor import Actor

# Breadcrumb source identifiers reach regista in two historical formats: the bare
# file `number` ("050") and the local-projection identifier ("BC-050"). They
# denote the same logical breadcrumb. Canonicalize by stripping a leading
# `BC-`/`bc_` prefix so the two collapse to one key — used both for the
# idempotent create lookup and the dedup repair (Plan 015). A separator is
# required so legitimate identifiers like "BCD-1" are left untouched.
_BC_PREFIX_RE = re.compile(r"^bc[-_]", re.IGNORECASE)


def normalize_source_identifier(value: str | None) -> str | None:
    """Canonicalize a breadcrumb ``source_identifier`` so ``BC-050`` == ``050``.

    Strips a leading ``BC-`` / ``bc_`` prefix (case-insensitive) and surrounding
    whitespace. ``None`` passes through. This is the single dedup key for a
    logical breadcrumb across the file-sync and migration ingestion paths.
    """
    if value is None:
        return None
    return _BC_PREFIX_RE.sub("", str(value).strip()).strip()


# Plan 010 (WI-2): agent-notes registers the single canonical workflow shipped
# from regista — the same one dossier registers — so a work-item is governed by
# one shared workflow. `breadcrumb` remains the work-item *type* (its custom
# fields are unchanged), now defined inside the canonical workflow.
WORKFLOW_NAME = "canonical"
WORK_ITEM_TYPE = "breadcrumb"


class _RegistaLike(Protocol):
    def create_work_item(self, *args: Any, **kwargs: Any) -> Any: ...
    def transition(self, *args: Any, **kwargs: Any) -> Any: ...
    def append_event(self, *args: Any, **kwargs: Any) -> Any: ...
    def register_workflow(self, yaml_content: str) -> Any: ...
    def get_work_item(self, *args: Any, **kwargs: Any) -> Any: ...
    def query_work_items(self, **kwargs: Any) -> Any: ...
    def read_events(self, **kwargs: Any) -> Any: ...
    def acquire_claim(self, *args: Any, **kwargs: Any) -> Any: ...
    def heartbeat_claim(self, *args: Any, **kwargs: Any) -> Any: ...
    def release_claim(self, *args: Any, **kwargs: Any) -> None: ...
    def close(self) -> None: ...


def _crack(actor: Actor) -> dict:
    return dict(
        actor_id=actor.actor_id,
        actor_kind=actor.actor_kind,
        actor_metadata=actor.actor_metadata(),
        on_behalf_of=actor.on_behalf_of,
    )


def packaged_workflow_yaml() -> str:
    # The canonical workflow is shipped from regista (the authoritative store),
    # so both faces register the exact same bytes — no per-face drift (Plan 010).
    import regista

    return regista.canonical_workflow_yaml()


class RegistaFace:
    """The agent/CLI face of regista. Construct with a ``Regista`` or
    ``InMemoryRegista``; registers the canonical workflow on first use."""

    def __init__(self, regista: _RegistaLike) -> None:
        self._reg = regista
        self._registered = False

    def ensure_workflow(self) -> None:
        if self._registered:
            return
        self._reg.register_workflow(packaged_workflow_yaml())
        self._registered = True

    def close(self) -> None:
        self._reg.close()

    def _ac(self, actor: Actor) -> dict:
        self.ensure_workflow()
        return _crack(actor)

    def create_breadcrumb(
        self,
        actor: Actor,
        *,
        title: str,
        description: str = "",
        severity: str = "medium",
        kind: str = "todo",
        external_refs: dict | None = None,
        diagnostic_keys: dict | None = None,
        source_identifier: str | None = None,
    ) -> tuple[Any, str]:
        ac = self._ac(actor)
        custom = {
            "title": title,
            "description": description,
            "severity": severity,
            "kind": kind,
            "external_refs": external_refs or {},
            "diagnostic_keys": diagnostic_keys or {},
        }
        if source_identifier is not None:
            custom["source_identifier"] = source_identifier
        work_item, _event = self._reg.create_work_item(
            WORKFLOW_NAME,
            WORK_ITEM_TYPE,
            ac["actor_id"],
            actor_kind=ac["actor_kind"],
            actor_metadata=ac["actor_metadata"],
            custom_fields=custom,
        )
        return work_item.work_item_id, work_item.current_state

    def amend_breadcrumb(
        self,
        actor: Actor,
        work_item_id: Any,
        *,
        current_state: str,
        title: str | None = None,
        description: str | None = None,
        severity: str | None = None,
        kind: str | None = None,
        external_refs: dict | None = None,
        diagnostic_keys: dict | None = None,
        payload: dict | None = None,
    ) -> str:
        ac = self._ac(actor)
        live_state = self._read_state(work_item_id)
        amend_state = live_state or current_state
        transition_name = _amend_transition_for(amend_state)
        custom: dict[str, Any] = {}
        for k, v in (
            ("title", title),
            ("description", description),
            ("severity", severity),
            ("kind", kind),
            ("external_refs", external_refs),
            ("diagnostic_keys", diagnostic_keys),
        ):
            if v is not None:
                custom[k] = v
        event = self._reg.transition(
            work_item_id,
            transition_name,
            ac["actor_id"],
            actor_kind=ac["actor_kind"],
            actor_metadata=ac["actor_metadata"],
            payload=payload,
            custom_fields=custom or None,
            on_behalf_of=ac["on_behalf_of"],
        )
        self._last_event = event
        return self._read_state(work_item_id) or amend_state

    def transition_breadcrumb(
        self,
        actor: Actor,
        work_item_id: Any,
        transition_name: str,
        *,
        payload: dict | None = None,
        custom_fields: dict | None = None,
        expected_event_seq: int | None = None,
    ) -> str:
        ac = self._ac(actor)
        event = self._reg.transition(
            work_item_id,
            transition_name,
            ac["actor_id"],
            actor_kind=ac["actor_kind"],
            actor_metadata=ac["actor_metadata"],
            payload=payload,
            custom_fields=custom_fields,
            on_behalf_of=ac["on_behalf_of"],
            expected_event_seq=expected_event_seq,
        )
        self._last_event = event
        return self._read_state(work_item_id)

    def _read_state(self, work_item_id: Any) -> str:
        wi = self._reg.get_work_item(work_item_id)
        return getattr(wi, "current_state", "") if wi is not None else ""

    def comment(self, actor: Actor, work_item_id: Any, body: str) -> None:
        ac = self._ac(actor)
        self._reg.append_event(
            work_item_id,
            ac["actor_id"],
            actor_kind=ac["actor_kind"],
            actor_metadata=ac["actor_metadata"],
            transition="comment",
            payload={"body": body},
            on_behalf_of=ac["on_behalf_of"],
        )

    def get(self, work_item_id: Any) -> Any:
        return self._reg.get_work_item(work_item_id)

    def list(self, *, current_states: list[str] | None = None, page_size: int = 100) -> list[Any]:
        items: list[Any] = []
        cursor: Any = None
        while True:
            page = self._reg.query_work_items(
                workflow_name=WORKFLOW_NAME,
                current_states=current_states,
                page_size=page_size,
                cursor=cursor,
            )
            items.extend(page.items)
            if not getattr(page, "has_more", False):
                break
            cursor = getattr(page, "cursor", None)
            if cursor is None:
                break
        return items

    def find_by_source_identifier(self, source_identifier: str | None) -> Any | None:
        """Return the existing work-item for ``source_identifier``, or ``None``.

        Matches on the normalized key (see ``normalize_source_identifier``), so a
        breadcrumb already filed as ``BC-050`` is found when re-synced as ``050``.
        This is the idempotency guard for the regista create path: callers look up
        before creating so a re-import updates rather than duplicates. Returns the
        first match (the store should hold at most one after the Plan 015 repair).
        """
        norm = normalize_source_identifier(source_identifier)
        if norm is None:
            return None
        cursor: Any = None
        while True:
            page = self._reg.query_work_items(
                workflow_name=WORKFLOW_NAME,
                custom_field_filters={"source_identifier": norm},
                page_size=100,
                cursor=cursor,
            )
            if page.items:
                return page.items[0]
            if not getattr(page, "has_more", False):
                return None
            cursor = getattr(page, "cursor", None)
            if cursor is None:
                return None

    def history(self, work_item_id: Any) -> list[Any]:
        return list(self._reg.read_events(work_item_id=work_item_id, limit=10_000))

    # ------------------------------------------------------------------
    # Lease axis (Plan 010 WI-2): regista claims, NOT lifecycle state.
    # claim/release/heartbeat are concurrency + liveness primitives, orthogonal
    # to the canonical workflow. The lifecycle does not move to `claimed`.
    # ------------------------------------------------------------------
    def acquire_claim(self, actor: Actor, work_item_id: Any, *, ttl_seconds: int = 300) -> Any:
        ac = self._ac(actor)
        return self._reg.acquire_claim(
            work_item_id,
            ac["actor_id"],
            ttl_seconds,
            actor_kind=ac["actor_kind"],
        )

    def heartbeat_claim(self, actor: Actor, work_item_id: Any, *, ttl_seconds: int = 300) -> Any:
        ac = self._ac(actor)
        return self._reg.heartbeat_claim(
            work_item_id,
            ac["actor_id"],
            ttl_seconds,
            actor_kind=ac["actor_kind"],
        )

    def release_claim(self, actor: Actor, work_item_id: Any) -> None:
        ac = self._ac(actor)
        self._reg.release_claim(
            work_item_id,
            ac["actor_id"],
            actor_kind=ac["actor_kind"],
        )


# Terminal canonical states cannot be amended directly (reopen first).
# Plan 013: this is intentionally a subset of lifecycle.IS_TERMINAL (which
# also includes ``closed``). On the regista path, items should only be in
# canonical states — ``closed`` is a legacy state that should not appear.
# If this set diverges from lifecycle.IS_TERMINAL, the consistency guard in
# test_lifecycle_consistency.py will flag it.
_TERMINAL_STATES = frozenset({"done"})


def _amend_transition_for(state: str) -> str:
    if state in _TERMINAL_STATES:
        raise ValueError(f"amend_breadcrumb: cannot amend terminal state {state!r} (reopen first)")
    # The canonical workflow defines `amend` as a self-transition for every
    # non-terminal state; regista resolves it by the work-item's current `from`.
    return "amend"
