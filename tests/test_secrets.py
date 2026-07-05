"""Tests for the suite secret-backend resolution layer (Plan 017 WI-4.1).

Covers:
- Literal DSN passthrough (no regression for plaintext deployments).
- ``env:`` / ``file:`` resolution via regista's providers (always available).
- Key-manifest materialization: bare path + ``file:`` pass through; a remote
  ``env:`` ref writes a 0600 temp file that the cleanup scrubs.
- ``atexit`` safety net + ``reset_face`` scrubbing.
- DSN-with-colon-in-password still treated as literal (regression guard).
- Vault/Azure providers: skipped cleanly when their SDK is absent (mirrors
  regista's own gated pattern).
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from agent_notes.core import secrets as suite_secrets

# ---------------------------------------------------------------------------
# resolve_dsn
# ---------------------------------------------------------------------------


def test_resolve_dsn_none_and_empty_passthrough():
    assert suite_secrets.resolve_dsn(None) is None
    assert suite_secrets.resolve_dsn("") == ""


def test_resolve_dsn_literal_unchanged():
    """A literal DSN has no provider prefix → returned as-is, regista untouched."""
    dsn = "postgresql://user:pass@host:5432/agent_notes"
    assert suite_secrets.resolve_dsn(dsn) == dsn


def test_resolve_dsn_literal_with_special_chars_in_password():
    """A password containing ``@``/``:`` must not be misread as a provider ref."""
    dsn = "postgresql://user:p@ss:word@host/db"
    assert suite_secrets.resolve_dsn(dsn) == dsn


def test_resolve_dsn_env_ref(monkeypatch):
    monkeypatch.setenv("AN_TEST_DSN", "postgresql://from-env/x")
    assert suite_secrets.resolve_dsn("env:AN_TEST_DSN") == "postgresql://from-env/x"


def test_resolve_dsn_file_ref(tmp_path):
    dsn_file = tmp_path / "dsn.txt"
    dsn_file.write_text("postgresql://from-file/x")
    assert suite_secrets.resolve_dsn(f"file:{dsn_path(dsn_file)}") == "postgresql://from-file/x"


def test_resolve_dsn_env_missing_raises_runtime():
    # regista raises RegistaError(env not set); we wrap to RuntimeError, type-only msg.
    with pytest.raises(RuntimeError, match="Failed to resolve DSN secret"):
        suite_secrets.resolve_dsn("env:AN_DEFINITELY_UNSET_DSN_VAR_X9Z")


def test_resolve_dsn_failure_message_is_type_only(monkeypatch):
    """A resolution failure surfaces only the exception type, never ``str(exc)``.

    regista's EnvProvider error echoes the var name; our wrapper must drop it so
    doctor/JSON output (which may land in aggregators) never echoes a ref. The
    configured value may include a sentinel substring that must not appear.
    """
    sentinel = "vault_secret_path_sentinel_x9z"
    monkeypatch.delenv(f"AN_{sentinel}", raising=False)
    with pytest.raises(RuntimeError) as exc_info:
        suite_secrets.resolve_dsn(f"env:AN_{sentinel}")
    assert sentinel not in str(exc_info.value)


def dsn_path(p: Path) -> str:
    # On Windows a leading "/" in file:/C:/... is fine; posix wants the abs path.
    return str(p)


# ---------------------------------------------------------------------------
# materialize_key_manifest
# ---------------------------------------------------------------------------


def test_manifest_none_and_empty():
    assert suite_secrets.materialize_key_manifest(None) == (None, None)
    assert suite_secrets.materialize_key_manifest("") == (None, None)


def test_manifest_bare_path_passes_through():
    """Today's default — a filesystem path regista reads directly."""
    path, cleanup = suite_secrets.materialize_key_manifest("/etc/regista/keys.json")
    assert path == "/etc/regista/keys.json"
    assert cleanup is None


