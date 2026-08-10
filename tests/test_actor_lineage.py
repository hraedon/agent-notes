"""Actor model_lineage resolution (Plan 007 / dossier cross-lineage rule).

The dossier ``adversarial_review`` validator compares the reviewer's
``model_lineage`` against author lineages and FAILS CLOSED when an agent's
lineage is undeclared. agent-notes agent actors must therefore be able to
declare a family-level lineage, sourced from environment (trust-rooted, never
prompt input) — this is the agent-notes side of G1 attribution for the shared
work-item universe.
"""

from __future__ import annotations

import pytest

from agent_notes.core.actor import (
    Actor,
    UndeclaredLineageError,
    load_actor_config,
    require_declared_lineage,
    resolve_actor,
)


@pytest.fixture(autouse=True)
def _clean_actor_env(monkeypatch, tmp_path):
    for v in (
        "AGENT_NOTES_ACTOR_ID",
        "AGENT_NOTES_MODEL_LINEAGE",
        "AGENT_NOTES_PRINCIPAL_ID",
        "AGENT_NOTES_PRINCIPAL_DISPLAY_NAME",
        "REGISTA_PRINCIPAL_ID",
    ):
        monkeypatch.delenv(v, raising=False)
    suite_env = tmp_path / "suite.env"
    monkeypatch.setenv("AGENT_SUITE_CONFIG", str(suite_env))
    monkeypatch.setenv("AGENT_SUITE_SYSTEM_CONFIG", str(suite_env))
    return suite_env


def test_resolve_actor_reads_model_lineage_from_env(monkeypatch):
    monkeypatch.setenv("AGENT_NOTES_MODEL_LINEAGE", "glm")
    monkeypatch.setenv("AGENT_NOTES_PRINCIPAL_ID", "alice@example.com")
    actor = resolve_actor(load_actor_config())
    assert actor.actor_kind == "agent"
    assert actor.model_lineage == "glm"
    meta = actor.actor_metadata()
    assert meta["model_lineage"] == "glm"


def test_actor_metadata_omits_lineage_when_unset(monkeypatch):
    monkeypatch.delenv("AGENT_NOTES_MODEL_LINEAGE", raising=False)
    monkeypatch.delenv("AGENT_NOTES_PRINCIPAL_ID", raising=False)
    monkeypatch.delenv("AGENT_NOTES_PRINCIPAL_DISPLAY_NAME", raising=False)
    # avoid git-config fallback by pointing at a non-repo; rely on defaults
    actor = resolve_actor(load_actor_config())
    assert actor.model_lineage is None
    meta = actor.actor_metadata()
    assert "model_lineage" not in meta


def test_actor_metadata_carries_lineage_through_construction():
    actor = Actor(actor_id="a", actor_kind="agent", display_name="A", model_lineage="kimi")
    assert actor.actor_metadata() == {
        "display_name": "A",
        "role": "agent",
        "model_lineage": "kimi",
    }


def test_migration_actor_has_no_lineage():
    from agent_notes.core.actor import migration_actor

    assert migration_actor().model_lineage is None


# ---------------------------------------------------------------------------
# WI-062 — fail closed on an undeclared lineage, and the config surface that
# lets a caller satisfy the requirement.
# ---------------------------------------------------------------------------


def test_lineage_resolves_from_suite_env_alone(_clean_actor_env, monkeypatch):
    """The documented per-user overlay is sufficient — no process env needed.

    ``suite_env``'s precedence is ``process env > per-user suite.env > system
    suite.env > default``, but ``load_actor_config`` used to consult the overlay
    for ``principal_id`` only. Setting the lineage in suite.env therefore looked
    right and did nothing, which is why no host in the estate had one when
    WI-062 was filed.
    """
    _clean_actor_env.write_text(
        "AGENT_NOTES_ACTOR_ID=hermes\n"
        "AGENT_NOTES_MODEL_LINEAGE=deepseek-v4-flash\n",
        encoding="utf-8",
    )
    actor = resolve_actor(load_actor_config())
    assert actor.actor_id == "hermes"
    assert actor.model_lineage == "deepseek-v4-flash"
    require_declared_lineage(actor)  # must not raise


def test_process_env_beats_suite_env(_clean_actor_env, monkeypatch):
    _clean_actor_env.write_text("AGENT_NOTES_MODEL_LINEAGE=from-file\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_NOTES_MODEL_LINEAGE", "from-env")
    assert resolve_actor(load_actor_config()).model_lineage == "from-env"


def test_explicit_override_beats_env(monkeypatch):
    """``--model-lineage`` (threaded as the override arg) wins over the env."""
    from agent_notes.core.face_factory import actor_with_overrides

    monkeypatch.setenv("AGENT_NOTES_MODEL_LINEAGE", "from-env")
    assert actor_with_overrides(None, "from-flag").model_lineage == "from-flag"


def test_whitespace_lineage_declares_nothing(monkeypatch):
    """``"   "`` is not a declaration — it must fail closed, not sneak through.

    regista's gate strips before trusting (``declared_lineage``), so a
    whitespace value would pass this side and be undeclared on the other.
    """
    monkeypatch.setenv("AGENT_NOTES_MODEL_LINEAGE", "   ")
    assert resolve_actor(load_actor_config()).model_lineage is None
    with pytest.raises(UndeclaredLineageError):
        require_declared_lineage(resolve_actor(load_actor_config()))


def test_agent_actor_without_lineage_is_refused_with_a_named_error(monkeypatch):
    from agent_notes.core.face_factory import actor_with_overrides

    with pytest.raises(UndeclaredLineageError) as exc_info:
        actor_with_overrides(operation="work-item file")

    exc = exc_info.value
    assert exc.code == "UNDECLARED_LINEAGE"
    assert exc.actor_id == "agent-notes"
    assert exc.operation == "work-item file"
    text = str(exc)
    assert "UNDECLARED_LINEAGE" in text
    # The remedy must be in the message, not in a doc the operator has to find.
    assert "AGENT_NOTES_MODEL_LINEAGE" in text
    assert "--model-lineage" in text
    assert "WI-062" in text


def test_non_agent_actors_are_not_required_to_declare_a_lineage():
    """A ``system`` actor has no model behind it; the gate never counts it."""
    from agent_notes.core.actor import migration_actor

    assert require_declared_lineage(migration_actor()) is not None
