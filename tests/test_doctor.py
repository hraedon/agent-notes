"""Tests for agent-notes-doctor health-check script (Phase 6.3).

Drives the checks against a clean ephemeral DB. Seeding workspace/project/vocab
is required so the schema checks pass.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from agent_notes.core import db as coredb
from tests.conftest import ephemeral_db  # noqa: F401

pytestmark = pytest.mark.usefixtures("ephemeral_db")


@pytest.fixture(autouse=True)
def _isolated_harness_home(monkeypatch, tmp_path):
    """Synthetic store checks must not inherit an operator's harness health.

    Keep the real Codex check active, with an absent installation as the
    controlled default. Dedicated cases can override this probe to assert
    that an explicitly configured stale installation still fails.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    monkeypatch.setattr(
        "agent_notes.scripts.doctor._probe_codex_agent_notes_plugin",
        lambda: ("absent", "fixture has no plugin"),
    )


def _fake_embed(text, task="document"):
    return np.zeros(768, dtype=np.float32)


class TestDoctorClean:
    @pytest.fixture(autouse=True)
    def _seed(self):
        ws = coredb.get_or_create_workspace("doc-ws", "Doc WS")
        coredb.get_or_create_project(
            ws.id,
            slug="doc-proj",
            name="Doc Proj",
            repo_root="/tmp",
        )
        coredb.add_vocabulary(ws.id, "bc_kind", "bug")
        coredb.add_vocabulary(ws.id, "bc_status", "new")
        coredb.add_vocabulary(ws.id, "bc_severity", "medium")
        coredb.add_vocabulary(ws.id, "memory_type", "note")

    def test_doctor_clean_exit_code(self, capsys):
        from agent_notes.scripts.doctor import run

        suite_patches = (
            patch(
                "agent_notes.scripts.doctor._check_skills_installed",
                return_value=(True, "mocked skills"),
            ),
            patch(
                "agent_notes.scripts.doctor._check_harness_wired",
                return_value=(True, "mocked harness"),
            ),
            patch(
                "agent_notes.scripts.doctor._check_chain_ok",
                return_value=(True, "mocked chain ok"),
            ),
        )
        for p in suite_patches:
            p.start()
        try:
            with patch(
                "agent_notes.scripts.doctor._check_embedding", return_value=(True, "mocked")
            ):
                code = run(check_embed=True)
        finally:
            for p in suite_patches:
                p.stop()
        captured = capsys.readouterr()
        assert code == 0, f"Doctor failed: {captured.out}"
        assert "DSN" in captured.out
        assert "embedding" in captured.out.lower()
        assert "Schema" in captured.out
        assert "Links Audit" in captured.out
        assert "Vocabulary Integrity" in captured.out
        assert "All checks passed" in captured.out


