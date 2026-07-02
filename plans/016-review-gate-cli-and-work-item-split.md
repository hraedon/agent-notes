# Plan 016 — Review-gate CLI + work_item_model split

**Status:** complete (2026-07-02)
**Scope:** surface the cross-lineage review gate on the CLI, and split the
2,189-line `work_item_model.py` into a `core/work_item/` subpackage.

## 1. Motivation

Plan 010 (canonical lifecycle convergence) and Plan 014 (degrade-mode gate
parity) shipped the *model-level* review-gate transitions
(`adversarial_pass` / `accept` / `reject` / `request_changes`) on
`WorkItemModel.review_transition`, plus the `attest-gate` operator command.
The gate was drivable only through the model API or by hand on the regista
face — there was no CLI surface, so an operator/reviewer could not actually
run a review pass from the command line.

Separately, `core/work_item_model.py` had grown to 2,189 lines: every
mutation carried two full inline implementations (regista path + native
op-log path). It was the largest module in the package and the hardest to
review or extend.

## 2. Review-gate CLI

New `work-item review` subcommand tree (Plan 010/014 surface):

```
agent-notes work-item review list                       # in_review / in_human_review queue
agent-notes work-item review pass    <id> --note <txt>  # adversarial_pass (→ in_human_review)
agent-notes work-item review accept  <id> --note <txt>  # accept (→ done; regista only — gate runs)
agent-notes work-item review reject  <id> --note <txt>  # reject (→ in_progress)
agent-notes work-item review request-changes <id> --note <txt>  # request_changes (→ in_progress)
```

Each review command accepts identity overrides so a subagent can declare its
own lineage without env-var mutation:

- `--actor-id` — override the reviewer's actor_id
- `--model-lineage` — override the reviewer's model lineage (required for the
  cross-lineage gate if the author was an agent)
- `--same-lineage-acknowledged` — explicit same-lineage review acknowledgement
  (passes through to regista's validator)

`--note` is **required** — regista's cross-lineage validators reject a
transition without a review note. On the native (degrade) path the note is
stamped into `diagnostic_keys.review_notes` for provenance; `accept` is
blocked off-regista (Plan 014 WI-2 — the gate cannot run without regista).

## 3. `reviewer_actor` helper

`face_factory.reviewer_actor(actor_id, model_lineage)` resolves an `Actor`
with per-call identity overrides. When `actor_id` is overridden, the
env-resolved principal (`on_behalf_of`) is cleared — a reviewer with a
distinct identity is its own principal, not acting on behalf of the author's
principal. Without this, the gate's separation-of-duties check would flag a
"delegated self-review."

## 4. `work_item_model.py` split

The monolith is now a thin dispatch facade (`WorkItemModel`, 518 lines) over
a `core/work_item/` subpackage of free-function implementations:

| Module | Responsibility |
|--------|----------------|
| `_common.py` | Shared helpers (workspace/vocab lookup, embedding diff, regista-snapshot mirroring, change-log payload builder). |
| `_regista.py` | Regista-face write path (converged store is SoT). |
| `_native.py` | Native op-log write path (degrade mode). |
| `_queries.py` | Read-only queries, `diagnose`, regista-path delete. |
| `_cross_project.py` | P3 `request` / `wait` / cross-project links. |

**Contract:** the public `WorkItemModel` class API and behavior are preserved
exactly. Each facade method selects the path via `face_factory.get_face()` and
delegates. Internal static/class helpers (`_mirror_regista_snapshot`,
`_validate_vocab`, …) became free functions in `_common`; no external caller
or test imported them, except one historical test that reached into
`WorkItemModel._mirror_regista_snapshot` — that test was updated to use the
explicit `mirror_regista_snapshot` helper from `_common` (removing a
private-name coupling rather than propagating it).

**Verification:** `make lint` clean; `make test` → 625 passed, 1 skipped
(unchanged from baseline). An adversarial review cross-checked the refactor
against the pre-split source; every flagged item was a faithfully-preserved
pre-existing behavior (e.g. the regista-vs-native `change_log` actor
asymmetry, native lease ops not writing change_log), not a regression.

## 5. Incidental

- `ruff format` pass collapsed several multi-line expressions across
  `cli/`, `core/lifecycle.py`, `core/verifier.py`, `web/app.py`, and tests.
- `uv.lock`: added `mypy` + `types-*` stubs to the `dev` extra (type-checking
  tooling; no runtime impact).

## 6. Not in scope

- README's `work-item` command enumeration lags several plans
  (`attest-gate`, `request`, `wait`, `add-link`). This plan documents the
  `review` surface; the broader README command-list refresh is tracked
  separately (BC-024-adjacent).
