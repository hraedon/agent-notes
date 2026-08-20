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
`0.5.4`), then `ruff` and `-e ".[test]"` (pytest, testcontainers, and the pinned
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

## Caution: regista must stay below 0.6 until the v6 port (WI-072)

regista 0.6.0 refuses `on_behalf_of` inside a v6 epoch
(`on_behalf_of_has_no_v6_field`) and refuses legacy writes on both sides of
genesis. agent-notes still passes `on_behalf_of` in `src`, so a 0.6.x substrate
does not fail at install time — it fails at *write* time, pointing at the epoch
rather than at the dependency that moved.

`pyproject.toml` therefore caps the spine at `regista-hraedon>=0.5.1,<0.6`. That
cap binds the published metadata and the pip path (`scripts/dev-install.py`, and
so CI). It does **not** bind `[tool.uv.sources]`: uv ignores version specifiers
on path/editable sources, so `uv lock` will record whatever version `../regista`
happens to be — silently, with `uv lock --check` still passing afterwards. So
`DEV_AGAINST=sibling` and `make test` / `uv run` can put you on a v6 spine even
with the cap in place.

Three tripwires in `tests/test_develop_against_lock.py` make that loud instead of
silent — they assert the cap is present, that `SUITE.lock [spine].version` is
pre-v6, and that the committed `uv.lock` records a pre-v6 regista. If the
`uv.lock` one fires, your sibling checkout has moved to v6: point `../regista` at
a 0.5.x revision and re-lock, or fall back to the default `DEV_AGAINST=lock`.

Retire the cap and all three tripwires together as part of the v6 port ([D]
phase) — not by muting them.

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
