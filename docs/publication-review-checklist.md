# Publication-Review Checklist

Before flipping `hraedon/agent-notes` from private to public (blueprint §3 —
"Public after sanitization"; Plan 017 WI-4.3), verify each item. History
scrubbing is irreversible — do it once, correctly. The public flip is the repo
owner's decision, gated on this checklist being complete.

## 0. Identifier-gate audit report (2026-07-05)

**Denylist sources:** samples from `adcs-lens`, `gpo-lens`, and the existing
`gpo-lens` denylist. The denylist was expanded per the **adcs-lens WI-010
lesson**: the scrub must cover **all identifier forms** — CA common name (CN),
CA hostname, NetBIOS domain name, real domain SID, certificate template names,
service accounts, and personal email — not just DNS hostnames.

### Identifier forms covered

The gitignored `.identifiers-denylist.local` lists the forbidden identifiers by
form. The actual identifiers are **not hardcoded here** (adcs-lens WI-010
lesson: the checklist must not itself be a leak vector). Re-verify before the
flip by running the identifier gate with the denylist.

| Form | Count | Source |
|------|-------|--------|
| AD domain (DNS form) | 1 | gpo-lens denylist, adcs-lens samples |
| AD domain (NetBIOS form) | 1 | gpo-lens samples (group-members.json) |
| Lab domain | 2 | gpo-lens denylist |
| Internal hostnames | 5 | gpo-lens denylist |
| CA hostname | 1 | adcs-lens samples (ca-config.json, collector-manifest.json) |
| **CA common name (CN)** | 1 | adcs-lens samples — **the WI-010 leak form** |
| Real domain SID | 1 | adcs-lens/gpo-lens samples |
| Certificate template name | 1 | adcs-lens samples (templates.json) |
| Service account | 1 | gpo-lens denylist |
| Personal email | 1 | gpo-lens denylist |

### Current tracked tree scan

`AGENT_NOTES_FORBIDDEN_IDENTIFIERS="$(cat .identifiers-denylist.local)"
python3 scripts/check_committed_identifiers.py` → **exit 0 (clean)**. No
forbidden identifiers in any tracked file.

### `git filter-repo --dry-run` audit (110 commits, bare clone)

Run on a temporary bare clone with `--replace-text` and `--replace-message`
covering all identifiers in the denylist.

**Blob content leaks found: 1**

| Commit | File | Identifier form | Replacement |
|--------|------|----------------|-------------|
| introduced `2747b9c`, deleted `6200f07` | `scripts/identifier-gate.py` | personal email | `REDACTED_EMAIL` |

This was the old inline denylist script (replaced by the secret-driven
`check_committed_identifiers.py`). The email appeared as a test-fixture
example. The file was deleted in `6200f07`; the leak is in history only.

**No other identifier forms** (CA CN, hostnames, domain SID, NetBIOS domain,
template names, service accounts) appear in any blob or commit message across
all 110 commits.

**Author/committer identity:** all 110 commits use the personal email (author
name: real name or short form). This must be scrubbed via `git filter-repo
--mailmap` before the public flip. The `--replace-text` flag does not touch
author/committer identity.

### Remediation required before public flip (destructive — owner-gated)

1. **`git filter-repo --replace-text`** to scrub the one blob-content leak
   (personal email in the deleted `scripts/identifier-gate.py`).
2. **`git filter-repo --mailmap`** to replace author/committer identity
   (personal email → public identity) across all 110 commits.
3. **Delete and recreate** the GitHub repository from the sanitized history
   (the adcs-lens WI-010 remedy: pushed refs are immutable; `refs/pull/*`
   snapshots retain pre-scrub commits).

**These steps are NOT performed here** — dry-run + report only, per the task
scope. The owner executes the scrub + flip when ready.

---

## 1. Identifier scrub

