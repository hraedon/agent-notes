// Fake `agent-notes` CLI for tests/test_opencode_plugin.py.
//
// The plugin spawns AGENT_NOTES_BIN (node itself) with the real CLI argv, and
// NODE_OPTIONS="--require <this file>" makes node run this instead of looking
// for a script called "msg". That keeps the plugin's real spawn path (argv, env,
// cwd, timeout) under test on every platform, including Windows where a .cmd
// shim could not be spawned without a shell.
//
// FAKE_AN_CONFIG names a JSON file:
//   { "log": <path>, "orient": {...}, "msg": [<response>, ...] }
// where each <response> is { "exit": 0, "stdout": "...", "stderr": "...",
// "hang": false } and the n-th `msg` call gets msg[n] (the last repeats).

const fs = require("node:fs");
const path = require("node:path");

const config = JSON.parse(fs.readFileSync(process.env.FAKE_AN_CONFIG, "utf8"));
const argv = [path.basename(process.argv[1]), ...process.argv.slice(2)];

let n = 0;
try {
  n = fs
    .readFileSync(config.log, "utf8")
    .split("\n")
    .filter((line) => line && JSON.parse(line).argv[0] === "msg").length;
} catch {
  n = 0;
}

fs.appendFileSync(
  config.log,
  JSON.stringify({
    argv,
    cwd: process.cwd(),
    sessionId: process.env.OPENCODE_SESSION_ID ?? null,
    hasAgentNotesSession: "AGENT_NOTES_SESSION" in process.env,
  }) + "\n"
);

let response;
if (argv[0] === "orient") {
  response = { exit: 0, stdout: JSON.stringify(config.orient) };
} else {
  const list = config.msg;
  response = list[Math.min(n, list.length - 1)];
}

// Everything here is synchronous: node goes on to load the "script" named by
// the first CLI argument as soon as this preload returns.
if (response.hang) {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 60000);
}
if (response.stderr) fs.writeSync(2, response.stderr);
fs.writeSync(1, response.stdout ?? "");
process.exit(response.exit ?? 0);
