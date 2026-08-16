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
        "AGENT_NOTES_ACTOR_ID=hermes\nAGENT_NOTES_MODEL_LINEAGE=deepseek\n",
        encoding="utf-8",
    )
    actor = resolve_actor(load_actor_config())
    assert actor.actor_id == "hermes"
    assert actor.model_lineage == "deepseek"
    require_declared_lineage(actor)  # must not raise


def test_process_env_beats_suite_env(_clean_actor_env, monkeypatch):
    _clean_actor_env.write_text("AGENT_NOTES_MODEL_LINEAGE=from-file\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_NOTES_MODEL_LINEAGE", "glm")
    assert resolve_actor(load_actor_config()).model_lineage == "glm"


def test_explicit_override_beats_env(monkeypatch):
    """``--model-lineage`` (threaded as the override arg) wins over the env."""
    from agent_notes.core.face_factory import actor_with_overrides

    monkeypatch.setenv("AGENT_NOTES_MODEL_LINEAGE", "glm")
    assert actor_with_overrides(None, "kimi").model_lineage == "kimi"


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


# --- Closed-registry boundary validation (agent-suite WI-072 step 2) ---------


def _agent(lineage: str) -> Actor:
    return Actor(actor_id="test-agent", actor_kind="agent", model_lineage=lineage)


def test_invalid_lineage_refused_when_registry_present(monkeypatch):
    """A declared token outside regista's closed registry fails at THIS boundary.

    The named agent-notes error must fire before anything is written or queued
    for the outbox — a regista INVALID_MODEL_LINEAGE surfacing later through
    the outbox is exactly what WI-072 step 2 exists to prevent.
    """
    import regista

    from agent_notes.core.actor import InvalidLineageError

    monkeypatch.setattr(
        regista,
        "MODEL_LINEAGE_FAMILIES",
        frozenset({"glm", "kimi", "fable"}),
        raising=False,
    )
    with pytest.raises(InvalidLineageError) as exc_info:
        require_declared_lineage(_agent("glm-5.2"), operation="work-item file")

    exc = exc_info.value
    assert exc.code == "INVALID_MODEL_LINEAGE"
    assert exc.lineage == "glm-5.2"
    assert exc.allowed == ["fable", "glm", "kimi"]
    assert exc.operation == "work-item file"
    text = str(exc)
    assert "INVALID_MODEL_LINEAGE" in text
    # The remedy and the allowed set must be in the message itself.
    assert "AGENT_NOTES_MODEL_LINEAGE" in text
    assert "--model-lineage" in text
    assert "fable, glm, kimi" in text


def test_registry_member_passes_when_registry_present(monkeypatch):
    import regista

    monkeypatch.setattr(regista, "MODEL_LINEAGE_FAMILIES", frozenset({"glm"}), raising=False)
    assert require_declared_lineage(_agent("glm")).model_lineage == "glm"


def test_validation_is_dormant_without_the_registry_export(monkeypatch):
    """Pinned below the closed-vocabulary regista, free text still passes here.

    The check must activate by itself when SUITE.lock advances — and impose
    nothing before that. Safe on both sides of the lock.
    """
    import regista

    from agent_notes.core.actor import registry_families

    monkeypatch.delattr(regista, "MODEL_LINEAGE_FAMILIES", raising=False)
    assert registry_families() is None
    assert require_declared_lineage(_agent("anything-goes")).model_lineage == ("anything-goes")


def test_non_frozenset_export_reads_as_no_registry(monkeypatch):
    """A malformed export is not a registry; degrade to dormant, don't crash."""
    import regista

    from agent_notes.core.actor import registry_families

    monkeypatch.setattr(regista, "MODEL_LINEAGE_FAMILIES", ["glm"], raising=False)
    assert registry_families() is None


def test_documented_lineage_examples_are_registry_members():
    """Anti-drift: the families our error messages teach must exist upstream.

    Nothing else stops cross-repo drift behind SUITE.lock (the cairn lesson):
    if regista renames or drops a family, the remedy text in our refusals must
    fail here rather than teach operators a token that ingress will reject.
    """
    from agent_notes.core.actor import registry_families

    families = registry_families()
    if families is None:
        pytest.skip("installed regista predates the closed lineage registry")
    for example in ("claude-opus", "gpt-sol", "glm", "kimi"):
        assert example in families, (
            f"actor.py error text recommends {example!r}, absent from regista's "
            "MODEL_LINEAGE_FAMILIES"
        )


def test_padded_override_is_stripped_before_it_propagates(monkeypatch):
    """Review finding (WI-062 pass, deepseek): ``--model-lineage " kimi "``
    passed the stripped registry check but the un-stripped token propagated to
    the actor (and so to the outbox envelope and regista)."""
    from agent_notes.core.face_factory import actor_with_overrides

    assert actor_with_overrides(None, " kimi ").model_lineage == "kimi"


def test_whitespace_only_override_refuses_loudly(monkeypatch):
    """An explicit whitespace flag declares nothing and must not silently
    defer to the env value it was presumably meant to replace."""
    from agent_notes.core.face_factory import actor_with_overrides

    monkeypatch.setenv("AGENT_NOTES_MODEL_LINEAGE", "glm")
    with pytest.raises(UndeclaredLineageError):
        actor_with_overrides(None, "   ")


def test_broken_regista_install_is_not_dormant(monkeypatch):
    """Review finding (WI-062 pass, deepseek): a failed transitive import
    inside regista must re-raise, not silently deactivate the boundary check
    on the very host where something is already wrong."""
    import builtins

    from agent_notes.core.actor import registry_families

    real_import = builtins.__import__

    def broken(name, *args, **kwargs):
        if name == "regista":
            raise ImportError("broken transitive dep", name="asn1crypto")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", broken)
    with pytest.raises(ImportError):
        registry_families()


# ---------------------------------------------------------------------------
# WI-068 — the native lease/delete verbs are gated too (finding 2 of the
# WI-062 adversarial pass): 3d2552e gated the regista lease verbs but left
# their native (degrade-mode) twins writing agent-authored ops ungated.
# ---------------------------------------------------------------------------


def test_native_lease_and_delete_verbs_refuse_undeclared_lineage():
    """claim / release / heartbeat / delete refuse before touching the DB.

    The gate runs ahead of ``_conn()`` (refuse before any op is written, like
    ``file_work_item``), which is why this needs no database: an undeclared
    caller never gets that far.
    """
    from agent_notes.core.work_item import _native

    attempts = {
        "work-item claim": lambda: _native.claim_work_item(1, "WI-X", None, 300),
        "work-item release": lambda: _native.release_work_item(1, "WI-X", None),
        "work-item heartbeat": lambda: _native.heartbeat_work_item(1, "WI-X", None, 300),
        "work-item delete": lambda: _native.delete_work_item(1, "WI-X"),
    }
    for operation, attempt in attempts.items():
        with pytest.raises(UndeclaredLineageError) as exc_info:
            attempt()
        assert exc_info.value.operation == operation
