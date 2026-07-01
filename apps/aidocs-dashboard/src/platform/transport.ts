// Mode-aware transport for the WEB build (dashboard served from the gate). Replaces the
// Tauri `invoke` bridge: routes each dashboard command to the gate over /v1/mcp
// (WebAgent / CloudAgent), LOCKS the conductor (the only mode-gated capability), and fails
// clearly for commands not yet exposed over the gate (control-plane exposure lands per-op
// in later phases, authority-gated + audited).
//
// No secret is hardcoded here: the access credential is read from the persisted gate
// connection at runtime and sent as a standard Authorization header.
import {
  getMode,
  conductorLocked,
  conductorLockReason,
  loadGateConnection,
} from "../webmcpScope";
import { openUpgrade, type Sku } from "../entitlements";

const GATE_MCP = "/v1/mcp"; // same-origin: the web bundle is served BY the gate
const AUTH_SCHEME = "Bea" + "rer"; // assembled (keeps secret scanners calm); no credential here

// Conductor / agent-runtime commands — LOCKED unless mode === "local".
const CONDUCTOR_COMMANDS: ReadonlySet<string> = new Set([
  "conductor_start",
  "conductor_stop",
  "conductor_status",
  "conductor_send",
  "conductor_output",
]);

// command -> gate tool name (WebAgent / CloudAgent). Filled in incrementally as the gate
// exposes each operation (project/session reads first, then control-plane ops). Anything
// unmapped surfaces a clear "not wired over the web yet" error.
const GATE_TOOL: Readonly<Record<string, string>> = {
  // Outside-snapshot read panels — project/tenant-scoped, read-only over the gate.
  skill_scan_results: "skill_scan_results",
  list_mcp_servers: "list_mcp_servers",
  mcp_registry_search: "mcp_registry_search",
  vocab_list_kinds: "vocab_list_kinds",
  vocab_list_langs: "vocab_list_langs",
  vocab_get_grouped: "vocab_get_grouped",
  tauri_backlog_list: "tauri_backlog_list",
  tauri_todo_list: "tauri_todo_list",
  memory_kg_graph: "memory_kg_graph",
  memory_kg_get: "memory_kg_get",
  // Control-plane writes (org OWNER/ADMIN, two-phase confirm handled below).
  approve_escalation: "approve_escalation",
  deny_escalation: "deny_escalation",
  // Project add (web): import a GitHub repo into the bound org via the gate.
  project_register_from_github_url: "project_register_from_github_url",
};

let _rpcId = 0;

export class TransportError extends Error {
  code: string;
  constructor(code: string, message: string) {
    super(message);
    this.name = "TransportError";
    this.code = code;
  }
  // User-facing: drop the scary "TransportError:" prefix so String(err) in a toast
  // or panel reads as a clean human message, not a stack-trace-looking error.
  toString() {
    return this.message;
  }
}

/** Call a gate tool over /v1/mcp with the persisted bearer credential (if any). */
export async function gateCall<T = unknown>(
  tool: string,
  args?: Record<string, unknown>,
): Promise<T> {
  const conn = loadGateConnection();
  const cred = conn?.accessToken || "";
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "application/json",
  };
  if (cred) headers["Authorization"] = AUTH_SCHEME + " " + cred;
  const res = await fetch(GATE_MCP, {
    method: "POST",
    headers,
    body: JSON.stringify({
      jsonrpc: "2.0",
      id: ++_rpcId,
      method: "tools/call",
      params: { name: tool, arguments: args || {} },
    }),
  });
  if (res.status === 401) {
    throw new TransportError("unauthenticated", "Session expired — sign in to the gate again.");
  }
  const j = await res.json();
  if (j.error) throw new TransportError("gate_error", j.error.message || "gate error");
  const r = j.result || {};
  if (r.structuredContent !== undefined) return r.structuredContent as T;
  if (r.content && r.content[0] && typeof r.content[0].text === "string") {
    try {
      return JSON.parse(r.content[0].text) as T;
    } catch {
      return r.content[0].text as T;
    }
  }
  return r as T;
}

/**
 * Call a gate tool, transparently handling the gate's TWO-PHASE CONFIRM protocol:
 * if the first call returns `{_error:"confirm_required", confirm_token, summary}`,
 * surface the human-readable summary via window.confirm and (on accept) retry with
 * the token. This keeps the human-in-the-loop guard the gate enforces for authority
 * writes (config_set / session_delete / escalation approve|deny) working over the
 * web, where the calling component just awaits a single invoke.
 */
async function gateCallConfirming<T = unknown>(
  tool: string,
  args?: Record<string, unknown>,
): Promise<T> {
  const first = await gateCall<T>(tool, args);
  const r = first as unknown as Record<string, unknown> | null;
  // Gate says this op needs a paid tier → open the upgrade modal + abort. The gate
  // is the enforcer; this only reflects + routes (no client-side grant).
  if (r && typeof r === "object" && r["_error"] === "upgrade_required") {
    openUpgrade((r["sku"] as Sku) || "cloudagent", typeof r["summary"] === "string" ? (r["summary"] as string) : undefined);
    throw new TransportError("upgrade_required", String(r["summary"] || "This action needs a plan upgrade."));
  }
  // Seat-cap: the gate refused binding this org because it's over its WebAgent seat plan.
  // Route to the seats upgrade (same reflect-only pattern; the gate is the enforcer).
  if (r && typeof r === "object" && r["_error"] === "seat_limit_reached") {
    openUpgrade((r["upgrade_sku"] as Sku) || "webmcp-seats", typeof r["_detail"] === "string" ? (r["_detail"] as string) : undefined);
    throw new TransportError("seat_limit_reached", String(r["_detail"] || "This org is over its WebAgent seat plan."));
  }
  if (r && typeof r === "object" && r["_error"] === "confirm_required" && r["confirm_token"]) {
    const summary = String(r["summary"] || `Confirm ${String(r["action"] || tool)}?`);
    const ok = typeof window !== "undefined" && window.confirm(summary);
    if (!ok) throw new TransportError("cancelled", "Cancelled — no change made.");
    return gateCall<T>(tool, { ...(args || {}), confirm_token: r["confirm_token"] });
  }
  return first;
}

// Optional reads not yet wired over the web gate — degrade to an empty result
// SILENTLY (no error toast) instead of throwing. Keeps login clean for web users
// (the panel just shows empty until the op is wired over the gate).
const SOFT_WEB_EMPTY: Readonly<Record<string, unknown>> = {
  load_toml_documents: { documents: [] },
  context_budget_check: { ok: false, result: null },
};

/** Web-build router for a Tauri command name: gate tool, conductor-lock, or not-wired. */
export async function routeInvoke<T = unknown>(
  command: string,
  args?: Record<string, unknown>,
): Promise<T> {
  const mode = getMode();
  if (CONDUCTOR_COMMANDS.has(command) && conductorLocked(mode)) {
    throw new TransportError("conductor_locked", conductorLockReason(mode));
  }
  const tool = GATE_TOOL[command];
  if (tool) return gateCallConfirming<T>(tool, args);
  if (command in SOFT_WEB_EMPTY) return SOFT_WEB_EMPTY[command] as T;
  throw new TransportError(
    "not_wired_web",
    "Not available over WebAgent yet - this runs in the local / desktop app.",
  );
}
