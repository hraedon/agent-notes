"""Plan 015 — idempotent breadcrumb filing into regista.

Regression coverage for the duplication bug: a re-import of an already-filed
breadcrumb (because the local projection used for the create-vs-update decision
was stale relative to the remote SoT) minted a duplicate work-item instead of
updating. The fix normalizes the source identifier and looks it up in regista
before creating.

The pure-unit and face-level tests need no Postgres (InMemoryRegista). The
end-to-end ``file_work_item`` idempotency test is DB-backed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_notes.core.actor import Actor
from agent_notes.core.regista_face import RegistaFace, normalize_source_identifier
from tests.conftest import provision_v6_regista


class TestNormalizeSourceIdentifier:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("BC-050", "050"),
            ("bc-050", "050"),
            ("BC_050", "050"),
            ("050", "050"),
            ("  BC-050  ", "050"),
            ("313", "313"),
            ("BC-313", "313"),
            # Non-breadcrumb identifiers are left intact (separator required).
            ("BCD-1", "BCD-1"),
            ("WI-005", "WI-005"),
            ("", ""),
        ],
    )
    def test_canonicalizes(self, raw, expected):
        assert normalize_source_identifier(raw) == expected

    def test_none_passes_through(self):
        assert normalize_source_identifier(None) is None

    def test_two_formats_collapse_to_one_key(self):
        assert normalize_source_identifier("BC-050") == normalize_source_identifier("050")


class TestFindBySourceIdentifier:
    @pytest.fixture
    def face(self, tmp_path: Path) -> RegistaFace:
        return RegistaFace(provision_v6_regista(tmp_path / "v6_keys.json"))

    @pytest.fixture
    def actor(self) -> Actor:
        return Actor(actor_id="agent:ac-test-agent", actor_kind="agent", display_name="Test")

    def test_finds_item_filed_under_other_format(self, face, actor):
        # Filed with the bare number; looked up with the BC- form.
        wid, _ = face.create_breadcrumb(
            actor, title="poll_hooks dead-letter", source_identifier="050"
        )
        found = face.find_by_source_identifier("BC-050")
        assert found is not None
        assert found.work_item_id == wid

    def test_finds_item_filed_with_prefix(self, face, actor):
        wid, _ = face.create_breadcrumb(
            actor, title="x", source_identifier=normalize_source_identifier("BC-313")
        )
        assert face.find_by_source_identifier("313").work_item_id == wid
        assert face.find_by_source_identifier("BC-313").work_item_id == wid

    def test_missing_returns_none(self, face):
        assert face.find_by_source_identifier("999") is None

    def test_none_returns_none(self, face):
        assert face.find_by_source_identifier(None) is None
