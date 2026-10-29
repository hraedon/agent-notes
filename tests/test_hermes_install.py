"""``install-harness hermes``: the ``pre_llm_call`` shell hook in config.yaml (Plan 025 Phase 7)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from agent_notes.cli.harness import _install_harness_one, _targets_for, _uninstall_one
from agent_notes.cli.hermes_config import BACKUP_SUFFIX
from agent_notes.hooks.hermes import EVENT, HOOK_COMMAND, hermes_hook_entry


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("AGENT_NOTES_DSN", "AGENT_NOTES_PROJECT"):
        monkeypatch.delenv(var, raising=False)


def _install(home: Path, dry_run: bool = False) -> dict:
    result, _warns = _install_harness_one("hermes", dry_run, None, None, None, home=home)
    return result


def _uninstall(home: Path) -> dict:
    result, _warns = _uninstall_one("hermes", home=home)
    return result


def _config(home: Path) -> Path:
    return home / ".hermes" / "config.yaml"


def _manifest(home: Path) -> dict:
    return json.loads((home / ".hermes" / ".agent-notes-harness.json").read_text(encoding="utf-8"))


def _hooks(home: Path) -> dict:
    return yaml.safe_load(_config(home).read_text(encoding="utf-8"))["hooks"]


def _write(home: Path, text: str) -> Path:
    path = _config(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")
    return path


COMMENTED = """\
# my Hermes setup
model:
  default: x/y   # the daily driver

terminal:
  cwd: /work
"""


def test_fresh_install_creates_config_and_uninstall_removes_it(tmp_path):
    result = _install(tmp_path)
    assert result["status"] == "installed", result["warnings"]
    assert _hooks(tmp_path) == {EVENT: [hermes_hook_entry()]}
    assert _manifest(tmp_path)["hooks"].keys() == {EVENT}
    assert _manifest(tmp_path)["hooks_file_created"] is True
    action = next(a for a in result["actions"] if a["kind"] == "merge_yaml")
    assert action["status"] == "created" and action["path"] == str(_config(tmp_path))

    assert _install(tmp_path)["no_op"] is True

    removed = _uninstall(tmp_path)
    assert removed["status"] == "installed"
    assert not _config(tmp_path).exists()
    assert not (tmp_path / ".hermes" / ".agent-notes-harness.json").exists()


def test_install_and_uninstall_keep_the_comments_of_a_config_without_hooks(tmp_path):
    path = _write(tmp_path, COMMENTED)
    _install(tmp_path)
    text = path.read_text(encoding="utf-8")
    assert text.startswith(COMMENTED) and "# the daily driver" in text
    parsed = yaml.safe_load(text)
    assert parsed["terminal"] == {"cwd": "/work"}
    assert parsed["hooks"] == {EVENT: [hermes_hook_entry()]}
    assert not path.with_name(path.name + BACKUP_SUFFIX).exists()
    assert _manifest(tmp_path)["hooks_file_created"] is False

    _uninstall(tmp_path)
    assert path.read_text(encoding="utf-8") == COMMENTED


def test_an_empty_hooks_mapping_is_filled_in_place_and_comments_survive(tmp_path):
    original = "# top\nmodel: {default: x/y}\nhooks: {}\nhooks_auto_accept: false  # keep\n"
    path = _write(tmp_path, original)
    _install(tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "# top" in text and "# keep" in text
    parsed = yaml.safe_load(text)
    assert parsed["hooks"] == {EVENT: [hermes_hook_entry()]}
    assert parsed["hooks_auto_accept"] is False
    assert text.count("hooks:") == 1  # never a duplicate key
    assert not path.with_name(path.name + BACKUP_SUFFIX).exists()

    _uninstall(tmp_path)
    after = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert after == {"model": {"default": "x/y"}, "hooks_auto_accept": False}
    assert "# top" in path.read_text(encoding="utf-8")


def test_existing_hooks_are_merged_with_a_backup_and_survive_uninstall(tmp_path):
    original = """\
# comments are lost when other hooks exist
hooks:
  pre_tool_call:
    - matcher: terminal
      command: ~/.hermes/agent-hooks/block.sh
  pre_llm_call:
    - command: ~/.hermes/agent-hooks/inject.sh
      timeout: 5
