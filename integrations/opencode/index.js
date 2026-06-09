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

function invokeAgentNotes(args, client) {
  return new Promise((resolve) => {
    const proc = spawn("agent-notes", args, {
      stdio: ["pipe", "pipe", "pipe"],
      env: process.env,
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
      if (exitCode !== 0) {
        const msg = `[agent-notes] failed (exit ${exitCode}): ${stderr.trim().slice(0, 200)}`;
        if (client?.app?.log) {
          client.app.log(msg);
        } else {
          console.error(msg);
        }
        resolve({ status: "error", stderr: stderr.trim() });
        return;
      }

      if (!stdout.trim()) {
        resolve({ status: "error", reason: "empty stdout" });
        return;
      }

      try {
        resolve({ status: "ok", data: JSON.parse(stdout) });
      } catch {
        resolve({ status: "ok", data: stdout.trim() });
      }
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

export default async function agentNotesPlugin(ctx) {
  const sessionDirs = new Map();

  return {
    event: async ({ event }) => {
      if (event?.type === "session.created" && event.properties?.info?.directory) {
        const sessionID = event.properties.sessionID;
        const dir = event.properties.info.directory;
        sessionDirs.set(sessionID, dir);
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
        ctx.client
      );

      if (reply.status === "ok" && typeof reply.data === "object") {
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
      output.context.push(reconcilePrompt);

      if (dir) {
        ctx.client?.app?.log?.(
          `[agent-notes] injected reconciliation prompt for session ${sessionID}`
        );
      }
    },
  };
}
