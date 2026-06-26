"""Actor model_lineage resolution (Plan 007 / dossier cross-lineage rule).

The dossier ``adversarial_review`` validator compares the reviewer's
``model_lineage`` against author lineages and FAILS CLOSED when an agent's
lineage is undeclared. agent-notes agent actors must therefore be able to
declare a family-level lineage, sourced from environment (trust-rooted, never
prompt input) — this is the agent-notes side of G1 attribution for the shared
work-item universe.
"""

from __future__ import annotations

from agent_notes.core.actor import Actor, load_actor_config, resolve_actor


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