model: x/y
"""
    path = _write(tmp_path, original)
    result = _install(tmp_path)
    assert result["status"] == "installed"
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    assert backup.read_text(encoding="utf-8") == original
    assert any("not preserved" in w or "without its comments" in w for w in result["warnings"])
    hooks = _hooks(tmp_path)
    assert hooks["pre_tool_call"] == [
        {
            "matcher": "terminal",
            "command": "~/.hermes/agent-hooks/block.sh",
        }
    ]
    assert hooks[EVENT] == [
        {"command": "~/.hermes/agent-hooks/inject.sh", "timeout": 5},
        hermes_hook_entry(),
    ]
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["model"] == "x/y"

    assert _install(tmp_path)["no_op"] is True
    _uninstall(tmp_path)
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert parsed == yaml.safe_load(original)
    assert backup.read_text(encoding="utf-8") == original  # the first backup is never replaced


def test_a_foreign_entry_with_our_command_is_neither_adopted_nor_removed(tmp_path):
    foreign = {"hooks": {EVENT: [{"command": HOOK_COMMAND, "timeout": 99}]}}
    path = _write(tmp_path, yaml.safe_dump(foreign))
    before = path.read_text(encoding="utf-8")
    result = _install(tmp_path)
    assert result["status"] == "failed"
    assert path.read_text(encoding="utf-8") == before
    _uninstall(tmp_path)
    assert path.read_text(encoding="utf-8") == before


def test_a_locally_modified_owned_entry_is_preserved(tmp_path):
    _install(tmp_path)
    path = _config(tmp_path)
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["hooks"][EVENT][0]["timeout"] = 99
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")

    assert _install(tmp_path)["status"] == "failed"
    assert _hooks(tmp_path)[EVENT][0]["timeout"] == 99
    removed = _uninstall(tmp_path)
    assert removed["status"] == "failed"
    assert _hooks(tmp_path)[EVENT][0]["timeout"] == 99
    assert _manifest(tmp_path)["hooks"].keys() == {EVENT}


def test_an_owned_entry_from_an_older_version_is_updated(tmp_path):
    _install(tmp_path)
    path = _config(tmp_path)
    old = {"command": HOOK_COMMAND, "timeout": 7}
    doc = {"hooks": {EVENT: [old]}}
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    from agent_notes.cli.harness import _json_hash

    manifest_path = tmp_path / ".hermes" / ".agent-notes-harness.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["hooks"][EVENT] = _json_hash(old)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = _install(tmp_path)
    assert result["status"] == "installed"
    assert _hooks(tmp_path)[EVENT] == [hermes_hook_entry()]


def test_dry_run_writes_nothing(tmp_path):
    result = _install(tmp_path, dry_run=True)
    action = next(a for a in result["actions"] if a["kind"] == "merge_yaml")
    assert action["status"] == "created"
    assert not _config(tmp_path).exists()
    assert not (tmp_path / ".hermes" / ".agent-notes-harness.json").exists()

    path = _write(tmp_path, "hooks:\n  pre_tool_call:\n    - command: x\n")
    before = path.read_text(encoding="utf-8")
    result = _install(tmp_path, dry_run=True)
    assert path.read_text(encoding="utf-8") == before
    assert not path.with_name(path.name + BACKUP_SUFFIX).exists()
    assert any("would be rewritten" in w for w in result["warnings"])


def test_unparseable_or_oddly_shaped_config_is_refused(tmp_path):
    path = _write(tmp_path, "hooks: [unterminated\n")
    with pytest.raises(RuntimeError, match="Cannot parse"):
        _install(tmp_path)
    path.write_text("hooks: nope\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="not a mapping"):
        _install(tmp_path)
    path.write_text("hooks:\n  pre_llm_call: nope\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="not a list"):
        _install(tmp_path)
    path.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="not a YAML mapping"):
        _install(tmp_path)


def test_a_non_utf8_config_is_refused_untouched(tmp_path):
    path = _config(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"model: caf\xe9\n")
    with pytest.raises(RuntimeError, match="not UTF-8"):
        _install(tmp_path)
    assert path.read_bytes() == b"model: caf\xe9\n"


def test_crlf_line_endings_are_kept(tmp_path):
    original = COMMENTED.replace("\n", "\r\n")
    path = _write(tmp_path, original)
    _install(tmp_path)
    raw = path.read_bytes().decode("utf-8")
    assert "\r\n" in raw and raw.replace("\r\n", "").count("\n") == 0
    _uninstall(tmp_path)
    assert path.read_bytes().decode("utf-8") == original


def test_hermes_accepts_the_written_hooks_block(tmp_path):
    """The rules of ``agent/shell_hooks.py::_parse_hooks_block`` (hermes-agent 0.15.2)."""
    _install(tmp_path)
    hooks = _hooks(tmp_path)
    assert set(hooks) == {"pre_llm_call"}  # a VALID_HOOKS event
    (entry,) = hooks["pre_llm_call"]
    assert isinstance(entry["command"], str) and entry["command"].strip()
    assert 1 <= entry["timeout"] <= 300 and "matcher" not in entry


def test_approval_note_depends_on_hermes_allowlist(tmp_path):
    _, warns = _install_harness_one("hermes", False, None, None, None, home=tmp_path)
    assert any("not approved yet" in w for w in warns)
    allowlist = tmp_path / ".hermes" / "shell-hooks-allowlist.json"
    allowlist.write_text(
        json.dumps({"approvals": [{"event": EVENT, "command": HOOK_COMMAND}]}), encoding="utf-8"
    )
    _, warns = _install_harness_one("hermes", False, None, None, None, home=tmp_path)
    assert not any("not approved" in w for w in warns)
    # install-harness only reads the allowlist.
    assert json.loads(allowlist.read_text(encoding="utf-8")) == {
        "approvals": [{"event": EVENT, "command": HOOK_COMMAND}]
    }


def test_all_includes_hermes_only_where_it_is_installed(tmp_path):
    assert "hermes" not in _targets_for("all", tmp_path)
    (tmp_path / ".hermes").mkdir()
    assert _targets_for("all", tmp_path) == ["claude", "opencode", "hermes"]
    assert _targets_for("hermes", tmp_path.parent) == ["hermes"]