class TestDoctorDanglingLink:
    @pytest.fixture(autouse=True)
    def _seed(self):
        ws = coredb.get_or_create_workspace("doc-link-ws", "Doc Link WS")
        proj = coredb.get_or_create_project(
            ws.id,
            slug="doc-link-proj",
            name="Doc Link Proj",
            repo_root="/tmp",
        )
        coredb.add_vocabulary(ws.id, "bc_kind", "bug")
        coredb.add_vocabulary(ws.id, "bc_status", "new")
        coredb.add_vocabulary(ws.id, "bc_severity", "medium")
        coredb.add_vocabulary(ws.id, "memory_type", "note")

        from agent_notes.core.db import _conn

        with _conn() as conn:
            cur = conn.cursor()
            # Soft cross-project wikilink: target absent locally but the
            # relationship is 'relates_to', so this must NOT fail the audit.
            cur.execute(
                """
                INSERT INTO links
                    (from_kind, from_workspace, from_project, from_identifier,
                     to_kind, to_workspace, to_project, to_identifier, relationship)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    "breadcrumb",
                    ws.id,
                    proj.id,
                    "NONEXISTENT",
                    "memory",
                    ws.id,
                    proj.id,
                    "ghost",
                    "relates_to",
                ),
            )
            # Strict edge: uses a fabricated strict relationship value
            # ('supersedes'). In production, the strict path catches any
            # non-relates_to memory edge — 'supersedes' is a stand-in for
            # "any non-relates_to value." No code path creates this
            # relationship value in the links table today.
            cur.execute(
                """
                INSERT INTO links
                    (from_kind, from_workspace, from_project, from_identifier,
                     to_kind, to_workspace, to_project, to_identifier, relationship)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    "memory",
                    ws.id,
                    proj.id,
                    "strict-source",
                    "memory",
                    ws.id,
                    proj.id,
                    "strict-ghost",
                    "supersedes",
                ),
            )
            conn.commit()

    def test_doctor_catches_strict_dangling_link(self, capsys):
        from agent_notes.scripts.doctor import run

        with patch("agent_notes.scripts.doctor._check_embedding", return_value=(True, "mocked")):
            code = run(check_embed=True)
        captured = capsys.readouterr()
        # The strict supersedes edge must fail the audit.
        assert code == 1, f"Expected failure, got: {captured.out}"
        assert "Dangling strict links" in captured.out

    def test_doctor_soft_wikilink_is_informational(self):
        """Soft relates_to wikilinks to absent targets are cross-project refs,
        not integrity violations. The audit must report them as informational
        and must not include them in the failing strict count."""
        # Re-seed: remove the strict edge so only the soft wikilink remains.
        from agent_notes.core.db import _conn
        from agent_notes.scripts.doctor import _check_links_audit

        with _conn() as conn:
            conn.execute("DELETE FROM links WHERE relationship = 'supersedes'")
            conn.commit()

        ok, detail = _check_links_audit()
        # Informational skip: not ok=True (no fail), not strict-dangling.
        assert ok is None, f"Expected informational skip, got ok={ok!r}: {detail}"
        assert "cross-project" in detail
        assert "1 cross-project" in detail

    def test_doctor_orphan_to_deleted_memory_is_strict_failure(self):
        """A relates_to wikilink to a soft-deleted memory is a local orphan,
        not a cross-project reference. The audit must fail (not skip) and must
        not misreport it as a cross-project ref.

        Setup: create a target memory, create an inbound relates_to wikilink
        from another memory, soft-delete the target. The link's target name
        exists in the DB but is inactive — a real broken edge.
        """
        from agent_notes.core.db import _conn
        from agent_notes.core.memory_model import add_memory, delete_memory
        from agent_notes.scripts.doctor import _check_links_audit

        # The class-level _seed fixture inserts a strict 'supersedes' edge and
        # a soft 'relates_to' edge in a different project. The audit is global
        # (not per-project), so clear those to get a clean baseline.
        with _conn() as conn:
            conn.execute(
                "DELETE FROM links WHERE from_identifier IN ('NONEXISTENT', 'strict-source')"
            )
            conn.commit()

        ws = coredb.get_or_create_workspace("orphan-ws", "Orphan WS")
        proj = coredb.get_or_create_project(
            ws.id, slug="orphan-proj", name="Orphan Proj", repo_root="/tmp"
        )
        coredb.add_vocabulary(ws.id, "memory_type", "note")

        # Target memory that will be soft-deleted.
        add_memory(ws.id, proj.id, "target-mem", "note", "target body")
        # Source memory with a wikilink to the target.
        add_memory(ws.id, proj.id, "source-mem", "note", "see [[target-mem]]")

        # Sanity: before delete, the audit should be clean (both memories active).
        ok, detail = _check_links_audit()
        assert ok is True, f"Expected clean audit before delete, got: {detail}"

        # Soft-delete the target — the inbound wikilink now dangles.
        deleted = delete_memory(ws.id, proj.id, "target-mem")
        assert deleted is not None, "delete_memory returned None — target not found"

        ok, detail = _check_links_audit()
        # Must be a failure (ok=False), NOT a skip (ok=None).
        assert ok is False, f"Expected failure for local orphan, got ok={ok!r}: {detail}"
        # Must NOT be misreported as a cross-project reference.
        assert "cross-project" not in detail, f"Local orphan misreported as cross-project: {detail}"
        assert "orphan" in detail.lower(), f"Expected orphan in detail: {detail}"

    def test_doctor_reports_strict_failure_and_soft_count_together(self):
        """When both a strict dangling edge and a soft cross-project edge are
        present, the audit must fail (strict) AND disclose the soft count in
        the same detail — so the operator sees the full picture, not just the
        strict bucket, and the audit doesn't lie about being clean.
        """
        from agent_notes.core.db import _conn
        from agent_notes.scripts.doctor import _check_links_audit

        ws = coredb.get_or_create_workspace("both-ws", "Both WS")
        proj = coredb.get_or_create_project(
            ws.id, slug="both-proj", name="Both Proj", repo_root="/tmp"
        )

        with _conn() as conn:
            cur = conn.cursor()
            # Strict dangling edge: non-relates_to relationship, target absent.
            cur.execute(
                """
                INSERT INTO links
                    (from_kind, from_workspace, from_project, from_identifier,
                     to_kind, to_workspace, to_project, to_identifier, relationship)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    "memory",
                    ws.id,
                    proj.id,
                    "strict-src",
                    "memory",
                    ws.id,
                    proj.id,
                    "strict-ghost",
                    "derived_from",
                ),
            )
            # Soft cross-project edge: relates_to, target absent locally.
            cur.execute(
                """
                INSERT INTO links
                    (from_kind, from_workspace, from_project, from_identifier,
                     to_kind, to_workspace, to_project, to_identifier, relationship)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    "memory",
                    ws.id,
                    proj.id,
                    "soft-src",
                    "memory",
                    ws.id,
                    proj.id,
                    "foreign-ghost",
                    "relates_to",
                ),
            )
            conn.commit()

        ok, detail = _check_links_audit()
        # Must fail because of the strict edge.
        assert ok is False, f"Expected failure, got ok={ok!r}: {detail}"
        # Must contain the strict dangling count.
        assert "Dangling strict links" in detail, f"Missing strict count: {detail}"
        # Must also disclose the cross-project soft count (preferred behavior).
        assert "cross-project" in detail, (
            f"Expected cross-project count in strict-failure detail: {detail}"
        )


