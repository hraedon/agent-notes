# Spike: retiring regista write-through (Plan 025 Phase 1)

**Date:** 2026-10-28/29. **Branch:** `work/025-p1-native-authority`.
**Scope:** whether production can switch every machine to native Postgres as the
only work-item authority, and the exact cutover steps. Every production query
below was read-only (`default_transaction_read_only=on`). Nothing was written
to `agent_notes` or `regista`.

## Verdict

**Safe to cut over once this branch is deployed. Not safe with the code
currently installed.** Nothing is waiting to be drained: there is no outbox, no
`pending_sync` row, and no live claim. Regista holds no agent-notes state that
the native tables lack, apart from 21 never-mirrored breadcrumbs (11 of them
real) and 2 July reflections. The danger is in the op-log, not regista. Only 98
of 2,280 `work_items` rows can be rebuilt from `op_log`. With the old code, any
native write on the other 2,182 rows either fails or silently reverts the row.
This branch seals those rows into the op-log (lazily on write, or in bulk with
the backfill script), so the flip itself loses nothing.

## How the native tables were kept in sync (code)

- **Work items, regista path** (`core/work_item/_regista.py`). Each mutation
  goes to regista first. The returned snapshot is then upserted into
  `work_items` through `projection.mirror_from_regista` (title, body blob,
  status, severity, kind, refs, `regista_work_item_id`), and a `change_log` row
  is written. **No `op_log` op is written.** For a regista-created item,
  `entity_id` is a synthetic hash of `{identifier, regista_work_item_id}`, and
  no op carries it. For an item that was filed natively and later migrated,
  the op chain stops at the migration, and later regista transitions exist
  only in the cache.
- **Claims** went to `regista.claims` and were mirrored into
  `work_item_leases`.
- **Notes** (`core/note_model.py`). The regista note event is written first,
  then the local `memories` row, which is always written (`regista_note_id` is
  the link). Memories therefore never depended on regista for reads.
- **Outbox** (`core/outbox.py`, `core/reconcile.py`). It is only active when
  `AGENT_NOTES_OUTBOX=1`. Ops that couldn't reach regista were queued as
  signed JSONL under `platformdirs.user_state_dir("regista")/outbox`
  (`~/.local/state/regista/outbox`), and rows were flagged `pending_sync`.
- **Rebuild** (`projection.rebuild_from_regista`, P3). It re-mirrors from
  regista into the cache and writes no ops.
- **Native fold** (`kernel.fold_work_item`). Every native mutation commits ops
  and then refolds the entity's whole chain into the cache. With no `create`
  op the fold returns `None` and the mutation raises `RuntimeError`. With a
  stale chain the fold overwrites the cache with the pre-regista state.

## Production findings

### Work items: native cache vs regista

| Measure | Count |
|---|---|
| `work_items` rows (29 projects) | 2,280 |
| with `regista_work_item_id` | 2,276; every one exists in regista |
| field mismatches, cache vs regista `work_items_current` | 14 (below) |
| `pending_sync = true` | 0 |
| live regista claims / unexpired local leases | 0 / 0 |
| latest agent-notes-originated regista event | 2026-10-26 05:14 UTC (adcs_lens WI-051, `codex`) |
| latest `op_log` write / latest `change_log` | 2026-10-24 07:43 / 2026-10-26 05:14 |

The 14 mismatches:

- **13 local rows are newer than regista.** The cache is `closed` and regista is
  still `in_review` or `open`. These are vitrine WI-004, 022, 023, 024, 031
  (local 2026-10-12) and adcs-lens WI-024, 025, 027, 029, 031, 033, 034, 036
  (local 2026-07-14). They are native force-closes made while those writers
  had write-through off. **Native wins after the cutover; nothing to do.**
- **1 regista row is newer.** sf2 BC-001 "Dead error codes" is `done` in
  regista (2026-06-30) and `open` in the cache (2026-06-29). It needs a
  one-off fix (runbook step 7).

Per project, the latest local `updated_at` matches the latest regista
`last_event_at` to within about a second. The exceptions are sf2 (BC-001,
above) and substrate, where one regista event lands two minutes later with no
field change.

