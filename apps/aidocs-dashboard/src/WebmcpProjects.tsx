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
const CLIENT_ID = "ogcid_59rkyVEDyb5p3S8XWCtgZA"; // registered public/PKCE client (loopback:8765)
const REDIRECT_PORT = 8765;
const REDIRECT_URI = "http://127.0.0.1:" + REDIRECT_PORT + "/callback";
const SCOPE = "catalog tier_r_invoke status project_import";

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

  // Exchange code → bearer token (Tauri HTTP plugin → Rust-backed, no CORS).
  const tokRes = await tauriFetch(GATE + "/oauth/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      client_id: CLIENT_ID,
      code,
      redirect_uri: REDIRECT_URI,
      code_verifier: verifier,
      resource: RESOURCE,
    }).toString(),
  });
  if (!tokRes.ok) throw new Error("Token exchange failed (" + tokRes.status + ")");
  const tok = await tokRes.json();
  const accessToken = tok.access_token as string;

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
  // Persist the connection so it survives the popup closing (issue: had to
  // reconnect every time). Expired tokens are dropped by loadGateConnection.
  saveGateConnection({
    accessToken,
    refreshToken: tok.refresh_token,
    scope: tok.scope,
    projects,
    expiresAt: Date.now() + (Number(tok.expires_in) || 3600) * 1000,
  });
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
