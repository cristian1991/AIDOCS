// WebAgent dashboard scope + persisted gate connection (DoD #6).
//
// Two pieces of state, both persisted to localStorage so they survive the login
// modal closing/reopening and app restarts, with a tiny pub/sub so the shell can
// re-render when the scope flips:
//   - scope: which scope the WHOLE dashboard operates in — "local" (the on-box
//     AIDOCS instance) or "web" (the codenexus.cloud WebAgent gate).
//   - gate connection: the bearer token + last project list from the gate OAuth
//     flow, so "connected" survives the popup closing (no reconnect each time).

const SCOPE_KEY = "aidocs.webmcp.scope";
const GATE_KEY = "aidocs.webmcp.gate";
const EVENT = "aidocs-webmcp-scope-change";

export type Scope = "local" | "web";

// The dashboard MODE (operator model 2026-06-16). One enum; the SELECTABLE set is
// build-filtered (web build = webmcp+cloudagent; desktop = all three). The CONDUCTOR is
// the ONLY mode-gated capability — unlocked in local, locked in webmcp (the connected web
// client is the agent) and cloudagent (until ADB AI-agent + dockerization). Everything
// else works in every mode.
export type Mode = "local" | "webmcp" | "cloudagent";

export type WebmcpProject = {
  project_id: string;
  name: string;
  source: string;
  is_default: boolean;
  current: boolean;
};

export type GateConnection = {
  accessToken: string;
  refreshToken?: string;
  scope?: string;
  email?: string;
  projects: WebmcpProject[];
  expiresAt: number; // epoch ms
};

// ── mode (3-mode model) ──────────────────────────────────────────────────────
const MODE_KEY = "aidocs.dashboard.mode";

/** True only in the web build (Vite `define` sets __AIDOCS_WEB__). Both builds define it
 *  (web=true, desktop=false) so the token is always replaced — never a runtime ReferenceError. */
export function isWebBuild(): boolean {
  return typeof __AIDOCS_WEB__ !== "undefined" && __AIDOCS_WEB__ === true;
}

/** Modes selectable in THIS build. The web build hides "local" — no on-box agent in a browser. */
export function availableModes(): Mode[] {
  return isWebBuild() ? ["webmcp", "cloudagent"] : ["local", "webmcp", "cloudagent"];
}

export function getMode(): Mode {
  const raw = localStorage.getItem(MODE_KEY) || "";
  let m: Mode | "" =
    raw === "local" || raw === "webmcp" || raw === "cloudagent" ? raw : "";
  if (!m) {
    // Back-compat: fall back to the legacy scope key ("web" → "webmcp").
    const legacy = localStorage.getItem(SCOPE_KEY);
    m = legacy === "web" ? "webmcp" : legacy === "local" ? "local" : "";
  }
  if (!m) m = isWebBuild() ? "webmcp" : "local";
  if (isWebBuild() && m === "local") m = "webmcp"; // a browser can't run the on-box agent
  return m;
}

export function setMode(mode: Mode): void {
  localStorage.setItem(MODE_KEY, mode);
  // Keep the legacy scope key in sync so existing getScope()-based branches still work.
  localStorage.setItem(SCOPE_KEY, mode === "local" ? "local" : "web");
  window.dispatchEvent(new CustomEvent(EVENT));
}

/** The CONDUCTOR is the ONLY mode-gated capability: unlocked only in local. */
export function conductorLocked(mode: Mode = getMode()): boolean {
  return mode !== "local";
}

/** UI message for why the conductor is locked in `mode` ("" when unlocked). */
export function conductorLockReason(mode: Mode = getMode()): string {
  if (mode === "local") return "";
  if (mode === "webmcp")
    return "In WebAgent mode the connected web client (ChatGPT/Claude) IS the agent — the dashboard does not run a Conductor.";
  return "CloudAgent runtime is not yet available (pending the ADB AI-agent + dockerization integration).";
}

// ── legacy scope adapter (existing code uses getScope() === "web") ───────────
export function getScope(): Scope {
  return getMode() === "local" ? "local" : "web";
}

export function setScope(scope: Scope): void {
  setMode(scope === "web" ? "webmcp" : "local");
}

/** Subscribe to scope changes (returns an unsubscribe). Also fires on storage
 *  events so a change in any window propagates. */
export function onScopeChange(cb: () => void): () => void {
  const handler = () => cb();
  window.addEventListener(EVENT, handler);
  window.addEventListener("storage", handler);
  return () => {
    window.removeEventListener(EVENT, handler);
    window.removeEventListener("storage", handler);
  };
}

export function loadGateConnection(): GateConnection | null {
  try {
    const raw = localStorage.getItem(GATE_KEY);
    if (!raw) return null;
    const c = JSON.parse(raw) as GateConnection;
    return c && c.expiresAt > Date.now() ? c : null; // drop expired tokens
  } catch {
    return null;
  }
}

export function saveGateConnection(conn: GateConnection | null): void {
  if (conn) localStorage.setItem(GATE_KEY, JSON.stringify(conn));
  else localStorage.removeItem(GATE_KEY);
  window.dispatchEvent(new CustomEvent(EVENT));
}
