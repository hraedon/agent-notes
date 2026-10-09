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
 *   - `ctx.session.hook("compaction", …)` — appends the regista sync
 *     block and reconciliation checklist to the compaction request's
 *     system parts (replacing V1's `experimental.session.compacting`
 *     `output.context` append), so they steer the summary the same way.
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
const RECONCILE_TIMEOUT_MS = parseInt(
  process.env.AGENT_NOTES_RECONCILE_TIMEOUT_MS ?? "60000",
  10
);

const log = (...args) => console.error("[agent-notes]", ...args);

function invokeAgentNotes(args, timeoutMs = ORIENT_TIMEOUT_MS) {
  return new Promise((resolve) => {
    const proc = spawn("agent-notes", args, {
      stdio: ["pipe", "pipe", "pipe"],
      env: process.env,
      timeout: timeoutMs,
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

async function buildRegistaSyncBlock() {
  // dossier-006 §6: Stop/PreCompact must reconcile and loudly report pending
  // ops. Reconcile is best-effort — if regista is unreachable it replays nothing
  // and the ops stay in the outbox; we then surface the stale count loudly.
  const lines = ["", "## Regista Sync"];

  let recData = null;
  try {
    const rec = await invokeAgentNotes(
      ["outbox", "reconcile", "--json"],
      RECONCILE_TIMEOUT_MS
    );
    // Reconcile exits non-zero on conflicts/rejected but still prints a JSON
    // report, so parse the data regardless of status.
    if (rec.data && typeof rec.data === "object") {
      recData = rec.data;
    }
  } catch {
    recData = null;
  }
  if (recData && (recData.replayed !== undefined || recData.error)) {
    if (recData.error) {
      lines.push(`Reconcile: not run — ${recData.error}`);
    } else {
      lines.push(
        `Reconcile: replayed ${recData.replayed ?? 0}, rejected ${recData.rejected ?? 0}, ` +
          `conflicts ${recData.conflicts ?? 0}.`
      );
    }
  } else {
    lines.push("Reconcile: unavailable (see logs).");
  }

  let totalPending = 0;
  let detail = "";
  try {
    const status = await invokeAgentNotes(["outbox", "status", "--json"]);
    if (status.data && Array.isArray(status.data.projects)) {
      for (const p of status.data.projects) {
        totalPending += p.pending ?? 0;
      }
      if (status.data.projects.length > 0) {
        detail = status.data.projects
          .map((p) => `${p.project}: ${p.pending} pending/${p.conflicts} conflicts`)
          .join("; ");
      }
    }
  } catch {
    totalPending = -1;
  }
  if (totalPending > 0) {
    lines.push(
      `⚠ STALE — ${totalPending} op(s) still pending sync. ` +
        `Resolve before relying on work-item state. ` +
        (detail ? `(${detail}) ` : "") +
        `Run: agent-notes outbox reconcile`
    );
  } else if (totalPending === 0) {
    lines.push("No ops pending sync.");
  } else {
    lines.push("Outbox status unavailable (see logs).");
  }
  lines.push("");
  return lines.join("\n");
}

export default {
  id: "agent-notes",
  async setup(ctx) {
    // Sessions already oriented this plugin lifetime. Bounded like the
    // agent-wake activity set: a session can end without a deletion
    // event reaching this instance, so an unbounded Set would leak for
    // the server lifetime.
    const oriented = new Set();
    const MAX_ORIENTED = 512;

    const contextHook = await ctx.session.hook("context", async (event) => {
      // Orientation is a session-START behavior: inject once per
      // session, on its first model request. (V1's system.transform
      // re-injected on every call; doing so in the V2 context hook —
      // which runs per agent-loop request — would spam every turn.)
      if (oriented.has(event.sessionID)) return;

      // Resolve the SESSION's directory, not the plugin instance's: one
      // server serves sessions in many directories, so orientation must
      // follow the session. (`ctx.location` is also absent from pre-2.0
      // builds, where reading it threw and killed every session.)
      let dir;
      try {
        const info = await ctx.session.get({ sessionID: event.sessionID });
        if (info && typeof info.location?.directory === "string") {
          dir = info.location.directory;
        }
      } catch (e) {
        log(
          `session.get failed for ${event.sessionID} (${e?.message ?? e})`
        );
      }

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
        if (oriented.size >= MAX_ORIENTED) oriented.clear();
        oriented.add(event.sessionID);
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
      const syncBlock = await buildRegistaSyncBlock();

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

      // V1 appended these to the compaction context; V2 pushes them
      // into the compaction request's system parts so the summarizer
      // sees (and carries forward) the sync state and checklist.
      event.system.push({ type: "text", text: syncBlock + reconcilePrompt });
      log(`injected reconciliation prompt for compaction (session ${event.sessionID})`);
    });

    // Hook registrations outlive setup; without disposing them a reload
    // stacks a second copy of both hooks on the same server.
    return async () => {
      await Promise.allSettled([contextHook.dispose(), compactionHook.dispose()]);
      oriented.clear();
    };
  },
};
