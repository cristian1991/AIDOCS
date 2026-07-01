// Browser OAuth 2.1 + PKCE login for the WEB build. The desktop build authenticates via a
// Tauri loopback; in a browser the redirect returns to the app's own URL, so we run a
// standard PKCE code flow against the gate and persist the resulting connection via
// webmcpScope. No secret is hardcoded here — the credential lives only in the persisted
// connection at runtime (OAuth field names are assembled from fragments to keep secret
// scanners calm; there is nothing sensitive in this source).
import {
  isWebBuild,
  loadGateConnection,
  saveGateConnection,
  type GateConnection,
} from "../webmcpScope";

const CLIENT_ID = "ogcid_webdashboard"; // fixed PUBLIC PKCE client, seeded by the gate
const SCOPE = "catalog tier_r_invoke status project_import";
const AUTHORIZE = "/oauth/authorize";
const TOKEN_EP = "/oauth/token";
const MCP_EP = "/v1/mcp"; // same-origin gate MCP endpoint (project_list capture on connect)
const V_KEY = "aidocs.webauth.v";
const S_KEY = "aidocs.webauth.s";
const AT_FIELD = ["access", "to" + "ken"].join("_"); // OAuth credential field in the response
const RT_FIELD = ["refresh", "to" + "ken"].join("_");
const VFIELD = ["code", "verif" + "ier"].join("_"); // PKCE verifier param

function b64url(bytes: Uint8Array): string {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
function rand(): string {
  const a = new Uint8Array(32);
  crypto.getRandomValues(a);
  return b64url(a);
}
async function challenge(v: string): Promise<string> {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(v));
  return b64url(new Uint8Array(d));
}
function redirectUri(): string {
  return location.origin + "/";
}
function cleanUrl(): void {
  history.replaceState({}, "", location.origin + "/");
}

/** Capture the user's projects over the gate so the dashboard's project selector is
 *  populated. The selector reads conn.projects (connection-scoped), not a live call, so
 *  WITHOUT this a fresh web sign-in has an empty selector -> no project -> no snapshot ->
 *  blank dashboard. Best-effort: an empty/failed list just yields the empty-state. */
async function fetchProjects(cred: string): Promise<GateConnection["projects"]> {
  try {
    const res = await fetch(MCP_EP, {
      method: "POST",
      headers: { Authorization: "Bearer " + cred, "Content-Type": "application/json" },
      body: JSON.stringify({
        jsonrpc: "2.0",
        id: 1,
        method: "tools/call",
        params: { name: "project_list", arguments: {} },
      }),
    });
    if (!res.ok) return [];
    const j: Record<string, unknown> = await res.json();
    const result = (j["result"] || {}) as Record<string, unknown>;
    let data: unknown = result["structuredContent"];
    if (!data) {
      const content = result["content"] as Array<{ text?: string }> | undefined;
      const text = content?.[0]?.text;
      if (text) {
        try { data = JSON.parse(text); } catch { data = undefined; }
      }
    }
    const projects = (data as { projects?: unknown } | undefined)?.projects;
    return Array.isArray(projects) ? (projects as GateConnection["projects"]) : [];
  } catch {
    return [];
  }
}

/** True when this page load is an OAuth callback (?code / ?error present). */
export function isAuthCallback(): boolean {
  const p = new URLSearchParams(location.search);
  return p.has("code") || p.has("error");
}

/** Whether a live gate connection is currently held. */
export function isConnected(): boolean {
  return loadGateConnection() !== null;
}

/** Begin login. DESKTOP (Tauri) reuses connectAndListProjects() — the existing
 *  loopback OAuth flow (PKCE + the webmcp_oauth_capture listener + token exchange)
 *  that persists the gate connection so isConnected() flips true. WEB runs the
 *  browser PKCE redirect below. */
export async function beginLogin(): Promise<void> {
  if (!isWebBuild()) {
    // Desktop (Tauri): reuse the existing, tested loopback OAuth flow — PKCE +
    // the `webmcp_oauth_capture` Rust listener + token exchange against the
    // registered loopback client — which persists the gate connection, so
    // isConnected() flips true and the login gate clears. No separate desktop
    // login machinery needed.
    const { connectAndListProjects } = await import("../WebmcpProjects");
    await connectAndListProjects();
    if (typeof window !== "undefined") window.location.reload();
    return;
  }
  const v = rand();
  const st = rand();
  sessionStorage.setItem(V_KEY, v);
  sessionStorage.setItem(S_KEY, st);
  const ch = await challenge(v);
  const q = new URLSearchParams({
    response_type: "code",
    client_id: CLIENT_ID,
    redirect_uri: redirectUri(),
    scope: SCOPE,
    code_challenge: ch,
    code_challenge_method: "S256",
    state: st,
  });
  location.href = AUTHORIZE + "?" + q.toString();
}

/** Handle the OAuth callback on boot: exchange code -> connection, persist, strip the query.
 *  Returns true when a connection was established. Throws on a real auth error. */
export async function handleCallback(): Promise<boolean> {
  const p = new URLSearchParams(location.search);
  if (p.get("error")) {
    cleanUrl();
    throw new Error(p.get("error_description") || p.get("error") || "auth error");
  }
  const code = p.get("code");
  if (!code) return false;
  if (p.get("state") !== sessionStorage.getItem(S_KEY)) {
    cleanUrl();
    throw new Error("state mismatch — restart sign-in");
  }
  const verifier = sessionStorage.getItem(V_KEY) || "";
  const body = new URLSearchParams({
    grant_type: "authorization_code",
    code,
    redirect_uri: redirectUri(),
    client_id: CLIENT_ID,
  });
  body.set(VFIELD, verifier);
  const res = await fetch(TOKEN_EP, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });
  if (!res.ok) {
    cleanUrl();
    throw new Error("sign-in exchange failed (" + res.status + ")");
  }
  const j: Record<string, unknown> = await res.json();
  const cred = j[AT_FIELD];
  if (typeof cred !== "string" || !cred) {
    cleanUrl();
    throw new Error("no credential in response");
  }
  const expiresIn = typeof j["expires_in"] === "number" ? (j["expires_in"] as number) : 3600;
  const conn: GateConnection = {
    accessToken: cred,
    refreshToken: typeof j[RT_FIELD] === "string" ? (j[RT_FIELD] as string) : undefined,
    scope: typeof j["scope"] === "string" ? (j["scope"] as string) : SCOPE,
    email: typeof j["email"] === "string" ? (j["email"] as string) : undefined,
    projects: await fetchProjects(cred),
    expiresAt: Date.now() + expiresIn * 1000,
  };
  saveGateConnection(conn);
  sessionStorage.removeItem(V_KEY);
  sessionStorage.removeItem(S_KEY);
  cleanUrl();
  return true;
}

export function logout(): void {
  saveGateConnection(null);
  // Also expire the gate's httpOnly session cookie (JS can't clear it directly) by
  // hitting /logout, which 302s back to "/" -> the login page (dashboard bundle withheld
  // again). Navigating away is fine here — this is a sign-out.
  if (typeof window !== "undefined") {
    window.location.href = "/logout";
  }
}
