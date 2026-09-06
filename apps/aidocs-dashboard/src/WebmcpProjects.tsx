import { useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { fetch as tauriFetch } from "@tauri-apps/plugin-http";
import { loadGateConnection, saveGateConnection } from "./webmcpScope";

// WebAgent cloud projects (DoD #6 "shows the user's projects via the gate API, no
// direct remote DB"). Runs the gate's OAuth Authorization-Code + PKCE flow — the
// only credentialed path the gate allows — and then calls the project_list MCP
// tool with the issued bearer token. The codenexus login happens IN THE BROWSER
// at the gate's /oauth/authorize page; a tiny loopback listener (Rust command
// `webmcp_oauth_capture`) catches the redirect code. HTTPS calls go through the
// Tauri HTTP plugin (Rust-backed → no CORS on the gate).

const GATE = "https://mcp.codenexus.cloud";
const RESOURCE = GATE + "/v1/mcp";
const CLIENT_ID = "ogcid_desktop"; // WELL-KNOWN public PKCE client, re-seeded by the gate every boot (a random one-shot id died on an oauth_clients rebuild and locked the operator out, 2026-07-25)
const REDIRECT_PORT = 8765;
const REDIRECT_URI = "http://127.0.0.1:" + REDIRECT_PORT + "/callback";
// Kept in lockstep with platform/webAuth.ts — `sync` (#1002) for /sync/events and
// /v1/backlog; `tier_m_edit` stays out (the gate withholds it from this client).
// `xaacp_write` (#1015) is the messaging grant. It is DISTINCT from
// `tier_m_edit`, which this client must never request: the dashboard sends
// XAACP messages, it does not edit source.
const SCOPE = "catalog tier_r_invoke status project_import sync xaacp_write";

type Project = { project_id: string; name: string; source: string; is_default: boolean; current: boolean };

function b64url(bytes: Uint8Array): string {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function makePkce(): Promise<{ verifier: string; challenge: string }> {
  const rnd = new Uint8Array(48);
  crypto.getRandomValues(rnd);
  const verifier = b64url(rnd);
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return { verifier, challenge: b64url(new Uint8Array(digest)) };
}

// #471 single-flight: ONE OAuth attempt at a time. A second click while the
// first listener still held :8765 used to open ANOTHER browser window first
// (open_url fires before the capture is awaited) and then fail to bind —
// N clicks = N browser windows + N failed listeners. Concurrent callers now
// share the in-flight attempt instead of starting a new one.
let oauthInFlight: Promise<Project[]> | null = null;

/** True while an OAuth sign-in attempt is live (for pending UI states). */
export function oauthLoginPending(): boolean {
  return oauthInFlight !== null;
}



export async function connectAndListProjects(): Promise<Project[]> {
  if (oauthInFlight) return oauthInFlight;
  const attempt = runOauthFlow().finally(() => {
    oauthInFlight = null;
  });
  oauthInFlight = attempt;
  return attempt;
}

async function runOauthFlow(): Promise<Project[]> {
  const { verifier, challenge } = await makePkce();
  const state = b64url(crypto.getRandomValues(new Uint8Array(16)));
  const authorizeUrl =
    GATE +
    "/oauth/authorize?" +
    new URLSearchParams({
      client_id: CLIENT_ID,
      redirect_uri: REDIRECT_URI,
      response_type: "code",
      code_challenge_method: "S256",
      code_challenge: challenge,
      scope: SCOPE,
      state,
      resource: RESOURCE,
    }).toString();

  // Start the loopback listener FIRST (don't await fully — it blocks until
  // the redirect arrives). #471: give it a short beat so a BIND failure
  // (port 8765 busy — another app or a leftover listener) surfaces as an
  // error HERE instead of opening a browser tab that can never complete.
  const capture = invoke<string>("webmcp_oauth_capture", { port: REDIRECT_PORT, timeoutSecs: 300 });
  const probe = await Promise.race<{ ok: string } | { err: unknown } | null>([
    capture.then(
      (v) => ({ ok: v }),
      (e) => ({ err: e }),
    ),
    new Promise<null>((resolve) => setTimeout(() => resolve(null), 250)),
  ]);
  if (probe && "err" in probe) {
    throw new Error(
      "Could not start the sign-in listener on 127.0.0.1:" +
        REDIRECT_PORT +
        " — " +
        String(probe.err),
    );
  }
  await invoke("open_url", { url: authorizeUrl });
  const captured = probe && "ok" in probe ? probe.ok : await capture; // "code|state"
  const [code, gotState] = String(captured).split("|");
  if (gotState !== state) throw new Error("OAuth state mismatch (possible CSRF) — aborted");
  if (!code) throw new Error("No authorization code returned");

  // Exchange code → bearer token IN THE RUST KERNEL (2026-07-22).
  //
  // The exchange used to run here, in the webview, and the local operator token
  // was never stamped — so a successful CodeNexus sign-in satisfied the cloud
  // gate and left the DESKTOP gate false, bouncing the operator to a local
  // password form forever (#207 §3). Moving the exchange into Rust means the
  // principal's email arrives from the gate over TLS instead of via this
  // context, which is what lets the local mint trust it (#404).
  const done = await invoke<{
    access_token: string; refresh_token?: string; expires_in?: number;
    scope?: string; email?: string; local_stamped?: boolean; local_reason?: string;
  }>("webmcp_oauth_complete", {
    gate: GATE,
    clientId: CLIENT_ID,
    code,
    verifier,
    redirectUri: REDIRECT_URI,
    resource: RESOURCE,
  });
  const tok = {
    access_token: done.access_token,
    refresh_token: done.refresh_token,
    expires_in: done.expires_in,
    scope: done.scope,
  };
  const accessToken = tok.access_token;

  // PERSIST THE AUTHENTICATION FIRST — before any data fetch can fail.
  //
  // This used to save only AFTER project_list succeeded, so a 4xx/5xx on that
  // secondary call threw past saveGateConnection and discarded a PERFECTLY GOOD
  // login. Authentication and project listing are independent concerns; only
  // the former decides whether you are signed in.
  const baseConn = {
    accessToken,
    refreshToken: tok.refresh_token,
    scope: tok.scope,
    expiresAt: Date.now() + (Number(tok.expires_in) || 3600) * 1000,
  };
  saveGateConnection({ ...baseConn, projects: loadGateConnection()?.projects ?? [] });

  // SURFACE A FAILED LOCAL STAMP (#474 / #509). The Rust kernel already computes
  // exactly why the local operator token could not be minted and returns it as
  // local_reason — and this file DECLARED both fields in the type above and then
  // never read either one. So a failed stamp was silently discarded, and the
  // operator saw only the symptom: the dashboard appears for ~0.5s and snaps back
  // to the connect screen, because dashboard_auth_status has no token to validate.
  // That is the same defect shape as #504's one-size-fits-all broker banner — the
  // diagnosis existed and was thrown away.
  //
  // Thrown AFTER saveGateConnection deliberately: the CLOUD session is already
  // persisted and stays valid, so this reports a real local failure without
  // discarding a good gate login (the #207 §3 bug that must never come back).
  if (done.local_stamped === false) {
    const why = (done.local_reason || "").trim() || "unknown";
    throw new Error(
      "CodeNexus verified you, but the LOCAL operator token could not be minted, " +
        "so this app cannot stay signed in. Reason: " +
        why,
    );
  }

  // Call the project_list MCP tool with the bearer token.
  const mcpRes = await tauriFetch(RESOURCE, {
    method: "POST",
    headers: { Authorization: "Bearer " + accessToken, "Content-Type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/call", params: { name: "project_list", arguments: {} } }),
  });
  if (!mcpRes.ok) throw new Error("project_list failed (" + mcpRes.status + ")");
  const rpc = await mcpRes.json();
  const text = rpc?.result?.content?.[0]?.text;
  const parsed = text ? JSON.parse(text) : {};
  const projects = (parsed.projects || []) as Project[];
  // Upgrade the already-persisted connection with the project list.
  saveGateConnection({ ...baseConn, projects });
  return projects;
}

export function WebmcpProjects() {
  // Seed from the persisted connection so reopening the popup shows the projects
  // without reconnecting (the connection stays active until the token expires).
  const [projects, setProjects] = useState<Project[] | null>(() => loadGateConnection()?.projects ?? null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function connect() {
    setBusy(true);
    setError(null);
    try {
      setProjects(await connectAndListProjects());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load projects");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rounded-xl border border-castle-line bg-black/20 p-3 text-sm">
      <div className="mb-2 flex items-center justify-between">
        <span className="font-semibold text-slate-200">Cloud projects</span>
        <button
          type="button"
          onClick={connect}
          disabled={busy}
          className="rounded border border-castle-allow/40 bg-castle-allow/15 px-2 py-1 text-[11px] font-medium text-white hover:bg-castle-allow/20 disabled:opacity-50"
        >
          {busy ? "Connecting…" : projects ? "Refresh" : "Connect via gate"}
        </button>
      </div>
      {error ? <p className="text-xs text-castle-deny">{error}</p> : null}
      {projects ? (
        projects.length ? (
          <ul className="space-y-1">
            {projects.map((p) => (
              <li key={p.project_id} className="flex items-center justify-between text-xs">
                <span className="truncate text-slate-300">{p.name}</span>
                <span className="ml-2 rounded bg-white/[0.06] px-1.5 py-0.5 text-[10px] text-castle-mute">
                  {p.current ? "current" : p.source}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-xs text-castle-mute">No projects on this gate yet.</p>
        )
      ) : (
        <p className="text-[11px] text-castle-mute">
          Authorize via codenexus.cloud (opens your browser) to list the projects this gate serves —
          fetched through the gate API, never the database directly.
        </p>
      )}
    </div>
  );
}
