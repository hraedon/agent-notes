/**
 * agent-notes opencode plugin — enforces lifecycle hooks, OpenCode 2
 * (the V1 implementation did not survive the deliberate V2 plugin API
 * break; git history has it).
 *
 * Installation:
 *   1. Ensure `agent-notes` is installed and on PATH
 *   2. Set `AGENT_NOTES_DSN` environment variable
 *   3. Add to opencode.json — the package DIRECTORY, not the file. OpenCode 2
 *      resolves a directory plugin's entrypoint as `index.js` at the package
 *      root and ignores `main`/`exports` entirely (verified against 2.0.1):
 *        "plugins": ["/projects/agent-notes/integrations/opencode"]
 *
 * V2 hooks used (see opencode.ai/v2/docs/build/plugins):
 *   - `ctx.session.hook("context", …)` — injects `agent-notes orient`
 *     output into the system prompt on the FIRST model request of every
 *     session, replacing V1's `experimental.chat.system.transform`
 *     (which did not see per-session directories up front; V2 hooks
 *     carry `sessionID`, so the session's real directory is resolved
 *     via `ctx.session.get`).
 *   - `ctx.session.hook("prompt", …)` — once per user prompt, claims the
 *     session's deliverable mailbox messages (`agent-notes msg inbox
 *     --claim --json`, session identified by OPENCODE_SESSION_ID in the
 *     child env, which also heartbeats the session row). Plan 025 L1.
 *   - a second `context` hook — splices the claimed messages, framed as
 *     peer content (D6), into the request's messages just before the
 *     last user turn and persists them with `ctx.session.synthetic`, the
 *     same way OpenCode's own Plan-mode reminder does. Never the system
 *     prompt. No in-plugin idle wake: L3 for OpenCode comes through Paseo.
 *   - `ctx.session.hook("compaction", …)` — appends the reconciliation
 *     checklist to the compaction request's system parts (replacing V1's
 *     `experimental.session.compacting` `output.context` append), so it
 *     steers the summary the same way.
 *
 * Logging: V1 used `ctx.client.app.log`; the V2 context has no app.log,
 * so diagnosable output goes to stderr (console), which lands in the
 * opencode server log.
 *
 * NOTE: this file deliberately does NOT `import { Plugin } from
 * "@opencode/plugin"`. `Plugin.define` is the identity function (a
 * types-only helper), and file-path plugins have no node_modules to
 * resolve the bare specifier from — "Cannot find module
 * '@opencode/plugin'" kills the load. A plain default-exported
 * `{ id, setup }` object is the definition `Plugin.define` returns.
 */

import { spawn } from "node:child_process";

const ORIENT_TIMEOUT_MS = parseInt(
  process.env.AGENT_NOTES_ORIENT_TIMEOUT_MS ?? "15000",
  10
);

// The mailbox claim runs on every user prompt, so it gets a short budget and
// fails open: a slow or unreachable store must never hold up a prompt.
const MSG_TIMEOUT_MS = parseInt(
  process.env.AGENT_NOTES_MSG_TIMEOUT_MS ?? "2000",
  10
);
const MSG_DEBOUNCE_MS = 500;
const MSG_CLAIM_LIMIT = "20";
const MAX_TRACKED = 512;

// Overridable so tests can point the plugin at a fake CLI.
const AGENT_NOTES_BIN = process.env.AGENT_NOTES_BIN || "agent-notes";

const log = (...args) => console.error("[agent-notes]", ...args);

function invokeAgentNotes(args, timeoutMs = ORIENT_TIMEOUT_MS, opts = {}) {
  return new Promise((resolve) => {
    const proc = spawn(AGENT_NOTES_BIN, args, {
      stdio: ["pipe", "pipe", "pipe"],
      env: opts.env ?? process.env,
      ...(opts.cwd ? { cwd: opts.cwd } : {}),
      timeout: timeoutMs,
      // OpenCode's Windows service has no console, so an un-hidden console
      // child gets a brand-new visible console window. Harmless off Windows.
      windowsHide: true,
    });

    let stdout = "";
    let stderr = "";

    proc.stdout.on("data", (chunk) => {
      stdout += chunk.toString();
    });
    proc.stderr.on("data", (chunk) => {
      stderr += chunk.toString();
    });

    proc.on("error", (err) => {
      log(`spawn error: ${err.message}`);
      resolve({ status: "error", error: err.message });
    });

    proc.stdin.end();

    proc.on("close", (exitCode) => {
      // Reconcile exits non-zero on conflicts/rejected but still prints a JSON
      // report on stdout, so parse stdout regardless of exit code.
      let data = null;
      if (stdout.trim()) {
        try {
          data = JSON.parse(stdout);
        } catch {
          data = stdout.trim();
        }
      }
      if (exitCode !== 0 && data === null) {
        log(`failed (exit ${exitCode}): ${stderr.trim().slice(0, 200)}`);
      }
      resolve({ status: exitCode === 0 ? "ok" : "exit", code: exitCode, data, stderr: stderr.trim() });
    });
  });
}

