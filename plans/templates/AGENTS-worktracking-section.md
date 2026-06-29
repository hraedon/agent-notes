<!--
Canonical "Work tracking" section for each project's AGENTS.md (agent-notes
Plan 012 WI-3). Copy the block below (between the markers) into the repo's
AGENTS.md, replacing any older breadcrumb/file-based instructions. It is
project-agnostic — the CLI resolves the current project from the path.
-->

<!-- BEGIN: work-tracking section -->
## Work tracking (issues)

Work-items for this project live in **regista** — the single source of truth.
regista is the authoritative, signed, hash-chained event log; the local
agent-notes store is a read projection of it. **Do not create physical
breadcrumb files** (`breadcrumbs/`, `OPEN_BREADCRUMBS.txt`, `*.breadcrumb.md`) —
those are retired.

**Agent face — the `agent-notes` CLI (and the `/file-breadcrumb` etc. skills).**
Run from the project root so `--path .` resolves this project; the CLI routes to
this project's regista schema automatically (you never set the schema).

```
# File an issue
agent-notes breadcrumb file --path . --title "<short title>" \
    --type <kind> [--severity low|medium|high|critical] [--body "<details>"]

# Find / show / update
agent-notes breadcrumb find  --path . [--status open] [--type bug] [--text "<q>"]
agent-notes breadcrumb get   --path . <WI-id>
agent-notes breadcrumb update --path . <WI-id> [--status <state>] [--title ...] [--body ...]
```

- **`--type` (kind):** todo, observation, decision, risk, task, bug, feature,
  improvement, question, experiment, spike, refactor, docs, ci, job.
- **`--severity`:** low, medium, high, critical.

**Lifecycle (canonical workflow):**
`open → in_progress → (blocked | deferred) → in_review → in_human_review → done`.
`done` is reachable only through the two-stage review gate (a cross-lineage
adversarial-review pass, then accept), except a pre-work `close_from_open`
dismissal (won't-fix / duplicate). "Who's working this" is a regista **claim**
(a separate liveness axis), not a lifecycle state.

**Human face:** dossier — the web window onto these same items (when deployed).
<!-- END: work-tracking section -->
