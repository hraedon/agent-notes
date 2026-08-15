import { spawn } from "node:child_process";

/**
 * agent-notes opencode plugin — enforces lifecycle hooks (Plan 007 Piece 2).
 *
 * Installation:
 *   1. Ensure `agent-notes` is installed and on PATH
 *   2. Set `AGENT_NOTES_DSN` environment variable
 *   3. Add to opencode.json:
 *        "plugin": ["/projects/agent-notes/integrations/opencode/index.js"]
 *
 * Hooks:
 *   - `experimental.chat.system.transform` — injects `agent-notes orient` into
 *     the system prompt on every session start.
 *   - `experimental.session.compacting` — appends a reconciliation prompt to
 *     the compaction context (the opencode equivalent of `/end`).
 */

const ORIENT_TIMEOUT_MS = parseInt(
  process.env.AGENT_NOTES_ORIENT_TIMEOUT_MS ?? "15000",
  10
);
const RECONCILE_TIMEOUT_MS = parseInt(
  process.env.AGENT_NOTES_RECONCILE_TIMEOUT_MS ?? "60000",
  10
);

function invokeAgentNotes(args, client, timeoutMs = ORIENT_TIMEOUT_MS, sessionID = undefined) {
  return new Promise((resolve) => {
    const env = { ...process.env };
    // WI-067: thread the harness session id into every spawned agent-notes
    // process so session-scoped identity records key correctly under opencode.
    if (sessionID) {
      env.OPENCODE_SESSION_ID = String(sessionID);
    }
    const proc = spawn("agent-notes", args, {
      stdio: ["pipe", "pipe", "pipe"],
      env,
      timeout: ORIENT_TIMEOUT_MS,
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
      const msg = `[agent-notes] spawn error: ${err.message}`;
      if (client?.app?.log) {
        client.app.log(msg);
      } else {
        console.error(msg);
      }
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
        const msg = `[agent-notes] failed (exit ${exitCode}): ${stderr.trim().slice(0, 200)}`;
        if (client?.app?.log) {
          client.app.log(msg);
        } else {
          console.error(msg);
        }
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

async function buildRegistaSyncBlock(client, sessionID = undefined) {
  // dossier-006 §6: Stop/PreCompact must reconcile and loudly report pending
  // ops. Reconcile is best-effort — if regista is unreachable it replays nothing
  // and the ops stay in the outbox; we then surface the stale count loudly.
  const lines = ["", "## Regista Sync"];

  let recData = null;
  try {
    const rec = await invokeAgentNotes(
      ["outbox", "reconcile", "--json"],
      client,
      RECONCILE_TIMEOUT_MS,
      sessionID
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
    const status = await invokeAgentNotes(
      ["outbox", "status", "--json"],
      client,
      undefined,
      sessionID
    );
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

export default async function agentNotesPlugin(ctx) {
  const sessionDirs = new Map();

  return {
    event: async ({ event }) => {
      if (event?.type === "session.created" && event.properties?.info?.directory) {
        const sessionID = event.properties.sessionID;
        const dir = event.properties.info.directory;
        sessionDirs.set(sessionID, dir);
        // WI-067: the session id is threaded per-spawn (invokeAgentNotes), NOT
        // stashed on process.env. A server process hosts concurrent sessions;
        // mutating process.env would leak one session's id into every other
        // session's tool subprocesses. Tool calls spawned by opencode itself
        // (not by this plugin) therefore have no OPENCODE_SESSION_ID and fail
        // closed in `agent-notes session declare` — the honest behavior when
        // the harness cannot expose a session safely to its own subprocesses.
        ctx.client?.app?.log?.(
          `[agent-notes] session ${sessionID} → dir ${dir}`
        );
      }
    },

    "experimental.chat.system.transform": async (input, output) => {
      const sessionID = input.sessionID;
      const dir = sessionDirs.get(sessionID);
      if (!dir) {
        ctx.client?.app?.log?.(
          `[agent-notes] no directory for session ${sessionID}; skipping orientation`
        );
        return;
      }

      const reply = await invokeAgentNotes(
        ["orient", "--path", dir, "--json"],
        ctx.client,
        undefined,
        sessionID
      );

      if (reply.status === "ok" && reply.data && typeof reply.data === "object") {
        const orientText = formatOrientPayload(reply.data);
        output.system = output.system ?? [];
        if (!Array.isArray(output.system)) {
          output.system = [output.system];
        }
        output.system.push(orientText);
        ctx.client?.app?.log?.(
          `[agent-notes] oriented session ${sessionID} (${reply.data.open_work_items.length} open work items)`
        );
      } else {
        ctx.client?.app?.log?.(
          `[agent-notes] orient failed for ${dir}: ${reply.error ?? reply.reason ?? "unknown"}`
        );
      }
    },

    "experimental.session.compacting": async (input, output) => {
      const sessionID = input.sessionID;
      const dir = sessionDirs.get(sessionID);

      const syncBlock = await buildRegistaSyncBlock(ctx.client, sessionID);

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

      output.context = output.context ?? [];
      output.context.push(syncBlock + reconcilePrompt);

      if (dir) {
        ctx.client?.app?.log?.(
          `[agent-notes] injected reconciliation prompt for session ${sessionID}`
        );
      }
    },
  };
}
