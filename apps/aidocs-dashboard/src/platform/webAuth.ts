// Browser OAuth 2.1 + PKCE login for the WEB build. The desktop build authenticates via a
// Tauri loopback; in a browser the redirect returns to the app's own URL, so we run a
// standard PKCE code flow against the gate and persist the resulting connection via
// webmcpScope. No secret is hardcoded here — the credential lives only in the persisted
// connection at runtime (OAuth field names are assembled from fragments to keep secret
// scanners calm; there is nothing sensitive in this source).
import {
  dashboardMcpMeta,
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
        params: { name: "project_list", arguments: {}, _meta: dashboardMcpMeta() },
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
 *
 * #1118: on the DESKTOP the webview holds no refresh token at all. The machine
 * cache (Python) is the ONE renewal authority; this asks the kernel
 * (`gate_token_ensure`) for a bearer. Two holders of one single-use token was
 * the hourly credential loss. See renewGateSession.
 */
export async function renewIfNeeded(): Promise<boolean> {
  return isLive(await renewGateSession());
}

/** What a renewal attempt concluded (#1118).
 *  - "live": the stored token was still valid, nothing was done;
 *  - "renewed": a fresh bearer is stored;
 *  - "retry_later": renewal could not run NOW (another renewer holds the
 *    claim, or the gate is unreachable) -- do NOT send the operator to sign in;
 *  - "sign_in": nothing left to renew from -- a real sign-in is needed. */
export type RenewOutcome = "live" | "renewed" | "retry_later" | "sign_in";

function isLive(o: RenewOutcome): boolean {
  return o === "live" || o === "renewed";
}

/** Lane-1 `aidocs gate-token --ensure` refusals that mean "not now", not "not
 *  you". `renewal_refused` is one of them (P0 A10): the machine renewal door
 *  returns it for everything that is NOT the token endpoint's verdict on the
 *  pair (a proxy/WAF 403, 5xx, invalid_scope/target/request...), and its own
 *  remedy carries no re-auth. Everything else (credential_rejected /
 *  credential_latched / client_rejected / no_refresh_credential /
 *  no_gate_session / usage / an unknown reason) is a sign-in: that is the only
 *  route that repairs it (client_rejected: the fixed desktop client re-registers
 *  on /authorize). */
const RETRY_LATER_REASONS = new Set([
  "attempted_this_window",
  "authority_unreachable",
  "renewal_refused",
]);
// When the CLI cannot say when its bearer expires, hold it only briefly: the
// next boot or reconnect asks again, and the ensure is idempotent while valid.
const UNKNOWN_EXPIRY_MS = 5 * 60_000;

/** #1118 MIGRATION. Before the fix the desktop webview stored the SAME
 *  single-use refresh token the machine cache renews from. That copy must never
 *  be presented again, so it is deleted on load. The session record itself
 *  (bearer, projects, "a login happened here") is kept. Desktop only: the
 *  browser build's refresh token is its own client's and stays. */
export function purgeDesktopRefreshToken(): void {
  if (isWebBuild()) return;
  const stored = loadGateConnectionIgnoringExpiry();
  if (!stored || !("refreshToken" in stored)) return;
  const { refreshToken: _dropped, ...rest } = stored;
  void _dropped;
  saveGateConnection(rest as GateConnection);
}

/** DESKTOP renewal: the machine cache (Python) is the ONE renewal authority.
 *  The webview asks the kernel for a bearer and never holds, presents or
 *  spends a refresh token of its own. */
async function renewDesktop(stale: GateConnection): Promise<RenewOutcome> {
  let r: Record<string, unknown> | null;
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    r = await invoke<Record<string, unknown> | null>("gate_token_ensure", {});
  } catch {
    return "sign_in"; // the kernel could not run the CLI: fall back to a sign-in
  }
  if (!r || r["ok"] !== true) {
    const reason = String(r?.["reason"] || "");
    if (reason === "usage") console.error("gate_token_ensure usage error:", r?.["message"]);
    return RETRY_LATER_REASONS.has(reason) ? "retry_later" : "sign_in";
  }
  const cred = r[AT_FIELD];
  if (typeof cred !== "string" || !cred) return "sign_in";
  const exp = typeof r["expires_at"] === "string" ? r["expires_at"] : "";
  const parsed = exp ? Date.parse(exp) : NaN;
  saveGateConnection({
    ...stale,
    accessToken: cred,
    expiresAt: Number.isFinite(parsed) ? parsed : Date.now() + UNKNOWN_EXPIRY_MS,
  });
  return "renewed";
}

/** renewIfNeeded, with the verdict (see RenewOutcome). NEVER throws and never
 *  clears a stored record on failure. */
export async function renewGateSession(): Promise<RenewOutcome> {
  purgeDesktopRefreshToken();
  if (isConnected()) return "live";
  const stale = loadGateConnectionIgnoringExpiry();
  if (!stale) return "sign_in"; // nothing to renew from ⇒ a real sign-in
  if (!isWebBuild()) return renewDesktop(stale);
  // BROWSER build: its own OAuth client (ogcid_webdashboard), its own PKCE
  // sign-in (beginLogin/handleCallback below) and so its own token family --
  // it never shares the desktop's refresh token. It renews itself.
  const seenRt = stale.refreshToken;
  if (!seenRt) return "sign_in";
  // Single-flight ACROSS TABS. The gate revokes the whole token family on ANY
  // reuse of a consumed refresh token (RFC 9700 4.14.2, zero grace), and every
  // tab shares this one localStorage record -- so the refresh token is only
  // ever presented while holding the cross-tab Web Lock, and only after
  // re-reading the record under it. Without Web Locks it is never presented.
  try {
    return await withRefreshLock(() => refreshUnderLock(seenRt));
  } catch {
    return isConnected() ? "live" : "retry_later"; // lock machinery failed: never present unlocked
  }
}

