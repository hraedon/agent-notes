# opencode skill mirror — deferred

Plan 004 §6 anticipates an opencode-specific tree mirroring `skills/`
with opencode's frontmatter and invocation conventions. Per Plan 004
**open question Q4**, opencode's skill loading format is not yet
settled — building a parallel tree today risks codifying the wrong
shape.

**What we know:**

- Claude Code skills live at `~/.claude/skills/<name>/SKILL.md` with
  YAML frontmatter (`name`, `description`).
- opencode has hooks (`session.prompt`) and tool-interception primitives,
  but a per-skill convention analogous to Claude Code's `SKILL.md` is
  not yet documented in the form this repo's `install-skills` command
  could target programmatically.

**What's deferred:**

- Authoring opencode versions of `file-breadcrumb`, `update-breadcrumb`,
  `find-breadcrumb`, `add-memory`, `start`, `reflect`, `end`.
- Implementing `agent-notes install-skills --target opencode` (currently
  exits with code 3 and a "not yet implemented" message).

**Unblocker:** confirm opencode's skill format (directory layout,
manifest, invocation surface). When that's known, mirror the
`SKILL.md` prose into this tree with adapted frontmatter — the prose
itself (the judgment) is intended to be ~identical across harnesses;
only the wrapper changes.

See `plans/004-flatten-cli-and-async-bridge.md` §9 question 4.