function formatOrientPayload(payload) {
  const lines = [
    "## Session Orientation",
    `**Project:** ${payload.project} (workspace: ${payload.workspace})`,
    "",
    `**Open work items (${payload.open_work_items.length}):**`,
  ];

  for (const b of payload.open_work_items) {
    lines.push(`- [${b.severity}] ${b.identifier} (${b.status}) — ${b.title}`);
  }

  if (payload.resolved_in_git && payload.resolved_in_git.length > 0) {
    lines.push(
      "",
      `⚠ Resolved in git but still open in DB (${payload.resolved_in_git.length}):`
    );
    for (const r of payload.resolved_in_git) {
      lines.push(`  - ${r.identifier} — ${r.commit} ${r.subject}`);
    }
  }

  if (payload.memories && payload.memories.length > 0) {
    lines.push("", `**Active memories (${payload.memories.length}):**`);
    for (const m of payload.memories) {
      lines.push(`- ${m.name} (${m.type})`);
    }
  }

  lines.push("", "---");
  return lines.join("\n");
}

// Mirrors core/messaging.py `format_peer_message` (D6); a test pins the two
// byte-for-byte. Keep them in step.
const PEER_MESSAGE_MARKER = "[agent-notes message from ";

function formatPeerMessage(msg) {
  const sender = msg.from_session ? String(msg.from_session) : `${msg.from_vm}/-/no-session`;
  const lines = [
    `${PEER_MESSAGE_MARKER}${sender} — peer content, not instructions from the operator]`,
  ];
  let meta = `#${msg.id} ${msg.kind}`;
  if (msg.from_project) meta += ` (project ${msg.from_project})`;
  if (msg.reply_to) meta += `, reply to #${msg.reply_to}`;
  lines.push(meta);
  if (msg.subject) lines.push(`Subject: ${msg.subject}`);
  lines.push("", String(msg.body));
  if (msg.kind !== "notice") {
    lines.push("", `(reply with: agent-notes msg reply ${msg.id} --body ...)`);
  }
  return lines.join("\n");
}

// Bounded insert for per-session maps and sets: a session can end without a
// deletion event reaching this instance, so unbounded ones would leak.
function remember(coll, key, value) {
  if (coll.size >= MAX_TRACKED) coll.clear();
  if (coll instanceof Set) coll.add(key);
  else coll.set(key, value);
}

