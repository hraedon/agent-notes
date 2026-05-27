# skills/opencode/ — placeholder directory

This directory used to be reserved for an opencode-specific mirror of the
skills tree. As of 2026-05-27 that approach was rejected in favor of a
single source of truth: `agent-notes install-skills --target opencode`
reads the same `skills/<name>/SKILL.md` files used by Claude Code, strips
the `name:` line from YAML frontmatter (opencode derives name from
filename), and writes to `~/.config/opencode/command/<name>.md`.

See Plan 004 §9 Q4 and `src/agent_notes/cli/skills.py` for the
implementation.

This directory is intentionally retained (and skipped by skill discovery)
so the rejection rationale lives next to where future readers will look.
If a skill's prose must diverge across harnesses, add a sibling override
file at `skills/<name>/SKILL.opencode.md` rather than a parallel tree.