@pytest.mark.skipif(os.name != "posix", reason="tilde expansion via HOME is POSIX-only")
def test_manifest_tilde_path_is_expanded(monkeypatch):
    """``~`` is expanded because regista's KeySet does not expand it itself."""
    monkeypatch.setenv("HOME", "/home/test")
    path, cleanup = suite_secrets.materialize_key_manifest("~/.config/regista/keys.json")
    assert path == "/home/test/.config/regista/keys.json"
    assert cleanup is None


def test_manifest_file_prefix_strips_to_plain_path(tmp_path):
    kp = tmp_path / "keys.json"
    kp.write_text('{"keys": []}')
    path, cleanup = suite_secrets.materialize_key_manifest(f"file:{kp}")
    assert path == str(kp)
    assert cleanup is None


def test_manifest_env_ref_materializes_temp_file(monkeypatch):
    manifest = json.dumps({"keys": [{"key_id": "k1", "secret": "x"}]})
    monkeypatch.setenv("AN_KEY_MANIFEST", manifest)
    path, cleanup = suite_secrets.materialize_key_manifest("env:AN_KEY_MANIFEST")

    assert path is not None
    assert cleanup is not None
    assert path != "env:AN_KEY_MANIFEST"
    # The temp file holds the resolved manifest bytes.
    assert Path(path).read_text() == manifest
    # 0600 on POSIX (owner-only); on Windows chmod is a no-op so skip the bit.
    if os.name == "posix":
        mode = stat.S_IMODE(Path(path).stat().st_mode)
        assert mode == 0o600
    # Registered for atexit scrub.
    assert suite_secrets.materialized_temp_count() >= 1
    # Cleanup scrubs the file and is idempotent.
    cleanup()
    assert not Path(path).exists()
    cleanup()  # second call is a no-op, must not raise


def test_manifest_env_ref_unresolvable_raises(monkeypatch):
    monkeypatch.delenv("AN_MISSING_KEY_MANIFEST", raising=False)
    with pytest.raises(RuntimeError, match="Failed to resolve key-set manifest"):
        suite_secrets.materialize_key_manifest("env:AN_MISSING_KEY_MANIFEST")


def test_manifest_literal_refused():
    """literal: is not a readable key-set path — refuse early."""
    with pytest.raises(RuntimeError, match="literal: is not a valid key-set manifest"):
        suite_secrets.materialize_key_manifest('literal:{"keys":[]}')


def test_manifest_vault_without_sdk_fails_cleanly(monkeypatch):
    """A vault ref must fail cleanly (RuntimeError), never leak a raw exception.

    Two sub-cases, both acceptable:
    - hvac absent: regista treats ``vault:`` as literal, returns the ref string;
      our manifest-shape validation rejects it (not valid JSON) → RuntimeError.
    - hvac present but VAULT_ADDR unset: VaultProvider raises RegistaError → our
      wrapper converts to RuntimeError.
    Either way the surfaced message must not echo the vault path.
    """
    monkeypatch.delenv("VAULT_ADDR", raising=False)
    monkeypatch.delenv("VAULT_TOKEN", raising=False)
    sentinel = "secret/agent-suite/regista"
    with pytest.raises(RuntimeError) as exc_info:
        suite_secrets.materialize_key_manifest(f"vault:{sentinel}#keys")
    assert sentinel not in str(exc_info.value)


# ---------------------------------------------------------------------------
# adversarial-review regression tests (BLOCKING / MAJOR findings)
# ---------------------------------------------------------------------------