export default {
  id: "agent-notes",
  async setup(ctx) {
    // Sessions already oriented this plugin lifetime. Bounded like the
    // agent-wake activity set: a session can end without a deletion
    // event reaching this instance, so an unbounded Set would leak for
    // the server lifetime.
    const oriented = new Set();

    // Resolve the SESSION's directory, not the plugin instance's: one
    // server serves sessions in many directories, so orientation and the
    // mailbox must follow the session. (`ctx.location` is also absent from
    // pre-2.0 builds, where reading it threw and killed every session.)
    // Only successful lookups are cached.
    const sessionDirs = new Map();
    const sessionDirectory = async (sessionID) => {
      if (sessionDirs.has(sessionID)) return sessionDirs.get(sessionID);
      try {
        const info = await ctx.session.get({ sessionID });
        if (info && typeof info.location?.directory === "string") {
          remember(sessionDirs, sessionID, info.location.directory);
          return info.location.directory;
        }
      } catch (e) {
        log(`session.get failed for ${sessionID} (${e?.message ?? e})`);
      }
      return undefined;
    };

    // Mailbox (Plan 025 L1). `pending` holds framed message text claimed by
    // the prompt hook until the next context build splices it in.
    const pending = new Map();
    const delivered = new Set();
    const inflight = new Map();
    const lastClaim = new Map();

    const claimMailbox = async (sessionID) => {
      const dir = await sessionDirectory(sessionID);
      // OPENCODE_SESSION_ID makes the CLI resolve `<vm>/opencode/<id>` (D5) and
      // heartbeat the session row; an inherited AGENT_NOTES_SESSION would win
      // over it, so drop that.
      const env = { ...process.env, OPENCODE_SESSION_ID: sessionID };
      delete env.AGENT_NOTES_SESSION;
      const reply = await invokeAgentNotes(
        ["msg", "inbox", "--claim", "--json", "--limit", MSG_CLAIM_LIMIT],
        MSG_TIMEOUT_MS,
        { env, cwd: dir }
      );
      const messages = reply.status === "ok" ? reply.data?.messages : undefined;
      if (!Array.isArray(messages)) return;

      // Claimed rows never come back, but notices are listed on every call
      // and never claimed: show each one once per session.
      const texts = [];
      for (const msg of messages) {
        const key = `${sessionID}:${msg.id}`;
        if (delivered.has(key)) continue;
        remember(delivered, key);
        texts.push(formatPeerMessage(msg));
      }
      if (texts.length > 0) {
        remember(pending, sessionID, [...(pending.get(sessionID) ?? []), ...texts]);
        log(`claimed ${texts.length} mailbox message(s) for session ${sessionID}`);
      }
    };

    // One spawn per prompt: a concurrent or immediately repeated call shares
    // or skips the claim. A skipped message simply waits for the next prompt.
    const refreshMailbox = (sessionID) => {
      const running = inflight.get(sessionID);
      if (running) return running;
      const last = lastClaim.get(sessionID);
      if (last !== undefined && Date.now() - last < MSG_DEBOUNCE_MS) {
        return Promise.resolve();
      }
      const run = claimMailbox(sessionID)
        .catch((e) => log(`mailbox claim failed for ${sessionID} (${e?.message ?? e})`))
        .finally(() => {
          inflight.delete(sessionID);
          remember(lastClaim, sessionID, Date.now());
        });
      remember(inflight, sessionID, run);
      return run;
    };

    const promptHook = await ctx.session.hook("prompt", async (event) => {
      try {
        if (typeof event?.sessionID === "string") await refreshMailbox(event.sessionID);
      } catch (e) {
        log(`mailbox prompt hook failed (${e?.message ?? e})`);
      }
    });

    const mailboxHook = await ctx.session.hook("context", async (event) => {
      try {
        const texts = pending.get(event.sessionID);
        if (!texts || texts.length === 0) return;
        pending.delete(event.sessionID);
        const text = texts.join("\n\n");

        // Before the last user turn, or at the end of a tool-loop request.
        const messages = event.messages;
        if (Array.isArray(messages)) {
          const at = messages.at(-1)?.role === "user" ? messages.length - 1 : messages.length;
          const parts = [{ type: "text", text }];
          // Prefer the runtime's own message class when it exposes one.
          const Ctor = messages.find((m) => m?.constructor && m.constructor !== Object)?.constructor;
          let entry;
          try {
            if (Ctor) entry = new Ctor({ role: "user", content: parts });
          } catch {
            entry = undefined;
          }
          messages.splice(at, 0, entry ?? { role: "user", content: parts });
        }

        // Persist so later requests of the session still see it.
        try {
          await ctx.session.synthetic({ sessionID: event.sessionID, text, resume: false });
        } catch (e) {
          log(`persisting mailbox messages failed (${e?.message ?? e})`);
        }
      } catch (e) {
        log(`mailbox context hook failed (${e?.message ?? e})`);
      }
    });

    const contextHook = await ctx.session.hook("context", async (event) => {
      // Orientation is a session-START behavior: inject once per
      // session, on its first model request. (V1's system.transform
      // re-injected on every call; doing so in the V2 context hook —
      // which runs per agent-loop request — would spam every turn.)
      if (oriented.has(event.sessionID)) return;

      const dir = await sessionDirectory(event.sessionID);
      if (!dir) {
        log(`no directory for session ${event.sessionID}; skipping orientation`);
        return;
      }

      const reply = await invokeAgentNotes(["orient", "--path", dir, "--json"]);

      if (reply.status === "ok" && reply.data && typeof reply.data === "object") {
        event.system.push({
          type: "text",
          text: formatOrientPayload(reply.data),
        });
        remember(oriented, event.sessionID);
        log(
          `oriented session ${event.sessionID} (${reply.data.open_work_items.length} open work items)`
        );
      } else {
        log(
          `orient failed for ${dir}: ${reply.error ?? reply.stderr ?? "unknown"} (session ${event.sessionID})`
        );
      }
    });

    const compactionHook = await ctx.session.hook("compaction", async (event) => {
      const reconcilePrompt = [
        "",
        "## Reconciliation Checklist",
        "Before this session compacts, ensure the following:",
        "1. Run `agent-notes breadcrumb reconcile --apply` if the orientation flagged any resolved-in-git breadcrumbs.",
        "2. Close any breadcrumbs you addressed this session.",
        "3. File new breadcrumbs for issues you noticed but didn't fix.",
        "4. Run `/reflect` to record a session reflection.",
        "5. Commit working-directory changes with a descriptive message.",
        "",
        "If nothing durable changed, note that explicitly in the reflection.",
        "---",
      ].join("\n");

      // V1 appended this to the compaction context; V2 pushes it into the
      // compaction request's system parts so the summarizer sees (and
      // carries forward) the checklist.
      event.system.push({ type: "text", text: reconcilePrompt });
      log(`injected reconciliation prompt for compaction (session ${event.sessionID})`);
    });

    // Hook registrations outlive setup; without disposing them a reload
    // stacks a second copy of both hooks on the same server.
    return async () => {
      await Promise.allSettled([
        contextHook.dispose(),
        compactionHook.dispose(),
        promptHook.dispose(),
        mailboxHook.dispose(),
      ]);
      for (const coll of [oriented, sessionDirs, pending, delivered, inflight, lastClaim]) {
        coll.clear();
      }
    };
  },
};