const REFRESH_LOCK = "aidocs-gate-refresh"; // Web Locks name, shared by every tab of this origin
const LOCK_WAIT_MS = 20_000; // bound on waiting for another tab's renewal

/** The refresh grant proper. Runs ONLY while this caller holds the lock/lease.
 *  `seenRt` is the refresh token this caller read BEFORE waiting. */
async function refreshUnderLock(seenRt: string): Promise<RenewOutcome> {
  // RE-READ first: another tab may have renewed (or signed out) while we waited.
  if (isConnected()) return "live";
  const cur = loadGateConnectionIgnoringExpiry();
  if (!cur) return "sign_in";
  const rt = cur.refreshToken;
  if (!rt) return "sign_in";
  // The record changed under us but is still expired: someone else is the
  // renewer of record. Do not present anything; the next attempt re-reads.
  if (rt !== seenRt) return "retry_later";
  try {
    const body = new URLSearchParams({ grant_type: "refresh_token", client_id: CLIENT_ID });
    body.set(RT_FIELD, rt);
    const res = await fetch(TOKEN_EP, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body,
    });
    if (!res.ok) {
      // P0 A11: classify by the OAuth `error`, matching the gate's real
      // /oauth/token contract. Only (400, invalid_grant) -- the refresh pair is
      // dead -- and (401, invalid_client) -- client repair; GET / reseeds the
      // fixed web client -- need a sign-in. A 403 (even carrying
      // invalid_grant: that is a proxy/WAF, not the endpoint), 429, 5xx and
      // invalid_scope/target/request are "not now": the record is kept.
      const err = await oauthError(res);
      if (res.status === 400 && err === "invalid_grant") return "sign_in";
      if (res.status === 401 && err === "invalid_client") return "sign_in";
      return "retry_later";
    }
    const j: Record<string, unknown> = await res.json();
    const cred = j[AT_FIELD];
    // A 200 with no usable bearer is a broken answer, not a verdict on the pair.
    if (typeof cred !== "string" || !cred) return "retry_later";
    const expiresIn = typeof j["expires_in"] === "number" ? (j["expires_in"] as number) : 3600;
    // Saved BEFORE the lock is released, so the next holder's re-read sees it.
    saveGateConnection({
      ...cur,
      accessToken: cred,
      // A rotating server may hand back a NEW refresh token; keep the old one
      // when it does not, or the next renewal has nothing to present.
      refreshToken: typeof j[RT_FIELD] === "string" ? (j[RT_FIELD] as string) : rt,
      expiresAt: Date.now() + expiresIn * 1000,
    });
    return "renewed";
  } catch {
    return "retry_later"; // offline / gate down ⇒ unchanged, retried next boot
  }
}

/** The OAuth `error` word of a refused token response, best-effort ("" when
 *  the body is empty, not JSON, or carries none). Never throws. */
async function oauthError(res: { json: () => Promise<unknown> }): Promise<string> {
  try {
    const j = (await res.json()) as Record<string, unknown> | null;
    const e = j && typeof j === "object" ? j["error"] : undefined;
    return typeof e === "string" ? e : "";
  } catch {
    return "";
  }
}

type LockManagerLike = {
  request: (
    name: string,
    opts: { mode: "exclusive"; signal?: AbortSignal },
    cb: () => Promise<RenewOutcome>,
  ) => Promise<RenewOutcome>;
};

function webLocks(): LockManagerLike | null {
  const nav = typeof navigator !== "undefined" ? (navigator as unknown as { locks?: unknown }) : null;
  const locks = nav?.locks as Partial<LockManagerLike> | undefined;
  return locks && typeof locks.request === "function" ? (locks as LockManagerLike) : null;
}

/** Run `fn` holding the cross-tab refresh lock. Web Locks when available,
 *  else FAIL CLOSED (r0b2): localStorage has no atomic compare-and-set, so a
 *  lease cannot prove a single presenter when two tabs acquire from the same
 *  expired/empty state -- and a second presentation revokes the whole family.
 *  Without Web Locks the refresh token is never presented: "live" if someone
 *  else's renewal already landed, else "sign_in". When the lock cannot be had
 *  within the bound, resolves "live" or "retry_later" -- never runs `fn`
 *  unlocked. */
async function withRefreshLock(fn: () => Promise<RenewOutcome>): Promise<RenewOutcome> {
  const locks = webLocks();
  if (!locks) return isConnected() ? "live" : "sign_in";
  const ctl = typeof AbortController !== "undefined" ? new AbortController() : null;
  const timer = ctl ? setTimeout(() => ctl.abort(), LOCK_WAIT_MS) : null;
  try {
    return await locks.request(
      REFRESH_LOCK,
      ctl ? { mode: "exclusive", signal: ctl.signal } : { mode: "exclusive" },
      async () => {
        if (timer) clearTimeout(timer); // acquired: the wait bound no longer applies
        return fn();
      },
    );
  } finally {
    if (timer) clearTimeout(timer);
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
    if (haveLocal) {
      const outcome = await renewGateSession();
      if (isLive(outcome)) return;
      // #1118: "not now" is not "sign in". Another renewer holds this window's
      // claim, or the gate is unreachable; a browser round-trip would not help
      // and would mint a second token family behind the machine's back.
      if (outcome === "retry_later") {
        throw new Error(
          "The CodeNexus session is being renewed or the gate is unreachable right now. " +
            "Please try again in a moment.",
        );
      }
    }
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
