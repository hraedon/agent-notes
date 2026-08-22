from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_notes.core.regista_face import RegistaFace


@dataclass(frozen=True)
class _Event:
    entity_kind: str


class _RegistaStub:
    def __init__(self, events: list[_Event]) -> None:
        self.events = events

    def read_events(self, **_kwargs: Any) -> list[_Event]:
        return self.events


def test_read_note_events_filters_same_uuid_work_item_events() -> None:
    note = _Event(entity_kind="note")
    work_item = _Event(entity_kind="work_item")
    face = RegistaFace(_RegistaStub([work_item, note]))  # type: ignore[arg-type]

    assert face.read_note_events("shared-uuid") == [note]
