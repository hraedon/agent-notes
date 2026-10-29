"""A fake ``paseo`` executable for dispatcher tests (Plan 025 Phase 5).

:func:`install` writes a small Python program plus a launcher (``paseo`` with a
shebang on POSIX, ``paseo.cmd`` on Windows). The program appends each call's
arguments (and, for ``send``, the prompt file's text) to a JSON-lines log, and
answers ``inspect <id> --json`` from a JSON state file the test edits.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

_PROGRAM = r"""
import json, sys
STATE = {state!r}
LOG = {log!r}
args = sys.argv[1:]
state = json.load(open(STATE, encoding="utf-8"))
entry = {{"args": args}}
if args and args[0] == "send" and "--prompt-file" in args:
    entry["prompt"] = open(args[args.index("--prompt-file") + 1], encoding="utf-8").read()
with open(LOG, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(entry) + "\n")
if args[:1] == ["inspect"]:
    agent = state.get("agents", {{}}).get(args[1])
    if agent is None:
        print("Agent not found: " + args[1], file=sys.stderr)
        sys.exit(1)
    print(json.dumps({{"Id": args[1], "Status": agent.get("status", "idle"),
                      "Archived": agent.get("archived", False)}}))
    sys.exit(0)
if args[:1] == ["send"]:
    code = int(state.get("send_exit", 0))
    if code:
        print("send failed", file=sys.stderr)
    else:
        print("sent")
    sys.exit(code)
sys.exit(2)
"""


@dataclass
class FakePaseo:
    binary: Path
    state_path: Path
    log_path: Path

    def set_agent(self, agent_id: str, status: str = "idle", archived: bool = False) -> None:
        state = self.state()
        state.setdefault("agents", {})[agent_id] = {"status": status, "archived": archived}
        self._write(state)

    def set_send_exit(self, code: int) -> None:
        state = self.state()
        state["send_exit"] = code
        self._write(state)

    def state(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def _write(self, state: dict) -> None:
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def calls(self) -> list[dict]:
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines()]

    def sends(self) -> list[dict]:
        return [c for c in self.calls() if c["args"][:1] == ["send"]]


def install(directory: Path) -> FakePaseo:
    directory.mkdir(parents=True, exist_ok=True)
    state = directory / "state.json"
    log = directory / "calls.jsonl"
    state.write_text(json.dumps({"agents": {}}), encoding="utf-8")
    program = directory / "fake_paseo_main.py"
    program.write_text(_PROGRAM.format(state=str(state), log=str(log)), encoding="utf-8")
    if os.name == "nt":
        binary = directory / "paseo.cmd"
        binary.write_text(f'@"{sys.executable}" "{program}" %*\r\n', encoding="utf-8")
    else:
        binary = directory / "paseo"
        binary.write_text(f"#!{sys.executable}\n" + program.read_text(encoding="utf-8"))
        binary.chmod(0o755)
    return FakePaseo(binary, state, log)
