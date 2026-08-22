# Develop against the locked substrate (Plan 019 B2)

**`SUITE.lock` is the single source of truth for what to develop against.**

agent-notes is one member of a polyrepo suite held compatible by version
contracts. Its one real substrate sibling is **regista** (the spine). Feature
work on agent-notes should happen against the regista the suite *ships* — the
released version pinned in `SUITE.lock` — not against regista's `main` or an
editable checkout that has drifted ahead. Developing against `main` is how
integration skew hides until interop time: on 2026-07-21 an agent-suite smoke
suite developed against a newer agent-notes than the lock pinned, and the break
only surfaced at interop. B2 removes that failure mode by making "install the
locked substrate" the default for both local dev and CI.

## The default

```bash
make dev            # or: python scripts/dev-install.py
```

installs `regista-hraedon==<SUITE.lock [spine].version>` from PyPI (today
`0.7.0`), then `ruff` and `-e ".[test]"` (pytest, testcontainers, and the pinned
`agent-suite-conformance` kit). CI runs the **same** `scripts/dev-install.py` in
both the Linux and Windows lanes, so "works on my machine" means "works in CI".

`SUITE.lock`'s `[spine]` section is the vendored, in-repo copy of the umbrella
`agent-suite/SUITE.lock` pin — it **must agree** with that umbrella's
`[components.regista]` `version` + `revision` (the umbrella is the generated
authority). Vendoring it here means CI resolves the spine without cloning
agent-suite.

## The escape hatch — `DEV_AGAINST`

Cross-member work is not forbidden; it is channeled to one obvious switch so the
coupling is always visible:

| `DEV_AGAINST` | installs regista from | when |
| --- | --- | --- |
| *unset* / `lock` | `regista-hraedon==<locked version>` (PyPI) | **default** — feature work on agent-notes alone |
| `sibling` | `-e ../regista` (editable working tree) | local co-development of regista + agent-notes together |
| `main` | `git+…/regista.git@main` | deliberately testing against regista's tip |
| `<ref>` | `git+…/regista.git@<ref>` | a specific regista branch / tag / SHA |

```bash
DEV_AGAINST=main    python scripts/dev-install.py    # test against regista tip
DEV_AGAINST=sibling python scripts/dev-install.py    # local co-dev (canonical clone)
python scripts/suite_lock.py describe                 # what am I developing against?
python scripts/suite_lock.py requirement --dev-against main
```

> `DEV_AGAINST=sibling` resolves `../regista`, which only exists in the
> constellation clone layout (`/projects/{regista,agent-notes}`), not inside a
> `git worktree`. Use it from the canonical clone.

Note: `make test` / `uv run` still resolve regista from the editable
`[tool.uv.sources]` mapping in `pyproject.toml` — that is the `uv`-native
equivalent of `DEV_AGAINST=sibling` (develop against the local working tree).
Reach for it deliberately when co-developing the spine; `make dev` is the
default that composes against the shipped release.

## V6 port state (WI-072)

The legacy caution below is historical. The writer port now uses canonical
principal IDs, regista's process-level producer configuration, and signed
action-delegation evidence; it no longer sends legacy proxy-principal fields or
per-call model identity overrides. Workflow registration and epoch opening are
external provisioning operations.

The regista configuration surface is also breaking: use `REGISTA_DSN`,
`REGISTA_KEY_PATH`, `REGISTA_REQUIRE_SSL`, and `AGENT_NOTES_PROJECT`. The
`config.json` `regista` block names the key-set field `key_path`; retired
aliases are ignored. `AGENT_NOTES_REGISTA_WRITES` remains the write-mode gate.

`pyproject.toml` targets the published post-port spine range
(`regista-hraedon>=0.7.0,<0.8`). `SUITE.lock` pins the published 0.7.0 artifact,
its `v0.7.0` merge SHA, envelope v6, and canonical workflow v3 together.

The editable `[tool.uv.sources]` mapping is a co-development hatch: uv may
resolve the local sibling regardless of the published requirement. Treat that
as sibling testing, not evidence that the release lock has advanced.

<!-- Historical pre-port guard; retained as context for the lock transition. -->

Before the port, regista 0.6.0 refused `on_behalf_of` inside a v6 epoch
(`on_behalf_of_has_no_v6_field`) and refused legacy writes on both sides of
genesis. The pre-port agent-notes writer still passed that field, so a 0.6.x
substrate did not fail at install time — it failed at *write* time, pointing at
the epoch rather than at the dependency that moved.

The pre-port `pyproject.toml` therefore capped the spine at
`regista-hraedon>=0.5.1,<0.6`. That cap bound the published metadata and the
pip path (`scripts/dev-install.py`, and so CI), but did **not** bind
`[tool.uv.sources]`: uv ignores version specifiers on path/editable sources.
The v6 port removes that cap and targets the post-port range instead.

The pre-port tripwires in `tests/test_develop_against_lock.py` made that drift
loud instead of silent. They were retired with the cap as part of the v6 port;
the remaining tests keep the resolver tied to the face-local lock.

The current editable `uv.lock` records the sibling checkout's 0.7.0 metadata.
That remains co-development state; the published artifact and exact git
provenance are independently recorded in `SUITE.lock`.

## Enforcement

`tests/test_develop_against_lock.py` is the mechanical control: it fails if CI
hardcodes a regista version (the `0.5.1`-vs-`0.5.3` drift class) or installs the
spine from `git+…@ref` without going through the `DEV_AGAINST` hatch, and it
pins the resolver's default to `SUITE.lock`'s `[spine].version`. Convention
plus CI, not a doc sentence.

## Related

- `plans/019-…` (in agent-suite) — the coupling-tax initiative; B2 is this.
- `scripts/suite_lock.py` — the resolver (reads `SUITE.lock`).
- `scripts/check_suite_lock.py` — compares the local `../regista` checkout's HEAD
  against `[spine].sha` (a complementary local-dev drift guard).
