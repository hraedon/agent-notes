"""Unit tests for the genesis-gate session-identity probe (WI-071).

The probe's whole value is that it describes *this* environment rather than a
fixture, so these tests drive it the only honest way: by moving the environment
the write path reads (``AGENT_NOTES_MODEL_LINEAGE``, ``AGENT_NOTES_ACTOR_ID``,
the ``suite.env`` overlays) and asserting the verdict follows.

Two things are stubbed, and only these two:

- ``agent_notes.core.actor.registry_families`` — the *installed regista's*
  closed lineage registry. Stubbing it is what makes these tests independent of
  which regista version is installed (0.5.x exports no registry; the 0.6 line
  does), which is a hard requirement: the same assertions must hold in both the
  ``SUITE.lock`` environment and a sibling-checkout environment. It is patched
  on the ``actor`` module, the single attribute both the probe and
  ``require_declared_lineage`` consult, so a stub can never make the probe
  describe a registry the write path did not use.
- ``require_declared_lineage`` / ``actor_with_overrides``, in the tests that
  exist to prove the probe *notices* a broken validator.
"""

from __future__ import annotations

import pytest

from agent_notes.core import actor as actor_module
from agent_notes.core import invariant_probe as probe
from agent_notes.core.actor import Actor, InvalidLineageError, UndeclaredLineageError

FAMILIES = frozenset({"claude-opus", "glm", "kimi"})

_IDENTITY_ENV = (
    "AGENT_NOTES_ACTOR_ID",
    "AGENT_NOTES_MODEL_LINEAGE",
    "AGENT_NOTES_PRINCIPAL_ID",
    "AGENT_NOTES_PRINCIPAL_KIND",
    "AGENT_NOTES_PRINCIPAL_DISPLAY_NAME",
    "REGISTA_PRINCIPAL_ID",
)


@pytest.fixture(autouse=True)
def clean_identity_env(monkeypatch, tmp_path):
    """Pin identity resolution to process env alone.

    The session-wide conftest sets ``AGENT_NOTES_MODEL_LINEAGE=claude-opus`` to
    stand in for a wired host; these tests need to move it, including deleting
    it. The suite.env overlays are pointed at a non-existent file so the
    operator's real per-user/system overlay cannot decide a verdict here.
    """
    for name in _IDENTITY_ENV:
        monkeypatch.delenv(name, raising=False)
    suite_env = tmp_path / "suite.env"
    monkeypatch.setenv("AGENT_SUITE_CONFIG", str(suite_env))
    monkeypatch.setenv("AGENT_SUITE_SYSTEM_CONFIG", str(suite_env))
    return suite_env


@pytest.fixture
def registry(monkeypatch):
    """Install a closed lineage registry regardless of the regista in site-packages."""

    def _install(families: frozenset[str] | None = FAMILIES):
        monkeypatch.setattr(actor_module, "registry_families", lambda: families)
        return families

    return _install


# ---------------------------------------------------------------------------
# probe_registry
# ---------------------------------------------------------------------------


def test_registry_available_when_regista_exports_families(registry):
    registry(FAMILIES)
    result = probe.probe_registry()
    assert (result.available, result.reason) == (True, "registry_available")
    assert result.family_count == 3


def test_registry_absent_when_regista_predates_the_closed_vocabulary(registry):
    registry(None)
    result = probe.probe_registry()
    assert (result.available, result.reason) == (False, "registry_absent")
    assert result.family_count is None


def test_empty_registry_counts_as_absent(registry):
    """A closed vocabulary with no members can admit nothing — not a pass."""
    registry(frozenset())
    result = probe.probe_registry()
    assert (result.available, result.reason) == (False, "registry_absent")
    assert result.family_count == 0


def test_registry_import_failure_is_its_own_reason(monkeypatch):
    """A *broken* regista must not be reported as an *absent* registry.

    ``registry_families`` deliberately re-raises an ImportError whose module is
    not ``regista`` (a failed transitive import inside a present regista). If the
    probe swallowed that into ``registry_absent`` it would describe a host where
    something is already wrong as merely old.
    """

    def _boom():
        raise ImportError("no module named 'psycopg'", name="psycopg")

    monkeypatch.setattr(actor_module, "registry_families", _boom)
    result = probe.probe_registry()
    assert (result.available, result.reason) == (False, "registry_import_error")
    assert result.error_type == "ImportError"


# ---------------------------------------------------------------------------
# probe_identity — the required check's verdict
# ---------------------------------------------------------------------------


