// Drives integrations/opencode/index.js against a fake OpenCode 2 `ctx` and a
// fake `agent-notes` CLI. Used by tests/test_opencode_plugin.py:
//
//   node opencode_plugin_harness.mjs <scenario.json>
//
// The scenario is { cli, dir, getFails, steps }. It prints one JSON report:
//   { steps: [...], synthetic: [...], cliCalls: [...], handlerErrors: [...] }
//
// Step ops:
//   { op: "prompt", session, count? }     fire the prompt hook (count>1: concurrently)
//   { op: "context", session }            run every context hook on a fresh request
//   { op: "sleep", ms }
//   { op: "dispose" }

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const scenario = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));

const logPath = path.join(
  fs.mkdtempSync(path.join(os.tmpdir(), "oc-plugin-")),
  "cli-calls.jsonl"
);
const configPath = logPath + ".config.json";
fs.writeFileSync(configPath, JSON.stringify({ ...scenario.cli, log: logPath }));

process.env.AGENT_NOTES_BIN = process.execPath;
process.env.NODE_OPTIONS = `--require "${path.join(here, "opencode_fake_cli.cjs").replaceAll("\\", "/")}"`;
process.env.FAKE_AN_CONFIG = configPath;
// A session variable inherited from the server must not leak into the child.
process.env.AGENT_NOTES_SESSION = "other-vm/claude/leaked";
Object.assign(process.env, scenario.env ?? {});

const hooks = { prompt: [], context: [], compaction: [] };
const synthetic = [];
const handlerErrors = [];
const originalError = console.error;
if (!process.env.HARNESS_DEBUG) console.error = () => {};

const ctx = {
  session: {
    hook: async (name, fn) => {
      const entry = { fn, disposed: false };
      hooks[name].push(entry);
      return { dispose: async () => (entry.disposed = true) };
    },
    get: async ({ sessionID }) => {
      if (scenario.getFails) throw new Error("session.get unavailable");
      return { id: sessionID, location: { directory: scenario.dir } };
    },
    synthetic: async (args) => {
      if (scenario.syntheticFails) throw new Error("synthetic unavailable");
      synthetic.push(args);
    },
  },
};

const plugin = (await import(pathToFileURL(path.join(here, "..", "integrations", "opencode", "index.js")))).default;
const cleanup = await plugin.setup(ctx);

async function run(entry, event) {
  try {
    await entry.fn(event);
  } catch (e) {
    handlerErrors.push(String(e?.message ?? e));
  }
}

const report = [];
for (const step of scenario.steps) {
  if (step.op === "sleep") {
    await new Promise((r) => setTimeout(r, step.ms));
  } else if (step.op === "prompt") {
    const fire = () =>
      Promise.all(
        hooks.prompt.map((h) => run(h, { sessionID: step.session, prompt: { text: "hi" } }))
      );
    await Promise.all(Array.from({ length: step.count ?? 1 }, fire));
  } else if (step.op === "context") {
    const event = {
      sessionID: step.session,
      system: [],
      messages: step.messages ?? [{ role: "user", content: [{ type: "text", text: "hi" }] }],
    };
    for (const h of hooks.context) await run(h, event);
    report.push({ op: "context", session: step.session, system: event.system, messages: event.messages });
  } else if (step.op === "dispose") {
    await cleanup();
    report.push({ op: "dispose", disposed: Object.values(hooks).flat().every((h) => h.disposed) });
  }
}

console.error = originalError;
const cliCalls = fs.existsSync(logPath)
  ? fs.readFileSync(logPath, "utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l))
  : [];
process.stdout.write(JSON.stringify({ steps: report, synthetic, cliCalls, handlerErrors }));
process.exit(0);