class TestDoctorSkipsOnDsnFailure:
    def test_skips_embedding_when_dsn_fails(self, capsys):
        from agent_notes.scripts.doctor import run

        with patch(
            "agent_notes.scripts.doctor._check_dsn",
            return_value=(False, "DSN not set"),
        ):
            code = run()
        captured = capsys.readouterr()
        assert code == 1
        assert "SKIPPED" in captured.out
        assert "prerequisite" in captured.out.lower()
        assert "Embedding Model" in captured.out
        assert "Links Audit" in captured.out
        assert "Vocabulary Integrity" in captured.out


class TestDoctorSkipsEmbeddingByDefault:
    @pytest.fixture(autouse=True)
    def _seed(self):
        ws = coredb.get_or_create_workspace("doc-embed-ws", "Doc Embed WS")
        coredb.get_or_create_project(
            ws.id,
            slug="doc-embed-proj",
            name="Doc Embed Proj",
            repo_root="/tmp",
        )
        coredb.add_vocabulary(ws.id, "bc_kind", "bug")
        coredb.add_vocabulary(ws.id, "bc_status", "new")
        coredb.add_vocabulary(ws.id, "bc_severity", "medium")
        coredb.add_vocabulary(ws.id, "memory_type", "note")

    def test_embedding_skipped_without_check_embed(self, capsys):
        from agent_notes.scripts.doctor import run

        run(check_embed=False)
        captured = capsys.readouterr()
        assert "SKIPPED: use --check-embed" in captured.out


# ---------------------------------------------------------------------------
# Plan 017 WI-3.1 — suite-shape `doctor --json`
# ---------------------------------------------------------------------------