def test_resolve_dsn_vault_without_sdk_raises_not_silent(monkeypatch):
    """BLOCKING B1: a vault: DSN with hvac absent must raise, not return the ref.

    Two sub-cases, both acceptable:
    - hvac absent + regista silently falls back: the ref string is returned
      unchanged, our silent-literal-fallback guard detects it and raises
      "did not resolve".
    - hvac absent but regista raises RegistaError (newer regista versions):
      our wrapper converts to RuntimeError("Failed to resolve DSN secret").
    Either way the ref must not be returned as a usable DSN.
    """
    monkeypatch.delenv("VAULT_ADDR", raising=False)
    monkeypatch.delenv("VAULT_TOKEN", raising=False)
    with pytest.raises(RuntimeError) as exc_info:
        suite_secrets.resolve_dsn("vault:secret/agent-suite/regista#dsn")
    msg = str(exc_info.value)
    assert "did not resolve" in msg or "Failed to resolve" in msg
    assert "secret/agent-suite/regista" not in msg
    assert exc_info.value.__cause__ is None


def test_resolve_dsn_azure_without_sdk_raises_not_silent(monkeypatch):
    """BLOCKING B1 (azure variant): azure: DSN with the SDK absent must raise."""
    monkeypatch.delenv("AZURE_KEY_VAULT_NAME", raising=False)
    with pytest.raises(RuntimeError):
        suite_secrets.resolve_dsn("azure:regista-dsn")


def test_resolve_dsn_uppercase_prefix_resolves(monkeypatch):
    """MAJOR M3: ENV:VAR must resolve (prefix normalized), not silently fail."""
    monkeypatch.setenv("AN_UPPER_DSN", "postgresql://resolved/x")
    assert suite_secrets.resolve_dsn("ENV:AN_UPPER_DSN") == "postgresql://resolved/x"
    monkeypatch.setenv("AN_UPPER_DSN2", "postgresql://resolved2/x")
    assert suite_secrets.resolve_dsn("Env:AN_UPPER_DSN2") == "postgresql://resolved2/x"


def test_resolve_dsn_env_self_referential_not_flagged(monkeypatch):
    """Round-2 B2: env: must NOT trip the silent-fallback guard.

    env is always registered (no SDK), so it can never silently fall back. A
    pathological env var whose value equals its own ref string must resolve
    (return the value), not raise a bogus 'install regista[env]' error.
    """
    monkeypatch.setenv("AN_SELFREF", "env:AN_SELFREF")
    # Resolves to the (weird) literal value without raising.
    assert suite_secrets.resolve_dsn("env:AN_SELFREF") == "env:AN_SELFREF"


def test_wrapped_exception_cause_is_suppressed(monkeypatch):
    """MAJOR M1: ``from None`` keeps the original (ref-echoing) exception out of
    ``__cause__`` so a logged traceback cannot leak it."""
    monkeypatch.delenv("AN_CAUSE_LEAK_VAR", raising=False)
    with pytest.raises(RuntimeError) as exc_info:
        suite_secrets.resolve_dsn("env:AN_CAUSE_LEAK_VAR")
    # from None → __cause__ is None; __context__ may be set but traceback display
    # of an explicit `from None` does not print the context chain.
    assert exc_info.value.__cause__ is None


def test_manifest_uppercase_prefix_resolves(monkeypatch):
    """MAJOR M3 (manifest variant): ENV: ref to a manifest resolves + materializes."""
    manifest = json.dumps({"keys": []})
    monkeypatch.setenv("AN_UPPER_MANIFEST", manifest)
    path, cleanup = suite_secrets.materialize_key_manifest("ENV:AN_UPPER_MANIFEST")
    assert path is not None and cleanup is not None
    assert Path(path).read_text() == manifest
    cleanup()


def test_manifest_non_list_keys_rejected(monkeypatch):
    """MINOR: a manifest whose 'keys' is not a list is rejected at validate time."""
    monkeypatch.setenv("AN_BAD_KEYS", '{"keys": "not-a-list"}')
    with pytest.raises(RuntimeError, match="must be a JSON object with a 'keys' array"):
        suite_secrets.materialize_key_manifest("env:AN_BAD_KEYS")