**Regista breadcrumbs never mirrored locally** (`workflow_name='canonical'`,
`work_item_type='breadcrumb'`, no local row with that regista id): 21. That
excludes cairn's 347,621 `cairn_agent_actions` tool-call items in the
`agent_provenance` schema, which are not agent-notes data.

| Project | Items | Notes |
|---|---|---|
| acme-adcs-ra | WI-011, WI-013, WI-031–036 (open), WI-039 (done) | real; WI-031–036 created 2026-10-01 by `claude-acme-drive-20261001` |
| agent-suite | WI-065 (open) | real |
| regista | WI-329 (open) | real |
| regista | WI-343 "TESTPROBE delete me" | probe |
| agent-notes | EMB-01, CHK-01, GO-LIVE-TEST | probes from the 2026-06-30 go-live |
| agent-wake | WI-012 "test-title-probe" | probe |
| windows-evidence-lab | WI-002 "probe-json-shape" | probe |
| agent-notes | BC-025; cert-watch BC-153 | migration duplicates; the identifier is taken locally by a different item |
| ad_steward (no local project) | 2 items with no source identifier | out of scope |

None of the real items' identifiers are taken locally, so they can be imported
under their own identifiers (runbook step 3).

### Notes

582 `memories` rows, 138 with `regista_note_id`, `pending_sync` 0. Regista holds
140 note entities. Two have no local row:
`reflection-2026-07-09-glm-5-2` in dossier and in vitrine (2026-07-09). No local
memory points at a missing regista note. Memory is out of this phase's scope.
These two can be recovered with `agent-notes memory rebuild-from-regista`
before the flip, or left alone.

### Outbox and pending state

- mvmcc02: `~/.local/state/regista/` does not exist. `AGENT_NOTES_OUTBOX` is
  not set in any harness config or running daemon.
