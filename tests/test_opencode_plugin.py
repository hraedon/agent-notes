"""OpenCode 2 plugin packaging checks.

These guard the two ways this plugin has failed *silently* on OC2 — neither
of which surfaces as a test failure anywhere else:

* referencing a context key the runtime does not provide (`ctx.location`
  threw inside the `context` hook, which killed every session), and
* a package whose entrypoint OC2 cannot find (it resolves `index.js` at the
  package root and ignores `main`/`exports`, so a mis-set `main` makes the
  plugin vanish with no error at all).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "integrations" / "opencode"
ENTRYPOINT = PLUGIN_ROOT / "index.js"
MANIFEST = PLUGIN_ROOT / "package.json"


def test_entrypoint_is_index_js_at_the_package_root() -> None:
    """OC2 loads `<package>/index.js` and consults neither `main` nor `exports`."""
    assert ENTRYPOINT.is_file(), f"OC2 will silently skip this plugin: no {ENTRYPOINT}"


def test_manifest_declares_esm_and_the_v2_plugin_api() -> None:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    # Without "type": "module" the ESM entrypoint is parsed as CommonJS and the
    # package is skipped — again with no error.
    assert payload["type"] == "module"
    deps = payload.get("dependencies", {})
    assert "@opencode/plugin" in deps, "V2 plugin API; @opencode-ai/plugin is the V1 package"
    assert "@opencode-ai/plugin" not in {**deps, **payload.get("peerDependencies", {})}


def test_default_export_is_a_v2_plugin_object_not_a_v1_factory() -> None:
    """V1 default-exported a function; V2 wants `{ id, setup }`."""
    src = ENTRYPOINT.read_text(encoding="utf-8")
    assert re.search(r"^export default \{", src, re.M), "V2 expects a default-exported object"
    assert re.search(r'^\s*id: "agent-notes",', src, re.M)
    assert re.search(r"^\s*async setup\(ctx\)", src, re.M)


def test_no_reference_to_context_keys_the_runtime_does_not_provide() -> None:
    """`ctx.location` is absent at runtime; reading it threw and killed sessions.

    The session's directory comes from `ctx.session.get`, which is also more
    correct: one server serves sessions rooted in many directories.
    """
    code = "\n".join(
        line for line in ENTRYPOINT.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith(("*", "//", "/*"))
    )
    assert "ctx.location" not in code
    assert "ctx.session.get(" in code


def test_setup_returns_a_cleanup_that_disposes_its_hook_registrations() -> None:
    """Hook registrations outlive setup; a reload otherwise stacks duplicates."""
    src = ENTRYPOINT.read_text(encoding="utf-8")
    assert "contextHook.dispose()" in src
    assert "compactionHook.dispose()" in src
