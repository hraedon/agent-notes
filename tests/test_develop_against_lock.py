"""Develop-against-lock enforcement (Plan 019 B2).

SUITE.lock is the single source of truth for *what to develop against*. These
tests are the mechanical control (the enforcement layer, per Plan 019 §2): they
fail if CI is wired to install the regista spine from a hardcoded version pin or
a sibling ``@main`` instead of the locked release — the exact drift that put a
stale ``regista-hraedon==0.5.1`` in CI while SUITE.lock pinned 0.5.3, and the
develop-against-``main`` skew that broke interop on 2026-07-21.

Pure unit tests — no DB, no network.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _ROOT / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import suite_lock  # noqa: E402  (script helper, resolved via the sys.path insert)

_CI = _ROOT / ".github" / "workflows" / "ci.yml"
_LOCK = _ROOT / "SUITE.lock"


def _lock() -> dict:
    return tomllib.loads(_LOCK.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The resolver: default is the locked release; the hatch is explicit.
# ---------------------------------------------------------------------------


def test_locked_version_is_read_from_suite_lock():
    assert suite_lock.regista_version() == _lock()["spine"]["version"]


def test_default_requirement_pins_the_locked_release(monkeypatch):
    """Unset DEV_AGAINST -> install regista-hraedon at the SUITE.lock version."""
    monkeypatch.delenv("DEV_AGAINST", raising=False)
    version = _lock()["spine"]["version"]
    assert suite_lock.regista_requirement() == [f"regista-hraedon=={version}"]


def test_lock_mode_is_explicit_default():
    version = _lock()["spine"]["version"]
    assert suite_lock.regista_requirement(mode="lock") == [f"regista-hraedon=={version}"]


def test_git_ref_hatch_installs_from_that_ref():
    assert suite_lock.regista_requirement(mode="main") == [
        "regista-hraedon @ git+https://github.com/hraedon/regista.git@main"
    ]
    # An arbitrary ref (branch/tag/SHA) is honored, not just "main".
    assert suite_lock.regista_requirement(mode="feature/x") == [
        "regista-hraedon @ git+https://github.com/hraedon/regista.git@feature/x"
    ]


def test_env_selects_mode(monkeypatch):
    monkeypatch.setenv("DEV_AGAINST", "main")
    assert suite_lock.dev_against() == "main"
    assert suite_lock.regista_requirement()[0].startswith("regista-hraedon @ git+")


# ---------------------------------------------------------------------------
# The SUITE.lock face-local copy stays internally coherent + agrees with the
# umbrella (checked here as: it records a released version + the tag's SHA).
# ---------------------------------------------------------------------------


def test_suite_lock_records_released_spine_version():
    spine = _lock()["spine"]
    assert spine["distribution"] == "regista-hraedon"
    # A released, PEP 440-ish version (not a "-rc"/"-dev" pre-release to develop against).
    assert re.fullmatch(r"\d+\.\d+\.\d+", spine["version"]), spine["version"]
    # 40-hex commit the version was cut from (== umbrella [components.regista].revision).
    assert re.fullmatch(r"[0-9a-f]{40}", spine["sha"]), spine["sha"]
    assert spine["describe"] == f"v{spine['version']}"


# ---------------------------------------------------------------------------
# CI is wired through the paved installer — no hardcoded pin, no unguarded @main.
# This is the control that would have caught the 0.5.1-vs-0.5.3 drift.
# ---------------------------------------------------------------------------


def test_ci_uses_dev_install_for_both_lanes():
    ci = _CI.read_text(encoding="utf-8")
    # Every lane that installs deps does it through the paved installer. The two
    # test lanes (Linux `check` + `windows-test`) both invoke it.
    assert ci.count("scripts/dev-install.py") >= 2, (
        "both the Linux and Windows test lanes must install via "
        "scripts/dev-install.py (develop-against-lock, Plan 019 B2)"
    )


def test_ci_carries_no_hardcoded_regista_pin():
    ci = _CI.read_text(encoding="utf-8")
    # A literal `regista-hraedon==<digit>` in CI is the anti-pattern this plan
    # kills: it drifts from SUITE.lock silently. The version must come from the
    # lock. (An illustrative `==<[spine].version>` in a comment is not a pin.)
    hardcoded = re.findall(r"regista-hraedon==\s*\d[\w.]*", ci)
    assert not hardcoded, (
        f"CI hardcodes a regista version {hardcoded} — it drifts from SUITE.lock. "
        "Install via scripts/dev-install.py, which reads [spine].version."
    )


def test_ci_carries_no_unguarded_sibling_install():
    ci = _CI.read_text(encoding="utf-8")
    # A raw git+ install of the spine in CI would be developing against @main
    # without the DEV_AGAINST hatch. The hatch is an env var on dev-install, not
    # a raw pip line, so the git URL must not appear in the workflow text.
    assert "git+https://github.com/hraedon/regista" not in ci, (
        "CI must not install regista from git directly; use DEV_AGAINST on "
        "scripts/dev-install.py for deliberate cross-member work."
    )


def test_dev_install_has_no_version_literal():
    """The installer resolves the version from the lock, never hardcodes it."""
    src = (_SCRIPTS / "dev-install.py").read_text(encoding="utf-8")
    assert "regista-hraedon==" not in src


# ---------------------------------------------------------------------------
# v6 tripwires (WI-072) — keep the regista substrate below 0.6 until the port.
#
# regista 0.6.0 refuses `on_behalf_of` inside a v6 epoch
# (`on_behalf_of_has_no_v6_field`) and refuses legacy writes on both sides of
# genesis. agent-notes still passes `on_behalf_of` in src (regista_face.py,
# actor.py), so a 0.6.0 substrate breaks writes at run time with confusing
# errors instead of failing where the version is chosen.
#
# pyproject's `regista-hraedon>=0.5.1,<0.6` cap covers the published metadata
# and the pip path (scripts/dev-install.py, i.e. CI). It does NOT cover the
# `[tool.uv.sources]` editable ../regista mapping: uv ignores version
# specifiers on path/editable sources, so `uv lock` silently records whatever
# the sibling checkout is (measured: it advanced 0.5.4 -> 0.6.0 with the cap in
# place, and `uv lock --check` then passed clean). These tests are the loud
# failure for that path, and for an accidental SUITE.lock advance.
#
# Retire this whole section together with the pyproject cap as part of the v6
# port ([D] phase) — not by muting it.
# ---------------------------------------------------------------------------

_V6 = (0, 6)


def _release(version: str) -> tuple[int, int]:
    """Leading (major, minor) of a PEP 440-ish version string."""
    parts = re.match(r"(\d+)\.(\d+)", version)
    assert parts, f"unparseable version: {version!r}"
    return (int(parts.group(1)), int(parts.group(2)))


def test_pyproject_caps_regista_below_v6():
    """The published floor carries an explicit <0.6 upper bound."""
    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    spec = next(
        d for d in pyproject["project"]["dependencies"] if d.startswith("regista-hraedon")
    )
    assert "<0.6" in spec, (
        f"regista dependency {spec!r} lost its <0.6 cap. regista 0.6.0 refuses "
        "on_behalf_of in a v6 epoch and agent-notes still uses it — restore the "
        "cap, or remove this test as part of the v6 port (WI-072)."
    )


def test_suite_lock_spine_is_below_v6():
    """The substrate CI installs (SUITE.lock [spine].version) stays pre-v6."""
    version = _lock()["spine"]["version"]
    assert _release(version) < _V6, (
        f"SUITE.lock pins regista {version}, which is >=0.6. dev-install.py "
        "installs exactly this version, so CI and local dev would both be on a "
        "substrate that refuses agent-notes' on_behalf_of writes. Complete the "
        "v6 port (WI-072) before advancing the spine."
    )


def test_uv_lock_records_regista_below_v6():
    """The committed uv.lock stays pre-v6.

    This is the tripwire for the editable-source hole: `uv lock` resolves
    ../regista by path and ignores the pyproject cap, so a routine re-lock on a
    machine whose sibling checkout has moved to 0.6.0 silently rewrites this
    entry. `uv run` (make test / make lint) syncs through the same lock, so a
    0.6.0 entry here means local writes are already broken.
    """
    lock_path = _ROOT / "uv.lock"
    packages = tomllib.loads(lock_path.read_text(encoding="utf-8"))["package"]
    entry = next(p for p in packages if p["name"] == "regista-hraedon")
    version = entry["version"]
    assert _release(version) < _V6, (
        f"uv.lock records regista-hraedon {version} (>=0.6), source "
        f"{entry.get('source')}. uv ignores the pyproject <0.6 cap for the "
        "editable ../regista path source, so this most likely came from a "
        "`uv lock` / `uv run` against a sibling checkout that has moved to v6. "
        "Point ../regista at a 0.5.x revision and re-lock, or use "
        "DEV_AGAINST=lock (scripts/dev-install.py) to develop against the "
        "released spine — or complete the v6 port (WI-072) and retire this test."
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
