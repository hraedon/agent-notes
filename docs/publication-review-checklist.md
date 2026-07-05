# Publication-Review Checklist

Before flipping `hraedon/agent-notes` from private to public (blueprint §3 —
"Public after sanitization"; Plan 017 WI-4.3), verify each item. History
scrubbing is irreversible — do it once, correctly. The public flip is the repo
owner's decision, gated on this checklist being complete.

## 1. Identifier scrub

- [ ] `python3 scripts/identifier-gate.py` exits 0 (no personal/internal
      identifiers in tracked files)
- [ ] The work-domain identifier(s) are added to `IDENTIFIERS` in the gate before
      the flip (deliberately absent from the committed script so it carries no
      secret) and the gate still passes
- [ ] Git history rewritten via `git filter-repo` to scrub author/committer
      identity and any historical identifiers
- [ ] No work-domain email in git log (`git log --format='%ae %ce' | sort -u`)
- [ ] No internal hostnames in history (the tree currently has none; verify
      history too)

## 2. Secrets

- [ ] No API keys, tokens, or DSN passwords in tracked files or history
- [ ] `~/.claude/settings.json` (which holds the live regista DSN + HMAC key
      path) is never committed
- [ ] Test fixtures use placeholder credentials only; the testcontainers
      Postgres DSN is ephemeral, not a real host
- [ ] No real DSNs / connection strings in code, plans, or reflections

## 3. Cross-project dependency hygiene

- [ ] The `agent_waked` import in the wake-bridge test stays
      `pytest.importorskip`-guarded so CI is green without agent-wake installed
      (the known gotcha — a hard import there hard-failed CI before)
- [ ] CI installs regista from its public git URL, pinned in `SUITE.lock`
- [ ] No dependency on private/internal packages

## 4. Naming & authorship

- [ ] Package name `agent-notes` consistent across `pyproject.toml`, CLI, docs
- [ ] `pyproject.toml` author is generic / the public identity, not the real name
- [ ] LICENSE present (bare `hraedon` / `hraedon.com` is the allowed public
      identity; the owner's personal `plm@`-form email must not appear — the
      identifier gate forbids the literal string)
- [ ] No employer-proprietary code or references

## 5. CI

- [ ] `.github/workflows/ci.yml` runs ruff + pytest on 3.13/3.14
- [ ] The `identifier-gate` job runs on every push and is green
- [ ] All tests pass on a clean checkout (testcontainers Docker + cached HF model)

## 6. Documentation

- [ ] `README.md` / `AGENTS.md` are coherent public-facing documents, current
      with regista-as-source-of-truth status
- [ ] `plans/` and `reflections/` are clean of personal identifiers and internal
      hostnames
- [ ] Cross-project references (regista, dossier, cairn, agent-wake) use public
      URLs, not local paths