def test_identity_resolves_for_a_wired_environment(monkeypatch, registry):
    registry()
    monkeypatch.setenv("AGENT_NOTES_MODEL_LINEAGE", "glm")
    monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "probe-host-agent")
    monkeypatch.setenv("REGISTA_PRINCIPAL_ID", "principal@example.invalid")

    result = probe.probe_identity(probe.probe_registry())
    assert (result.ok, result.reason) == (True, "resolved")
    assert result.actor_id == "probe-host-agent"
    assert result.actor_kind == "agent"
    assert result.model_lineage == "glm"
    assert result.lineage_in_registry is True
    assert result.principal_resolved is True
    assert result.error_code is None


def test_undeclared_lineage_fails_with_the_write_paths_own_error(registry):
    """The verdict is the write path's verdict — same error code, named."""
    registry()
    result = probe.probe_identity(probe.probe_registry())
    assert (result.ok, result.reason) == (False, "lineage_undeclared")
    assert result.error_code == "UNDECLARED_LINEAGE"
    assert result.error_type == "UndeclaredLineageError"
    assert result.model_lineage is None


def test_whitespace_lineage_reads_as_undeclared(monkeypatch, registry):
    registry()
    monkeypatch.setenv("AGENT_NOTES_MODEL_LINEAGE", "   ")
    result = probe.probe_identity(probe.probe_registry())
    assert (result.ok, result.reason) == (False, "lineage_undeclared")


def test_lineage_outside_the_registry_fails_named(monkeypatch, registry):
    registry()
    monkeypatch.setenv("AGENT_NOTES_MODEL_LINEAGE", "opencode-glm-5.3")
    result = probe.probe_identity(probe.probe_registry())
    assert (result.ok, result.reason) == (False, "lineage_not_in_registry")
    assert result.error_code == "INVALID_MODEL_LINEAGE"
    assert result.lineage_in_registry is False


def test_unavailable_registry_fails_rather_than_degrading(monkeypatch, registry):
    """The strict decision, pinned.

    A declared lineage with no registry to resolve it against means the check's
    claim ("a write would carry a *resolvable* identity") is unproven. Unproven
    is a failure on a gate-feeding probe, and the reason names the capability
    gap rather than blaming the host's wiring.
    """
    registry(None)
    monkeypatch.setenv("AGENT_NOTES_MODEL_LINEAGE", "glm")
    result = probe.probe_identity(probe.probe_registry())
    assert (result.ok, result.reason) == (False, "lineage_registry_unavailable")
    assert result.model_lineage == "glm"
    assert result.lineage_in_registry is None
    # The write path itself did NOT refuse — the boundary check is dormant there.
    assert result.error_code is None


def test_broken_registry_import_is_reported_as_a_registry_error(monkeypatch):
    """``require_declared_lineage`` raises the same ImportError; name the cause."""

    def _boom():
        raise ImportError("broken regista", name="regista_internal")

    monkeypatch.setattr(actor_module, "registry_families", _boom)
    monkeypatch.setenv("AGENT_NOTES_MODEL_LINEAGE", "glm")
    result = probe.probe_identity(probe.probe_registry())
    assert (result.ok, result.reason) == (False, "lineage_registry_error")


def test_blank_actor_id_is_not_a_resolvable_identity(monkeypatch, registry):
    """Stricter than the write path, deliberately.

    ``require_declared_lineage`` never inspects ``actor_id``, so a whitespace
    ``AGENT_NOTES_ACTOR_ID`` is accepted by a real write and stamped as the
    author. The probe refuses to call that resolvable.
    """
    registry()
    monkeypatch.setenv("AGENT_NOTES_MODEL_LINEAGE", "glm")
    monkeypatch.setenv("AGENT_NOTES_ACTOR_ID", "   ")
    result = probe.probe_identity(probe.probe_registry())
    assert (result.ok, result.reason) == (False, "no_actor_resolvable")


def test_non_agent_actor_kind_fails(monkeypatch, registry):
    """The gate reads agent-kind events; anything else means the path changed."""
    registry()
    monkeypatch.setenv("AGENT_NOTES_MODEL_LINEAGE", "glm")
    from agent_notes.core import face_factory

    monkeypatch.setattr(
        face_factory,
        "actor_with_overrides",
        lambda *a, **kw: Actor(actor_id="x", actor_kind="system", model_lineage="glm"),
    )
    result = probe.probe_identity(probe.probe_registry())
    assert (result.ok, result.reason) == (False, "unexpected_actor_kind")


def test_unexpected_resolution_failure_is_named_not_swallowed(monkeypatch, registry):
    registry()
    monkeypatch.setenv("AGENT_NOTES_MODEL_LINEAGE", "glm")
    from agent_notes.core import face_factory

    def _boom(*_a, **_kw):
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(face_factory, "actor_with_overrides", _boom)
    result = probe.probe_identity(probe.probe_registry())
    assert (result.ok, result.reason) == (False, "identity_resolution_error")
    assert result.error_type == "RuntimeError"
    # Best-effort facts still land, so the report can say who *would* have written.
    assert result.actor_id == "agent-notes"


