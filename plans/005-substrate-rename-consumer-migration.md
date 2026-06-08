# Plan 005 — Consumer migration: substrate → regista

**Status:** COMPLETE (2026-05-28). The rename landed in commit `112446d` (sf2) and `b43ada8` (regista). All live source references have been updated; historical plans and breadcrumbs are intentionally left untouched per the "Intentionally not touched" section below.
**Scope:** agent-notes specifically. See `/projects/RENAME-substrate-to-regista.md` for the orchestration context.
**Regista refs in this repo:** 0 in live source (`src/`, `pyproject.toml`); 1 in a comment (`src/agent_notes/core/bc_files.py`).

---

## Pre-flight

- [ ] Regista has tagged `v0.4.0` with the rename complete.
- [ ] Tests pass on current main: `pytest -q tests/`.
- [ ] You're on a fresh branch: `git checkout -b rename/substrate-to-regista`.

## Steps

### 1. Inventory substrate references

```bash
grep -rln '\bsubstrate\b\|\bSUBSTRATE\b\|\bSubstrate\b' \
  --include='*.py' --include='*.md' --include='*.toml' --include='*.yaml' \
  . \
  | grep -v -E 'reflections/|breadcrumbs/|plans/00[1-4]-|\.venv/|\.git/|\.claude/worktrees/'
```

Expected hits (17 total per pre-survey): AGENTS.md, README.md, several plans (Plan 004 mentions substrate; live), CROSS-PROJECT-UNIFICATION refs.

### 2. Update dependency declarations

Search `pyproject.toml` for any direct dependency on `substrate`. Per the current pre-survey, agent-notes does *not* import substrate at runtime (it's downstream-coordinated via the agent-wake bridge); confirm this is still true. If a `substrate` package dependency is found, change to `regista` and pin to `>=0.4.0`.

### 3. Sed pass over live files

```bash
sed -i \
  -e 's/\bsubstrate\b/regista/g' \
  -e 's/\bSUBSTRATE\b/REGISTA/g' \
  -e 's/\bSubstrate\b/Regista/g' \
  $(grep -rl '\bsubstrate\b\|\bSUBSTRATE\b\|\bSubstrate\b' \
      --include='*.py' --include='*.md' --include='*.toml' --include='*.yaml' \
      . \
      | grep -v -E 'reflections/|breadcrumbs/|plans/00[1-4]-|\.venv/|\.git/|\.claude/worktrees/')
```

### 4. Hand-review substantive prose

The sed handles the literal word. Read these files after and decide whether any *framing* needs rewrite:

- `README.md` — does any sentence describe regista's role in a way that doesn't make sense post-rename?
- `AGENTS.md` — same check.
- `plans/004-flatten-cli-and-async-bridge.md` — has substrate references in §1 ("composes with substrate"), §3 (decision 56 about "Bridge does not also publish to substrate"). The sed touches these; verify they still read sensibly.

### 5. Update auto-memory cross-refs (post-final-reconciliation phase usually handles this; check during this step if memory entries seem stale)

If you find `~/.claude/projects/-projects/memory/*.md` references that the meta plan's Phase 4 didn't already catch, sed them.

### 6. Tests and commit

```bash
.venv/bin/pytest -q tests/test_cli.py
```

20+ tests should pass. If any reference "substrate" in expected strings, fix them.

```bash
git add -A
git commit -m "rename: substrate → regista (Plan 005)"
git push -u origin rename/substrate-to-regista
```

Open PR, merge to main.

## Exit criteria

- [ ] `grep -rn 'substrate\|SUBSTRATE\|Substrate' --include='*.py' --include='*.toml' src/ pyproject.toml` returns 0 hits.
- [ ] Tests green.
- [ ] PR merged.

## Intentionally not touched

- `reflections/*.md` — historical
- `breadcrumbs/active/*.md`, `breadcrumbs/resolved/*.md` — historical (BC bodies are point-in-time)
- `plans/001-003` — historical (closed plans)
- `plans/dispatch-prompts/*` — historical (build-out phase prompts)
- `.claude/worktrees/*` — stale agent worktrees

## Rollback

If something breaks after merge, `git revert <commit>` restores the pre-rename state. No DB changes here (this is a pure source-code rename for the consumer; regista's DB column rename is upstream).