def test_config_resolve_dsn_explicit_arg_routes_through_resolver(monkeypatch):
    """MAJOR M4: the explicit arg is resolved too, not returned raw."""
    from agent_notes.core import config

    monkeypatch.setenv("AN_EXPLICIT_DSN", "postgresql://explicit-resolved/x")
    assert config.resolve_dsn("env:AN_EXPLICIT_DSN") == "postgresql://explicit-resolved/x"
    # Literal explicit arg still passes through unchanged.
    assert config.resolve_dsn("postgresql://literal/x") == "postgresql://literal/x"


def test_doctor_secrets_check_fails_for_missing_file_manifest(monkeypatch, tmp_path):
    """MAJOR M6: a file: key manifest that does not exist must fail the doctor
    check, not pass silently (materialize returns the path unread)."""
    from agent_notes.scripts.doctor import _check_secrets_backend

    # Build a RegistaConfig-like with a file: ref to a nonexistent manifest.
    missing = tmp_path / "absent-keys.json"

    class _Cfg:
        dsn = None
        hmac_key_path = f"file:{missing}"

    ok, msg = _check_secrets_backend(_Cfg())
    assert ok is False
    assert "REGISTA_KEY_PATH" in msg


def test_face_reset_scrubs_real_temp_manifest(monkeypatch):
    """Real (not fake) reset_face scrub: a materialized temp file is unlinked."""
    from agent_notes.core import face_factory

    manifest = json.dumps({"keys": []})
    monkeypatch.setenv("AN_REAL_RESET_MANIFEST", manifest)
    path, cleanup = suite_secrets.materialize_key_manifest("env:AN_REAL_RESET_MANIFEST")
    assert Path(path).exists()
    face_factory._FACE_CLEANUPS["real-proj"] = cleanup
    face_factory.reset_face()
    assert not Path(path).exists()


# ---------------------------------------------------------------------------
# atexit safety net + reset_face scrubbing
# ---------------------------------------------------------------------------


def test_scrub_all_cleans_registered_temps(monkeypatch):
    monkeypatch.setenv("AN_ATEXIT_MANIFEST", '{"keys":[]}')
    before = suite_secrets.materialized_temp_count()
    path, _cleanup = suite_secrets.materialize_key_manifest("env:AN_ATEXIT_MANIFEST")
    assert Path(path).exists()
    assert suite_secrets.materialized_temp_count() == before + 1
    suite_secrets._scrub_all()
    assert not Path(path).exists()
    assert suite_secrets.materialized_temp_count() == 0


def test_face_reset_scrubs_materialized_manifest(monkeypatch, tmp_path):
    """A face built with a backend-sourced manifest must scrub it on reset_face.

    Uses an in-memory test face path indirectly: we only assert the cleanup is
    registered and invoked, without standing up a real regista connection (which
    needs Postgres). This isolates the lifecycle bookkeeping from the DB.
    """
    from agent_notes.core import face_factory

    monkeypatch.setenv("AN_FACE_RESET_MANIFEST", '{"keys":[]}')
    cleanup_called = []
    fake_cleanup = lambda: cleanup_called.append(True)  # noqa: E731
    face_factory._FACE_CLEANUPS["test-proj"] = fake_cleanup
    face_factory.reset_face()
    assert cleanup_called == [True]
    assert "test-proj" not in face_factory._FACE_CLEANUPS


# ---------------------------------------------------------------------------
# is_backend_ref predicate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, False),
        ("", False),
        ("postgresql://user:pass@host/db", False),
        ("/etc/regista/keys.json", False),
        ("~/.config/regista/keys.json", False),
        ("env:VAR", True),
        ("ENV:VAR", True),  # case-insensitive recognition (resolved via normalize)
        ("Vault:secret/x", True),
        ("vault:secret/x", True),
        ("azure:name", True),
        ("AZURE:name", True),
        ("file:/x", True),
        ("postgresql://host", False),  # scheme not a provider
        ("akv:https://v/secrets/k", False),  # akv unsupported by regista — not a ref
    ],
)
def test_is_backend_ref(value, expected):
    assert suite_secrets.is_backend_ref(value) is expected