class TestDoctorJsonSuiteShape:
    """The suite contract (blueprint §2.4 / Plan 017 WI-3.1) requires every
    component's ``doctor --json`` to emit a common shape so a suite-doctor
    umbrella can aggregate them. Plan 026 D1 removed the ``regista`` block and
    the ``degraded`` status: regista write-through is deleted, so an absent
    regista is the normal state, never a degraded one.
    """

    @pytest.fixture(autouse=True)
    def _seed(self):
        ws = coredb.get_or_create_workspace("doc-json-ws", "Doc JSON WS")
        coredb.get_or_create_project(
            ws.id,
            slug="doc-json-proj",
            name="Doc JSON Proj",
            repo_root="/tmp",
        )
        coredb.add_vocabulary(ws.id, "bc_kind", "bug")
        coredb.add_vocabulary(ws.id, "bc_status", "new")
        coredb.add_vocabulary(ws.id, "bc_severity", "medium")
        coredb.add_vocabulary(ws.id, "memory_type", "note")

    def _required_top_keys(self):
        return {"component", "version", "ok", "degraded", "status", "checks"}

    def _hermetic(self, doctor):
        """Pin DB-state and host-state checks against session-shared rows and
        the operator's installed harnesses."""
        return [
            patch.object(doctor, "_check_chain_ok", return_value=(True, "0 violations")),
            patch.object(doctor, "_check_links_audit", return_value=(True, "clean")),
            patch.object(doctor, "_check_vocab_integrity", return_value=(True, "clean")),
            patch.object(doctor, "_check_skills_installed", return_value=(True, "7 skills")),
            patch.object(doctor, "_check_harness_wired", return_value=(True, "wired")),
            patch.object(doctor, "_check_codex_harness", return_value=(None, "absent")),
            patch.object(
                doctor, "_probe_codex_agent_notes_plugin", return_value=("absent", "absent")
            ),
            patch.object(doctor, "_check_bridge_target", return_value=(True, "disabled")),
            patch.object(doctor, "_check_harness_configs", return_value=(True, "clean")),
        ]

    def _run(self, doctor):
        patchers = self._hermetic(doctor)
        for p in patchers:
            p.start()
        try:
            return doctor.run_json()
        finally:
            for p in patchers:
                p.stop()

    def test_emits_suite_shape(self):
        from agent_notes.scripts.doctor import run_json

        payload, code = run_json()
        assert self._required_top_keys() <= set(payload)
        assert "regista" not in payload
        assert payload["component"] == "agent-notes"
        assert payload["version"] != "unknown"
        assert payload["status"] in {"healthy", "unhealthy"}
        assert isinstance(payload["ok"], bool)
        assert payload["degraded"] is False
        # The umbrella-read boolean agrees with the human-facing status.
        assert payload["ok"] == (payload["status"] != "unhealthy")
        assert isinstance(payload["checks"], list) and payload["checks"]
        names = {check["name"] for check in payload["checks"]}
        assert "codex_harness" in names
        assert "retired_settings" in names
        assert not names & {"regista_reachable", "regista_face", "secrets_backend"}
        for c in payload["checks"]:
            assert {"name", "status", "detail"} <= set(c)
            assert c["status"] in {"ok", "warn", "fail", "skip"}

    def test_absent_regista_is_healthy_not_degraded(self, monkeypatch):
        """No regista configuration at all is the normal state (Plan 026 D1)."""
        from agent_notes.scripts import doctor

        for name in ("REGISTA_DSN", "REGISTA_KEY_PATH", "AGENT_NOTES_REGISTA_WRITES"):
            monkeypatch.delenv(name, raising=False)
        payload, code = self._run(doctor)
        assert code == 0, payload
        assert payload["status"] == "healthy"
        assert payload["degraded"] is False
        modes = {c["name"]: c for c in payload["checks"]}
        assert modes["coordination_mode"]["status"] == "ok"
        assert "local-lease" in modes["coordination_mode"]["detail"]

    def test_configured_regista_dsn_is_ignored(self, monkeypatch):
        """A leftover REGISTA_DSN is never probed, so it can't fail or degrade."""
        from agent_notes.scripts import doctor

        monkeypatch.setenv("REGISTA_DSN", "postgresql://nobody@127.0.0.1:1/none")
        monkeypatch.delenv("AGENT_NOTES_REGISTA_WRITES", raising=False)
        payload, code = self._run(doctor)
        assert code == 0, payload
        assert payload["status"] == "healthy"
        assert "nobody" not in str(payload)

    def test_retired_regista_writes_setting_fails(self, monkeypatch):
        from agent_notes.scripts import doctor

        monkeypatch.setenv("AGENT_NOTES_REGISTA_WRITES", "1")
        payload, code = self._run(doctor)
        checks = {c["name"]: c for c in payload["checks"]}
        assert checks["retired_settings"]["status"] == "fail"
        assert "025-regista-cutover.md" in checks["retired_settings"]["detail"]
        assert code == 1
        assert payload["status"] == "unhealthy"

    def test_configured_stale_codex_plugin_still_fails(self, monkeypatch):
        from agent_notes.scripts import doctor

        monkeypatch.setattr(
            doctor, "_probe_codex_agent_notes_plugin", lambda: ("stale", "fixture version mismatch")
        )
        payload, code = doctor.run_json()
        checks = {check["name"]: check for check in payload["checks"]}
        assert checks["codex_harness"]["status"] == "fail"
        assert "stale" in checks["codex_harness"]["detail"]
        assert payload["status"] == "unhealthy"
        assert code == 1

    def test_retired_memory_engine_setting_fails(self, monkeypatch):
        from agent_notes.scripts import doctor

        monkeypatch.setenv("AGENT_NOTES_MEMORY_ENGINE", "hindsight")
        payload, code = self._run(doctor)
        checks = {c["name"]: c for c in payload["checks"]}
        assert checks["memory_provider"]["status"] == "fail"
        assert "native is the only" in checks["memory_provider"]["detail"]
        assert code == 1

    def test_native_memory_engine_setting_is_clean(self, monkeypatch):
        from agent_notes.scripts import doctor

        monkeypatch.setenv("AGENT_NOTES_MEMORY_ENGINE", "native")
        payload, code = self._run(doctor)
        checks = {c["name"]: c for c in payload["checks"]}
        assert checks["memory_provider"]["status"] == "skip"
        assert code == 0, payload


