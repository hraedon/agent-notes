"""Tests for degrade contract / coordination mode (Plan 008 P4 Tier A)."""

from __future__ import annotations

from agent_notes.core import coordinator


class TestCoordinationMode:
    def test_default_mode_is_local_lease(self, monkeypatch):
        monkeypatch.delenv("AGENT_NOTES_COORDINATOR_URL", raising=False)
        assert coordinator.get_coordination_mode() == "coordinator-absent / local-lease"

    def test_distributed_claim_not_available(self, monkeypatch):
        monkeypatch.delenv("AGENT_NOTES_COORDINATOR_URL", raising=False)
        assert coordinator.is_distributed_claim_available() is False

    def test_coordinator_configured_reports_tier_b_pending(self, monkeypatch):
        monkeypatch.setenv("AGENT_NOTES_COORDINATOR_URL", "http://localhost:9999")
        mode = coordinator.get_coordination_mode()
        assert "coordinator-present" in mode
        assert "Tier B pending" in mode

    def test_check_health_when_absent(self, monkeypatch):
        monkeypatch.delenv("AGENT_NOTES_COORDINATOR_URL", raising=False)
        ok, msg = coordinator.check_coordinator_health()
        assert ok is False
        assert "coordinator-absent" in msg
        assert "local-lease" in msg

    def test_check_health_when_configured(self, monkeypatch):
        monkeypatch.setenv("AGENT_NOTES_COORDINATOR_URL", "http://localhost:9999")
        ok, msg = coordinator.check_coordinator_health()
        assert ok is False
        assert "configured" in msg
        assert "Tier B" in msg
