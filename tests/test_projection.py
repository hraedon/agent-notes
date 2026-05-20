"""Integration tests for core/projection.py.

Projection itself has no DB dependency; tests run without ephemeral_db.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from agent_notes.core.projection import (
    SafeWriteResult,
    parse_frontmatter,
    render_frontmatter,
    render_index,
    safe_write,
    slugify,
)


class TestFrontmatter:
    def test_round_trip_simple(self) -> None:
        text = "---\ntitle: Hello\nstatus: open\n---\nBody text."
        fm, body = parse_frontmatter(text)
        assert fm["title"] == "Hello"
        assert fm["status"] == "open"
        assert body.strip() == "Body text."

    def test_no_frontmatter(self) -> None:
        text = "Just a body."
        fm, body = parse_frontmatter(text)
        assert fm == {}
        assert body.strip() == "Just a body."

    def test_render_frontmatter_sorted_keys(self) -> None:
        fm = {"z_key": "z", "a_key": "a", "m_key": "m"}
        rendered = render_frontmatter(fm)
        assert rendered.startswith("---\n")
        assert rendered.endswith("---\n")
        # Keys must be sorted
        lines = [ln for ln in rendered.split("\n") if ":" in ln]
        keys = [ln.split(":")[0].strip() for ln in lines]
        assert keys == sorted(keys)

    def test_render_frontmatter_empty(self) -> None:
        assert render_frontmatter({}) == ""

    def test_parse_render_round_trip(self) -> None:
        original = {"identifier": "BC-001", "status": "open", "title": "Test BC"}
        rendered = render_frontmatter(original)
        fm, _ = parse_frontmatter(rendered + "\nBody.")
        assert fm["identifier"] == "BC-001"
        assert fm["status"] == "open"


class TestSlugify:
    def test_basic(self) -> None:
        assert slugify("Hello World") == "hello-world"

    def test_special_chars(self) -> None:
        assert slugify("RFC: Fix the thing!") == "rfc-fix-the-thing"

    def test_numbers(self) -> None:
        assert slugify("Phase 2a Plan") == "phase-2a-plan"

    def test_leading_trailing_stripped(self) -> None:
        s = slugify("  foo bar  ")
        assert not s.startswith("-")
        assert not s.endswith("-")


class TestRenderIndex:
    def test_empty_rows(self) -> None:
        result = render_index([], "breadcrumb")
        assert "Identifier" in result

    def test_rows_appear(self) -> None:
        rows = [
            {"identifier": "BC-001", "title": "Fix bug", "status": "open"},
            {"identifier": "BC-002", "title": "Add feature", "status": "resolved"},
        ]
        result = render_index(rows, "breadcrumb")
        assert "BC-001" in result
        assert "Fix bug" in result
        assert "resolved" in result

    def test_unknown_kind_uses_default(self) -> None:
        rows = [{"identifier": "X-1", "title": "T", "status": "s"}]
        result = render_index(rows, "unknown_kind_xyz")
        assert "X-1" in result


class TestSafeWrite:
    def test_creates_new_file(self, tmp_path: Path) -> None:
        p = tmp_path / "subdir" / "note.md"
        outcome = safe_write(p, "hello", expected_sha256=None)
        assert outcome.result == SafeWriteResult.WRITTEN
        assert p.read_text() == "hello"
        assert outcome.new_sha256 == hashlib.sha256(b"hello").digest()

    def test_unchanged_if_same_content(self, tmp_path: Path) -> None:
        p = tmp_path / "note.md"
        p.write_text("hello")
        outcome = safe_write(p, "hello", expected_sha256=None)
        assert outcome.result == SafeWriteResult.UNCHANGED
        assert outcome.new_sha256 == hashlib.sha256(b"hello").digest()

    def test_overwrites_when_expected_matches(self, tmp_path: Path) -> None:
        p = tmp_path / "note.md"
        p.write_bytes(b"old content")
        expected = hashlib.sha256(b"old content").digest()
        outcome = safe_write(p, "new content", expected_sha256=expected)
        assert outcome.result == SafeWriteResult.WRITTEN
        assert p.read_text() == "new content"

    def test_drift_when_expected_mismatch(self, tmp_path: Path) -> None:
        p = tmp_path / "note.md"
        p.write_bytes(b"human edited content")
        wrong_expected = hashlib.sha256(b"original content").digest()
        outcome = safe_write(p, "new content", expected_sha256=wrong_expected)
        assert outcome.result == SafeWriteResult.DRIFT
        # File must NOT have been modified.
        assert p.read_text() == "human edited content"

    def test_first_write_no_expected(self, tmp_path: Path) -> None:
        # expected_sha256=None means first projection; overwrite whatever is there.
        p = tmp_path / "note.md"
        p.write_bytes(b"some existing content")
        outcome = safe_write(p, "projection content", expected_sha256=None)
        assert outcome.result == SafeWriteResult.WRITTEN
        assert p.read_text() == "projection content"

    def test_parent_mkdir(self, tmp_path: Path) -> None:
        p = tmp_path / "a" / "b" / "c" / "note.md"
        outcome = safe_write(p, "deep", expected_sha256=None)
        assert outcome.result == SafeWriteResult.WRITTEN
        assert p.exists()
