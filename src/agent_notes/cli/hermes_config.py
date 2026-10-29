"""Edit the ``hooks:`` block of ``~/.hermes/config.yaml`` (Plan 025 Phase 7).

Text-first, so a config's comments survive: with no ``hooks`` key, or a bare
``hooks: {}``, the owned entry goes in as a sentinel-wrapped block. Anything
else (the user already has hooks) is a PyYAML round trip, which drops
comments, so :func:`write_config` saves the original next to the file first.
Hermes rewrites the file the same way on every ``save_config``, so ownership
is judged on the parsed entry, never on the sentinels.

Every text edit is checked by parsing the result: if it isn't exactly the
intended document, the round trip is used instead.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

BEGIN = "# BEGIN agent-notes-harness-managed (hermes hook)"
END = "# END agent-notes-harness-managed (hermes hook)"
BACKUP_SUFFIX = ".agent-notes.bak"

_EMPTY_HOOKS_LINE = re.compile(r"^hooks:[ \t]*(\{[ \t]*\}|null|~)?[ \t]*(#.*)?$", re.MULTILINE)


class ConfigError(RuntimeError):
    """The config can't be parsed or has an unexpected shape; it is left alone."""


def read_text(path: Path) -> tuple[str, str]:
    """``(text with LF newlines, original newline)``; ``("", "\\n")`` if absent."""
    if not path.is_file():
        return "", "\n"
    try:
        raw = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path} is not UTF-8 text; refusing to edit it.") from exc
    eol = "\r\n" if "\r\n" in raw else "\n"
    return raw.replace("\r\n", "\n"), eol


def parse(text: str, path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - PyYAML ships with sentence-transformers
        raise ConfigError("PyYAML is required to edit the Hermes config") from exc
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(
            f"Cannot parse {path}: {exc}. Refusing to overwrite a corrupted config. "
            f"Fix or remove the file and re-run."
        ) from exc
    if doc is None:
        return {}
    if not isinstance(doc, dict):
        raise ConfigError(f"{path} contains a {type(doc).__name__}, not a YAML mapping.")
    return doc


def hook_list(doc: dict[str, Any], event: str) -> list[Any] | None:
    """``hooks.<event>`` when it is a list; ``None`` when absent or empty."""
    hooks = doc.get("hooks")
    if hooks is None:
        return None
    if not isinstance(hooks, dict):
        raise ConfigError("`hooks:` in the Hermes config is not a mapping")
    entries = hooks.get(event)
    if entries is None:
        return None
    if not isinstance(entries, list):
        raise ConfigError(f"`hooks.{event}` in the Hermes config is not a list")
    return entries


def find_entry(doc: dict[str, Any], event: str, command: str) -> dict[str, Any] | None:
    for entry in hook_list(doc, event) or []:
        if isinstance(entry, dict) and entry.get("command") == command:
            return entry
    return None


def _block(event: str, entry: dict[str, Any]) -> str:
    lines = [BEGIN, "hooks:", f"  {event}:"]
    first = True
    for key, value in entry.items():
        lines.append(f"    {'-' if first else ' '} {key}: {json.dumps(value, ensure_ascii=False)}")
        first = False
    lines.append(END)
    return "\n".join(lines) + "\n"


def _dump(doc: dict[str, Any]) -> str:
    import yaml

    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, default_flow_style=False)


def _same(text: str, expected: dict[str, Any], path: Path) -> bool:
    try:
        return parse(text, path) == expected
    except ConfigError:
        return False


def with_entry(
    text: str, doc: dict[str, Any], event: str, entry: dict[str, Any], path: Path
) -> tuple[str, bool]:
    """``(new text, comments_kept)`` with ``entry`` added or replacing ours in ``hooks.<event>``."""
    command = entry["command"]
    expected = copy.deepcopy(doc)
    hooks = expected.get("hooks")
    if not isinstance(hooks, dict):
        hooks = expected["hooks"] = {}
    entries = hooks.get(event)
    if not isinstance(entries, list):
        entries = hooks[event] = []
    replaced = False
    for index, current in enumerate(entries):
        if isinstance(current, dict) and current.get("command") == command:
            entries[index] = dict(entry)
            replaced = True
    if not replaced:
        entries.append(dict(entry))

    if not replaced and not doc.get("hooks"):
        block = _block(event, entry)
        candidate = None
        if "hooks" not in doc:
            body = text if text.endswith("\n") or not text else text + "\n"
            candidate = body + ("\n" if body.strip() else "") + block
        elif len(_EMPTY_HOOKS_LINE.findall(text)) == 1:
            candidate = _EMPTY_HOOKS_LINE.sub(lambda _m: block.rstrip("\n"), text, count=1)
            candidate = candidate if candidate.endswith("\n") else candidate + "\n"
        if candidate is not None and _same(candidate, expected, path):
            return candidate, True
    return _dump(expected), False


def without_entry(
    text: str, doc: dict[str, Any], event: str, command: str, path: Path
) -> tuple[str, bool]:
    """``(new text, comments_kept)`` with our entry dropped and empty containers pruned."""
    expected = copy.deepcopy(doc)
    hooks = expected.get("hooks")
    if isinstance(hooks, dict) and isinstance(hooks.get(event), list):
        hooks[event] = [
            e for e in hooks[event] if not (isinstance(e, dict) and e.get("command") == command)
        ]
        if not hooks[event]:
            del hooks[event]
        if not hooks:
            del expected["hooks"]

    lines = text.split("\n")
    if BEGIN in lines and END in lines and lines.index(BEGIN) < lines.index(END):
        start, stop = lines.index(BEGIN), lines.index(END)
        kept = lines[:start] + lines[stop + 1 :]
        if start > 0 and kept[start - 1] == "" and (start >= len(kept) or kept[start] == ""):
            del kept[start - 1]
        candidate = "\n".join(kept)
        if _same(candidate, expected, path):
            return candidate, True
    return (_dump(expected) if expected else ""), False


def write_config(path: Path, text: str, eol: str, *, backup: bool) -> Path | None:
    """Write ``text`` atomically, saving the original as ``config.yaml.agent-notes.bak``
    first when ``backup`` and a file exists (never overwriting an earlier backup)."""
    saved: Path | None = None
    if backup and path.is_file():
        saved = path.with_name(path.name + BACKUP_SUFFIX)
        if not saved.exists():
            shutil.copy2(path, saved)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.replace("\n", eol)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(data)
        if path.is_file():
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
    return saved