def test_identity_verdict_comes_from_the_write_path_entry_point(monkeypatch, registry):
    """Parity guard: the probe must call what authored writes call.

    If the probe ever re-implemented resolution instead of calling
    ``face_factory.actor_with_overrides``, it could pass while every write
    refused (or vice versa). Pinning the call site is the cheapest way to keep
    that from drifting silently.
    """
    registry()
    monkeypatch.setenv("AGENT_NOTES_MODEL_LINEAGE", "glm")
    from agent_notes.core import face_factory

    calls: list[dict] = []
    real = face_factory.actor_with_overrides

    def _spy(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return real(*args, **kwargs)

    monkeypatch.setattr(face_factory, "actor_with_overrides", _spy)
    assert probe.probe_identity(probe.probe_registry()).ok is True
    assert len(calls) == 1
    # No per-invocation overrides: the probe measures the ambient environment.
    assert calls[0]["args"] == ()
    assert calls[0]["kwargs"] == {"operation": "invariants probe"}


# ---------------------------------------------------------------------------
# probe_refusal — the deny-case that keeps a passing report meaningful
# ---------------------------------------------------------------------------


def test_refusals_are_enforced_with_a_registry(registry):
    families = registry()
    result = probe.probe_refusal(probe.probe_registry())
    assert (result.ok, result.reason) == (True, "refusals_enforced")
    assert result.undeclared_refused is True
    assert result.whitespace_refused is True
    assert result.non_family_refused is True
    assert result.declared_family_accepted is True
    assert result.system_actor_exempt is True
    assert result.unexpected == ()
    assert probe._non_family_token(families) not in families


def test_non_family_refusal_is_not_exercised_without_a_registry(registry):
    """Not exercised is recorded as ``None`` — never assumed to have passed."""
    registry(None)
    result = probe.probe_refusal(probe.probe_registry())
    assert result.ok is True
    assert result.non_family_refused is None
    assert result.undeclared_refused is True


def test_a_fail_open_validator_is_caught(monkeypatch, registry):
    """If ``require_declared_lineage`` stopped refusing, the probe must say so."""
    registry()
    monkeypatch.setattr(actor_module, "require_declared_lineage", lambda a, op=None: a)
    result = probe.probe_refusal(probe.probe_registry())
    assert (result.ok, result.reason) == (False, "refusal_missing")
    assert result.undeclared_refused is False


def test_a_refuse_everything_validator_is_caught(monkeypatch, registry):
    """The positive control: a validator that refuses a valid lineage also fails.

    Without this, a pure deny-case would pass against a validator that rejects
    everything — the tautology the deny-case exists to avoid.
    """
    registry()

    def _always_refuse(actor, operation=None):
        raise UndeclaredLineageError(actor.actor_id, operation)

    monkeypatch.setattr(actor_module, "require_declared_lineage", _always_refuse)
    result = probe.probe_refusal(probe.probe_registry())
    assert (result.ok, result.reason) == (False, "refusal_missing")
    assert result.undeclared_refused is True
    assert result.declared_family_accepted is False


def test_wrong_exception_type_is_recorded_as_unexpected(monkeypatch, registry):
    registry()

    def _wrong(actor, operation=None):
        raise ValueError("mislabelled")

    monkeypatch.setattr(actor_module, "require_declared_lineage", _wrong)
    result = probe.probe_refusal(probe.probe_registry())
    assert result.ok is False
    assert any("ValueError" in entry for entry in result.unexpected)


def test_invalid_lineage_error_is_what_a_non_family_token_raises(registry):
    """Sanity-check the deny-case's premise against the real validator."""
    families = registry()
    with pytest.raises(InvalidLineageError):
        actor_module.require_declared_lineage(
            Actor(actor_id="p", actor_kind="agent", model_lineage=probe._non_family_token(families))
        )


# ---------------------------------------------------------------------------
# Report shape — the half of the gate contract that lives in this module
# ---------------------------------------------------------------------------


def test_report_shape_satisfies_the_gate_contract(monkeypatch, registry):
    registry()
    monkeypatch.setenv("AGENT_NOTES_MODEL_LINEAGE", "glm")
    report = probe.invariant_probe_report()

    assert report["component"] == "agent-notes"
    assert type(report["probe_version"]) is int
    assert report["probe_version"] == 1
    assert isinstance(report["ok"], bool)
    ids = [check["id"] for check in report["checks"]]
    assert probe.SESSION_IDENTITY_CHECK in ids
    assert len(set(ids)) == len(ids)
    assert all(check_id.startswith(probe.CHECK_PREFIX) for check_id in ids)
    assert all(check["status"] in {"pass", "fail"} for check in report["checks"])
    assert report["ok"] is True


def test_report_ok_is_false_when_any_check_fails(monkeypatch, registry):
    registry(None)
    monkeypatch.setenv("AGENT_NOTES_MODEL_LINEAGE", "glm")
    report = probe.invariant_probe_report()
    assert report["ok"] is False
    failed = {check["id"] for check in report["checks"] if check["status"] == "fail"}
    assert failed == {probe.SESSION_IDENTITY_CHECK, probe.LINEAGE_REGISTRY_CHECK}


def test_required_check_status_is_pass_not_measured(monkeypatch, registry):
    """The umbrella requires exactly ``pass`` for this check id.

    ``measured`` is accepted by the gate for regista's measurement check only;
    using it here would be MALFORMED whenever ``ok`` is true.
    """
    registry()
    monkeypatch.setenv("AGENT_NOTES_MODEL_LINEAGE", "glm")
    report = probe.invariant_probe_report()
    required = next(c for c in report["checks"] if c["id"] == probe.SESSION_IDENTITY_CHECK)
    assert required["status"] == "pass"


def test_report_never_carries_the_principal_identifier(monkeypatch, registry):
    """The gate report is persisted; a human's principal_id must not ride along."""
    registry()
    monkeypatch.setenv("AGENT_NOTES_MODEL_LINEAGE", "glm")
    monkeypatch.setenv("REGISTA_PRINCIPAL_ID", "someone@example.invalid")
    monkeypatch.setenv("AGENT_NOTES_PRINCIPAL_DISPLAY_NAME", "Some One")
    import json

    rendered = json.dumps(probe.invariant_probe_report())
    assert "someone@example.invalid" not in rendered
    assert "Some One" not in rendered
    evidence = probe.invariant_probe_report()["checks"][0]["evidence"]
    assert evidence["principal_resolved"] is True
    assert evidence["principal_kind"] == "human"


def test_probe_crash_still_yields_a_contract_shaped_failing_report(monkeypatch, capsys):
    """A crashed probe must read as FAIL with a named reason, not MALFORMED."""

    def _boom():
        raise RuntimeError("secret-bearing: postgresql://u:p@h/db")

    monkeypatch.setattr(probe, "build_report", _boom)
    report = probe.invariant_probe_report()
    assert report["component"] == "agent-notes"
    assert report["probe_version"] == 1
    assert report["ok"] is False
    assert [c["id"] for c in report["checks"]] == [probe.SESSION_IDENTITY_CHECK]
    check = report["checks"][0]
    assert (check["status"], check["reason"]) == ("fail", "probe_error")
    # The message may carry a DSN (suite.env parsing sits on this path), so only
    # the exception *type* is allowed into the report; the traceback goes to stderr.
    import json

    assert "postgresql://" not in json.dumps(report)
    assert "RuntimeError" in capsys.readouterr().err


def test_every_reason_is_declared_and_has_a_detail_string():
    """No reason may reach a report without a human sentence, and vice versa."""
    mapped = (
        set(probe._IDENTITY_DETAIL)
        | set(probe._REGISTRY_DETAIL)
        | set(probe._REFUSAL_DETAIL)
        | {"probe_error"}
    )
    assert mapped == set(probe.REASONS)


# ---------------------------------------------------------------------------
# Read-only by construction
# ---------------------------------------------------------------------------


def test_probe_opens_no_database_connection_and_builds_no_face(monkeypatch, registry):
    """Structural proof of the read-only claim.

    Both doors to a write — the regista face and the projection DB pool — are
    booby-trapped. A probe that touched either would fail here rather than in
    production, where "read-only" is the only reason it is safe to run this from
    a scheduled timer against a live store.
    """
    registry()
    monkeypatch.setenv("AGENT_NOTES_MODEL_LINEAGE", "glm")

    from agent_notes.core import db as coredb
    from agent_notes.core import face_factory

    def _forbidden(*_a, **_kw):
        raise AssertionError("the invariant probe must not touch the store")

    monkeypatch.setattr(face_factory, "get_face", _forbidden)
    monkeypatch.setattr(face_factory, "_build_face", _forbidden)
    monkeypatch.setattr(coredb, "_conn", _forbidden)
    monkeypatch.setattr(coredb, "_get_pool", _forbidden, raising=False)

    report = probe.invariant_probe_report()
    assert report["ok"] is True