- Windows: `%LOCALAPPDATA%\regista\` holds only `keys.json`; there is no
  `outbox/`.
- `pending_sync` rows: 0.

**There is nothing to drain.**

### Does the op-log fold reproduce `work_items`?

No. Folding every row's chain with `kernel.fold_work_item_state` and comparing
it to the cache:

| Result | Rows |
|---|---|
| fold reproduces the cache | 98 |
| no usable op chain (regista-era; the fold returns nothing) | 1,408 |
| stale chain (fold differs) | 774 |

Fields that differ on the stale chains:

- `status` on 763 rows: `closed→done` 685, `open→done` 56, `claimed→in_progress` 12, `open→in_human_review` 4, `open→in_review` 2, and 4 others
- `body_hash` on 44
- `external_refs` on 5
- `title` on 2
- `severity` on 2
- `kind` on 1

Only the native-only projects (sanrio-friends, zcode) and parts of sf2,
substrate, and agent-wake fold cleanly.

### What breaks or goes stale if mvmcc02 flips `writes_enabled` today (old code)

1. **A native write on any of the 1,408 chainless rows fails.** That covers
   update, close, claim, release, and review: `fold_work_item returned None`.
   The transaction rolls back, so nothing is corrupted, but those items can't
   be edited. This already happens on the Windows workstation, where
   write-through is off.
2. **A native write on any of the 774 stale-chain rows silently reverts it.**
   For example, retitling a `done` item refolds the chain from before the
   migration and flips it back to `closed`, or even to `open` (56 rows). A
   `fold_all_work_items` recovery run would revert all 774 at once.
3. **Native `close` never completes work.** It defers to `in_review`, and
   native `accept` is blocked, so nothing can reach `done` without `--force`.
   `claim` → `close` fails outright, because the claim leaves the legacy
   `claimed` status.
4. **The flip alone wouldn't take effect on mvmcc02.**
   `~/.claude/settings.json` sets `AGENT_NOTES_REGISTA_WRITES=1` in `env`, and
   the process env overrides `config.json`. Both installed harness manifests
   (`~/.claude`, `~/.config/opencode`) have `env_keys: []`, so these keys were
   written by hand and `install-harness` will not remove them.
5. **Noise.** Per-repo Claude Stop hooks wired by `agent-notes init` run
   `agent-notes outbox reconcile`, which exits 3 when the gate is off.

The branch fixes 1–3 and 5 in code. Step 4 is a config edit in the runbook.

**Other observations (not blockers):**

- mvmcc02 `config.json` names the key `regista.hmac_key_path`, which is
  ignored. The face took its key from `REGISTA_KEY_PATH` in `suite.env` and the
  Claude env.
- The per-user `suite.env` sets `REGISTA_PRINCIPAL_ID=human-1`, which isn't in
  the canonical `kind:name` grammar. Native writes need a valid actor just as
  regista writes did. Check with `agent-notes invariants probe` (step 6).

## What the branch changes

- `core/work_item/_seal.py` seals a cache row into the op-log so the fold
  reproduces it exactly:
  - a `create` stub `{identifier, regista_work_item_id}`, whose op_id equals
    the regista-era `entity_id`, so later `parent_op_ids=[entity_id]` resolve;
  - then a `snapshot` op with the cached state.

  Every native mutation calls it first (update, close, force-close, claim,
  release, review), so the cutover is correct even before the bulk backfill.
- `scripts/backfill_native_op_log.py` runs the same seal in bulk. It is
  idempotent and dry-run by default. `--import-unmirrored` files the
  never-mirrored regista breadcrumbs natively, reading regista read-only.
- Native `close` now goes to `done` and releases the lease. Native `accept` is
  allowed. The review workflow is explicit.
- `install-harness` no longer propagates the regista keys or the write gate,
  and removes them where its manifest shows it wrote them.
- orient's `regista_sync` section, the Codex Stop reconcile, and the OpenCode
  compaction sync block are removed. `init` stops wiring the Claude
  `outbox reconcile` Stop hook and removes the existing one. `outbox reconcile`
  exits 0 when the gate is off.

## Cutover runbook (coordinator)

Run on mvmcc02 as `itadmin` unless noted. `PY` is the agent-notes tool
interpreter: `PY=~/.local/share/uv/tools/agent-notes-hraedon/bin/python`.

**0. Deploy the branch first** to mvmcc02 and the Windows workstation
(reinstall the uv tool from the merged checkout). Do not flip the gate on the
old code.

**1. Pre-flight checks (read-only; expect zeros).**

```bash
agent-notes outbox status --json            # expect no project with pending > 0
ls ~/.local/state/regista/outbox 2>/dev/null # expect nothing
psql "$AGENT_NOTES_DSN" -c "SELECT count(*) FROM work_items WHERE pending_sync"   # 0
psql "$AGENT_NOTES_DSN" -c "SELECT count(*) FROM work_item_leases WHERE expires_at > now()"  # note it
```

If anything is pending, run `agent-notes outbox reconcile --project <schema>`
while write-through is still on, and resolve any conflicts before continuing.

**2. The two missing reflections.** Leave them for the memory work (Plan 025
Phase 2) to decide on. `agent-notes memory rebuild-from-regista` needs the gate
on and re-mirrors whole projects, so don't run it as part of this cutover.

**3. Import the never-mirrored breadcrumbs.** This only reads regista; the gate
can be on or off. First do a dry run and check that the list matches the table
above:

```bash
$PY -m agent_notes.scripts.backfill_native_op_log --import-unmirrored \
  --skip EMB-01 --skip CHK-01 --skip GO-LIVE-TEST \
  --skip WI-343 --skip WI-012 --skip WI-002