class TestLegacyOutbox:
    """Plan 026 D5: a leftover regista outbox queue must be archived, not lost."""

    def test_absent_directory_passes(self, monkeypatch, tmp_path):
        from agent_notes.scripts import doctor

        monkeypatch.setenv("AGENT_NOTES_OUTBOX_DIR", str(tmp_path / "outbox"))
        ok, msg = doctor._check_legacy_outbox()
        assert ok is True
        assert "no legacy outbox" in msg

    def test_empty_queue_files_pass(self, monkeypatch, tmp_path):
        from agent_notes.scripts import doctor

        root = tmp_path / "outbox"
        (root / "agent_notes").mkdir(parents=True)
        (root / "agent_notes" / "session.jsonl").write_text("")
        monkeypatch.setenv("AGENT_NOTES_OUTBOX_DIR", str(root))
        ok, msg = doctor._check_legacy_outbox()
        assert ok is True
        assert "empty" in msg

    def test_non_empty_queue_fails_with_archive_message(self, monkeypatch, tmp_path):
        from agent_notes.scripts import doctor

        root = tmp_path / "outbox"
        (root / "agent_notes").mkdir(parents=True)
        queued = root / "agent_notes" / "abc.jsonl"
        queued.write_text('{"not": "parsed"}\n')
        (root / "agent_notes" / "abc.conflicts.jsonl").write_text("x\n")
        before = queued.read_bytes()
        monkeypatch.setenv("AGENT_NOTES_OUTBOX_DIR", str(root))

        ok, msg = doctor._check_legacy_outbox()
        assert ok is False
        assert "2 non-empty legacy regista outbox file(s)" in msg
        assert "agent_notes/abc.jsonl" in msg.replace("\\", "/")
        assert "archive" in msg
        # Read-only: the queue is left exactly as it was.
        assert queued.read_bytes() == before

    def test_default_path_is_the_old_platform_state_dir(self, monkeypatch):
        import platformdirs

        from agent_notes.scripts import doctor

        monkeypatch.delenv("AGENT_NOTES_OUTBOX_DIR", raising=False)
        expected = Path(platformdirs.user_state_dir("regista")) / "outbox"
        assert doctor._legacy_outbox_dir() == expected

    def test_json_reports_it_as_a_failing_check(self, monkeypatch, tmp_path):
        from agent_notes.scripts import doctor

        root = tmp_path / "outbox"
        root.mkdir()
        (root / "s.jsonl").write_text("op\n")
        monkeypatch.setenv("AGENT_NOTES_OUTBOX_DIR", str(root))
        with patch.object(doctor, "_check_dsn", return_value=(False, "no dsn")):
            payload, code = doctor.run_json()
        checks = {c["name"]: c for c in payload["checks"]}
        assert checks["legacy_outbox"]["status"] == "fail"
        assert code == 1