The gate borrows the gpo-lens pattern: the denylist is **never committed**. It is
supplied via the `AGENT_NOTES_FORBIDDEN_IDENTIFIERS` env var — from the
gitignored `.identifiers-denylist.local` locally (also read by the pre-commit
hook) and the repository secret of the same name in CI. The committed script
`scripts/check_committed_identifiers.py` carries no identifiers.

- [x] `scripts/install-git-hooks.sh` has been run in this clone (activates the
      pre-commit gate — catches a leak *before* it enters history, not after push)
- [x] `.identifiers-denylist.local` exists and lists the forbidden work-domain
      and personal identifiers (expanded 2026-07-05 with CA CN, NetBIOS domain,
      domain SID, and certificate template name per the adcs-lens WI-010 lesson);
      it is gitignored
- [ ] The `AGENT_NOTES_FORBIDDEN_IDENTIFIERS` repository secret is set in CI
      (the secret must include all identifier forms from the audit report §0)
- [x] `AGENT_NOTES_FORBIDDEN_IDENTIFIERS="$(cat .identifiers-denylist.local)"
      python3 scripts/check_committed_identifiers.py` exits 0 on the full tree
- [ ] Git history rewritten via `git filter-repo` to scrub author/committer
      identity and the one blob-content leak (personal email in the deleted
      `scripts/identifier-gate.py`) — **dry-run only; not executed (owner-gated)**
- [ ] No work-domain email in git log (`git log --format='%ae %ce' | sort -u`)
      — **currently the personal email across all 110 commits; requires mailmap scrub**
- [x] No internal hostnames in history (the tree currently has none; verified
      in history too — the `git filter-repo --dry-run` audit found zero
      hostname/CN/SID/template leaks in any blob or commit message)

## 2. Secrets

- [x] No API keys, tokens, or DSN passwords in tracked files or history
      (identifier-gate scan clean; `git filter-repo --dry-run` found no
      secret-pattern leaks beyond the personal email)
- [x] `~/.claude/settings.json` (which holds the live regista DSN + HMAC key
      path) is never committed (gitignored via `.claude/worktrees/` pattern;
      settings.json itself is outside the repo)
- [x] Test fixtures use placeholder credentials only; the testcontainers
      Postgres DSN is ephemeral, not a real host
- [x] No real DSNs / connection strings in code, plans, or reflections
      (identifier-gate scan clean)

## 3. Cross-project dependency hygiene

- [x] The `agent_waked` import in the wake-bridge test stays
      `pytest.importorskip`-guarded so CI is green without agent-wake installed
      (the known gotcha — a hard import there hard-failed CI before)
- [x] CI installs regista from its public git URL, pinned in `SUITE.lock`
- [x] No dependency on private/internal packages

## 4. Naming & authorship

- [x] Package name `agent-notes` consistent across `pyproject.toml`, CLI, docs
- [ ] `pyproject.toml` author is generic / the public identity, not the real name
      — **`pyproject.toml` has no `authors` field; add one with the public identity**
- [ ] LICENSE present — **no LICENSE file exists; add one before the flip**
      (bare `hraedon` / `hraedon.com` is the allowed public identity; the owner's
      personal email must not appear — the identifier gate forbids the literal
      string)
- [x] No employer-proprietary code or references

## 5. CI

- [x] `.github/workflows/ci.yml` runs ruff + pytest on 3.13/3.14
- [x] The `identifier-gate` job runs on every push and is green (exits 0 when
      the secret is unset; will enforce the expanded denylist once the secret
      is configured)
- [x] All tests pass on a clean checkout (testcontainers Docker + cached HF model)

## 6. Documentation

- [x] `README.md` / `AGENTS.md` are coherent public-facing documents, current
      with regista-as-source-of-truth status (WI-3.2 cutover doc-drift resolved
      2026-07-05)
- [x] `plans/` and `reflections/` are clean of personal identifiers and internal
      hostnames (identifier-gate scan clean; `git filter-repo --dry-run` found
      no leaks in plans/reflections)
- [x] Cross-project references (regista, dossier, cairn, agent-wake) use public
      URLs, not local paths
