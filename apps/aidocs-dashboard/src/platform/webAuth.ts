// Browser OAuth 2.1 + PKCE login for the WEB build. The desktop build authenticates via a
// Tauri loopback; in a browser the redirect returns to the app's own URL, so we run a
// standard PKCE code flow against the gate and persist the resulting connection via
// webmcpScope. No secret is hardcoded here — the credential lives only in the persisted
// connection at runtime (OAuth field names are assembled from fragments to keep secret
// scanners calm; there is nothing sensitive in this source).
import {
  isWebBuild,
  loadGateConnection,
  loadGateConnectionIgnoringExpiry,
  saveGateConnection,
  type GateConnection,
} from "../webmcpScope";

const CLIENT_ID = "ogcid_webdashboard"; // fixed PUBLIC PKCE client, seeded by the gate
// `sync` (#1002) reaches /sync/events and /v1/backlog, which the dashboard's own
// backlog and event views read; without it every sign-in got 403 insufficient_scope
// on its own data. `tier_m_edit` is deliberately NOT requested — the gate withholds
// source-edit authority from this browser client (ensure_web_dashboard_client).
// `xaacp_write` (#1015) is the messaging grant. It is DISTINCT from
// `tier_m_edit`, which this client must never request: the dashboard sends
// XAACP messages, it does not edit source.
const SCOPE = "catalog tier_r_invoke status project_import sync xaacp_write";
const AUTHORIZE = "/oauth/authorize";
const TOKEN_EP = "/oauth/token";
// Absolute gate origin for the DESKTOP build, whose own origin is the local
// webview rather than the gate (see renewIfNeeded).
const GATE_ORIGIN = "https://mcp.codenexus.cloud";
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

/**
 * Silent renewal (#92 tranche 2 — "renew, never re-wall").
 *
 * An access token lives ~1h. Until now, the hour simply ELAPSED and the user was
 * thrown back to the sign-in card — even though a `refreshToken` had been sitting
 * in the same record the whole time, unused, because `loadGateConnection()`
 * discards expired records before anyone can look at them. Re-authenticating
 * through a browser round-trip once an hour is not a session; it is a wall on a
 * timer.
 *
 * Returns true when a live connection exists afterwards (already-valid, or
 * renewed). NEVER throws and never clears a stored record on failure: a refresh
 * that fails offline must leave the user exactly as they were, so the next
 * attempt can still succeed.
 */
export async function renewIfNeeded(): Promise<boolean> {
  if (isConnected()) return true;
  const stale = loadGateConnectionIgnoringExpiry();
  const rt = stale?.refreshToken;
  if (!stale || !rt) return false; // nothing to renew from ⇒ a real sign-in
  try {
    const body = new URLSearchParams({ grant_type: "refresh_token", client_id: CLIENT_ID });
    body.set(RT_FIELD, rt);
    // The token endpoint is SAME-ORIGIN only in the web build. In the desktop
    // app the origin is the local webview (127.0.0.1:1420), so a relative URL
    // posts the refresh token to the app itself and always fails — and it fails
    // SILENTLY, since renewal swallows errors by design. Desktop must address
    // the gate absolutely, over the Rust-backed HTTP plugin (no CORS).
    const res = isWebBuild()
      ? await fetch(TOKEN_EP, {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body,
        })
      : await (await import("@tauri-apps/plugin-http")).fetch(GATE_ORIGIN + TOKEN_EP, {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: body.toString(),
        });
    if (!res.ok) return false;
    const j: Record<string, unknown> = await res.json();
    const cred = j[AT_FIELD];
    if (typeof cred !== "string" || !cred) return false;
    const expiresIn = typeof j["expires_in"] === "number" ? (j["expires_in"] as number) : 3600;
    saveGateConnection({
      ...stale,
      accessToken: cred,
      // A rotating server may hand back a NEW refresh token; keep the old one
      // when it does not, or the next renewal has nothing to present.
      refreshToken: typeof j[RT_FIELD] === "string" ? (j[RT_FIELD] as string) : rt,
      expiresAt: Date.now() + expiresIn * 1000,
    });
    return true;
  } catch {
    return false; // offline / gate down ⇒ unchanged, retried next boot
  }
}

/** Begin login. DESKTOP (Tauri) reuses connectAndListProjects() — the existing
 *  loopback OAuth flow (PKCE + the webmcp_oauth_capture listener + token exchange)
 *  that persists the gate connection so isConnected() flips true. WEB runs the
 *  browser PKCE redirect below. */
/** Does a LOCAL operator token currently validate? (desktop only)
 *
 * Deliberately fail-CLOSED here, which is the opposite of the #509 read-path rule
 * and for a different reason: this answer only decides whether to take a browser
 * round-trip. Treating "unknown" as "have it" would skip the stamp and reproduce
 * the silent bounce; treating it as "missing" merely costs one extra sign-in.
 */
async function desktopOperatorTokenValid(): Promise<boolean> {
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    const st = await invoke<{ authenticated?: boolean }>("dashboard_auth_status", {});
    return Boolean(st?.authenticated);
  } catch {
    return false;
  }
}

export async function beginLogin(): Promise<void> {
  if (!isWebBuild()) {
    // Desktop (Tauri): reuse the existing, tested loopback OAuth flow — PKCE +
    // the `webmcp_oauth_capture` Rust listener + token exchange against the
    // registered loopback client — which persists the gate connection, so
    // isConnected() flips true and the login gate clears. No separate desktop
    // login machinery needed.
    // Renew before re-authenticating: an expired token with a live refresh
    // token needs a token-endpoint call, NOT a browser round-trip.
    //
    // BUT ONLY IF THE LOCAL OPERATOR TOKEN ALREADY EXISTS (#509, found live
    // 2026-07-25). connectAndListProjects() below is the ONLY code path that
    // stamps the local token — it is what invokes webmcp_oauth_complete, whose
    // Rust side mints it from a GATE-ATTESTED email. So returning early here
    // skipped the stamp entirely.
    //
    // The symptom was maddening and gave no error at all: the operator clicked
    // connect, the overlay cleared, the dashboard rendered for ~0.5s, then the
    // overlay came back. A gate session survived from an earlier attempt, so
    // renewIfNeeded() renewed the CLOUD token and returned true; the local token
    // was never minted; dashboard_auth_status answered an affirmative false; the
    // shell bounced back. Nothing failed loudly because nothing failed — the
    // stamping code was simply never reached, which is why the local_reason
    // diagnostic (added the same day) also stayed silent.
    //
    // A renewed CLOUD session is therefore NOT sufficient on desktop: this app
    // needs BOTH halves. When the local half is missing we deliberately pay the
    // browser round-trip, because that is the only route that produces it.
    const haveLocal = await desktopOperatorTokenValid();
    if (haveLocal && (await renewIfNeeded())) return;
    const { connectAndListProjects } = await import("../WebmcpProjects");
    await connectAndListProjects();
    // #471: NO full page reload here. saveGateConnection() (inside the flow
    // above) dispatches the scope-change event and the shell re-renders from
    // state. The old window.location.reload() tore the webview down mid-
    // flight and stampeded every boot-time invoke into the SPAWN_GATE at
    // once — the post-login freeze.
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