```

`--skip` matches a regista UUID or a source identifier, and it only ever
applies to never-mirrored items. In today's set, each of those identifiers
names exactly one probe. BC-025 and BC-153 are skipped automatically as
`skip_identifier_in_use`. ad_steward has no local project, so it is not
considered. Then re-run with `--apply`. Expect 11 `imported` (acme-adcs-ra 9,
agent-suite 1, regista 1). Imported items get no embedding.

**4. Flip the gate (mvmcc02).**

- `~/.config/agent-notes/config.json`: set `regista.writes_enabled` to `false`.
  Keep the block; it is inert without the gate.
- `~/.claude/settings.json` → `env`: delete `AGENT_NOTES_REGISTA_WRITES`.
  `AGENT_NOTES_REGISTA_HMAC_KEY_PATH` and `REGISTA_KEY_PATH` are inert now and
  can go too.
- Confirm nothing else sets the gate:
  `grep -rl AGENT_NOTES_REGISTA_WRITES ~/.claude/settings.json ~/.config ~/.hermes ~/.codex/config.toml 2>/dev/null`.
  Expect no output.
- Restart long-lived processes so they drop the old env: the OpenCode server,
  `agent-notes-bridge` if it is running, and any open Claude/Codex sessions.
  Paseo-hosted agents inherit at spawn, so restart those agents.
- Windows is already `writes_enabled: false` with no Claude env gate. No change
  is needed there.

**5. Seal the op-log.**

```bash
$PY -m agent_notes.scripts.backfill_native_op_log            # dry run
$PY -m agent_notes.scripts.backfill_native_op_log --apply
$PY -m agent_notes.scripts.backfill_native_op_log            # expect every row in_sync
```

Expect about 2,182 rows sealed (1,408 `no_op_chain` and 774
`stale_op_chain`) and 0 failures. The script exits 1 if any row fails. The op
volume is about 3,600 ops, with embeddings only on chainless rows: tens of MB
in `op_log`.

**6. Verify.**

```bash
agent-notes verify run --check-cache --json   # 0 cache violations
agent-notes invariants probe --json           # identity resolves (see REGISTA_PRINCIPAL_ID note)
agent-notes doctor
agent-notes orient --path /projects/agent-notes --json | grep -c regista_sync   # 0
```

```sql
-- every cache row now has a create op
SELECT count(*) FROM work_items w
WHERE NOT EXISTS (SELECT 1 FROM op_log o
                  WHERE o.entity_id = w.entity_id AND o.op_type = 'create');   -- 0
-- seal ops written by this cutover
SELECT count(*) FROM op_log
WHERE op_type = 'snapshot' AND payload->>'reason' = 'plan-025-native-authority';
-- after a smoke write: native moves, regista doesn't
SELECT max(created_at) FROM op_log;          -- advances
-- (regista) SELECT max(last_event_at) FROM agent_notes.work_items_current;   -- stays put
```

Smoke test, in a scratch registered project: `work-item file`, then
`work-item claim`, then `work-item close`. Each must report `done`. Then edit a
regista-era `done` item's title and confirm it stays `done`.

**7. One-off data fix.** sf2 BC-001 is `done` in regista and `open` locally:
`agent-notes work-item close BC-001 --path /projects/sf2`, or leave it open if
the regista close was wrong.

**8. Tidy the harnesses (optional).**

- `agent-notes install-harness opencode` refreshes the OpenCode plugin (drops
  the compaction sync block).
- `agent-notes init <repo>` removes the old `outbox reconcile` Stop hook from
  a repo's `.claude/settings.json`. The hook is harmless either way, since it
  now exits 0.
- Codex hook groups are unchanged; there is no re-trust prompt.

### Rollback

- **Code:** redeploy the previous agent-notes. The seal and import ops are
  additive. The regista path never reads `op_log`, so they are harmless to it.
- **Gate:** restore `regista.writes_enabled: true` and
  `AGENT_NOTES_REGISTA_WRITES=1`, then restart the same processes. This is a
  one-way door once real native writes accumulate. Work-item writes made while
  the gate was off exist only natively, and nothing replays them into regista
  (the outbox only covers ops that were queued). Before rolling back, list
  them with
  `SELECT * FROM change_log WHERE kind='work_item' AND changed_at > '<flip time>'`
  and re-apply the ones that matter by hand. Under the restored write-through,
  the regista path mirrors regista's state over the cache on the next write to
  each item.
- **Imported items:** each has a `change_log` `filed` row with
  `imported_from_regista`. Remove an item with `agent-notes work-item delete`.
