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

from typing import Any, Protocol

from agent_notes.core.actor import Actor

WORKFLOW_NAME = "breadcrumb"
WORK_ITEM_TYPE = "breadcrumb"


class _RegistaLike(Protocol):
    def create_work_item(self, *args: Any, **kwargs: Any) -> Any: ...
    def transition(self, *args: Any, **kwargs: Any) -> Any: ...
    def append_event(self, *args: Any, **kwargs: Any) -> Any: ...
    def register_workflow(self, yaml_content: str) -> Any: ...
    def get_work_item(self, *args: Any, **kwargs: Any) -> Any: ...
    def query_work_items(self, **kwargs: Any) -> Any: ...
    def read_events(self, **kwargs: Any) -> Any: ...
    def close(self) -> None: ...


def _crack(actor: Actor) -> dict:
    return dict(
        actor_id=actor.actor_id,
        actor_kind=actor.actor_kind,
        actor_metadata=actor.actor_metadata(),
        on_behalf_of=actor.on_behalf_of,
    )


def packaged_workflow_yaml() -> str:
    import importlib.resources as ir

    return (ir.files("agent_notes") / "workflows" / "breadcrumb.workflow.yaml").read_text()


class RegistaFace:
    """The agent/CLI face of regista. Construct with a ``Regista`` or
    ``InMemoryRegista``; registers the breadcrumb workflow on construction."""

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
        page = self._reg.query_work_items(
            workflow_name=WORKFLOW_NAME,
            current_states=current_states,
            page_size=page_size,
        )
        return list(page.items)

    def history(self, work_item_id: Any) -> list[Any]:
        return list(self._reg.read_events(work_item_id=work_item_id, limit=10_000))


def _amend_transition_for(state: str) -> str:
    table = {"open": "amend_open", "claimed": "amend_claimed", "deferred": "amend_deferred"}
    if state not in table:
        raise ValueError(f"amend_breadcrumb: no amend transition for state {state!r}")
    return table[state]
