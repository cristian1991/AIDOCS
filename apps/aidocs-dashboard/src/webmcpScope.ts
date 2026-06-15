// WebMCP dashboard scope + persisted gate connection (DoD #6).
//
// Two pieces of state, both persisted to localStorage so they survive the login
// modal closing/reopening and app restarts, with a tiny pub/sub so the shell can
// re-render when the scope flips:
//   - scope: which scope the WHOLE dashboard operates in — "local" (the on-box
//     AIDOCS instance) or "web" (the codenexus.cloud WebMCP gate).
//   - gate connection: the bearer token + last project list from the gate OAuth
//     flow, so "connected" survives the popup closing (no reconnect each time).

const SCOPE_KEY = "aidocs.webmcp.scope";
const GATE_KEY = "aidocs.webmcp.gate";
const EVENT = "aidocs-webmcp-scope-change";

export type Scope = "local" | "web";

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
  projects: WebmcpProject[];
  expiresAt: number; // epoch ms
};

export function getScope(): Scope {
  return localStorage.getItem(SCOPE_KEY) === "web" ? "web" : "local";
}

export function setScope(scope: Scope): void {
  localStorage.setItem(SCOPE_KEY, scope);
  window.dispatchEvent(new CustomEvent(EVENT));
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
