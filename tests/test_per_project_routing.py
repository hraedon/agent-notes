"""Plan 011 WI-3 — per-project regista routing.

The convergence store is one regista project (schema) per software-project. The
write path must route to the schema matching the current project, not a single
static one. These tests cover the slug->schema mapping, contextvar routing,
caching, fallback, and the test-override precedence.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_notes.core import face_factory


@pytest.fixture(autouse=True)
def _clean_faces():
    face_factory.reset_face()
    yield
    face_factory.reset_face()


def _cfg(enabled=True, project="default_proj"):
    return SimpleNamespace(
        enabled=enabled, project=project, dsn="x", hmac_key_path="k", require_ssl=False
    )


def test_regista_project_name_maps_hyphens_to_underscores():
    assert face_factory.regista_project_name("cert-watch") == "cert_watch"
    assert face_factory.regista_project_name("agent-capability-broker") == "agent_capability_broker"
    assert face_factory.regista_project_name("sf2") == "sf2"


def test_get_face_routes_and_caches_per_project(monkeypatch):
    built: list[str] = []

    def fake_build(cfg, project):
        built.append(project)
        # _build_face returns (face, cleanup) — Plan 017 WI-4.1 added the
        # cleanup so a backend-sourced key manifest is scrubbed on reset.
        return SimpleNamespace(project=project, close=lambda: None), None

    monkeypatch.setattr(face_factory, "regista_config", lambda: _cfg())
    monkeypatch.setattr(face_factory, "_build_face", fake_build)

    # No current project -> falls back to cfg.project.
    assert face_factory.get_face().project == "default_proj"

    face_factory.set_current_project("proj_a")
    fa = face_factory.get_face()
    face_factory.set_current_project("proj_b")
    fb = face_factory.get_face()
    assert fa.project == "proj_a" and fb.project == "proj_b"
    assert fa is not fb

    # Same project -> cached instance, not rebuilt.
    face_factory.set_current_project("proj_a")
    assert face_factory.get_face() is fa
    assert built == ["default_proj", "proj_a", "proj_b"]


def test_test_override_wins_over_routing():
    sentinel = SimpleNamespace(name="override", close=lambda: None)
    face_factory.set_face_for_test(sentinel)
    face_factory.set_current_project("anything")
    assert face_factory.get_face() is sentinel


def test_disabled_returns_none(monkeypatch):
    monkeypatch.setattr(face_factory, "regista_config", lambda: _cfg(enabled=False))
    face_factory.set_current_project("proj_a")
    assert face_factory.get_face() is None
