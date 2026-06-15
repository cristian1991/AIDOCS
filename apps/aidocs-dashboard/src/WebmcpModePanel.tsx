import { useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { fetch as tauriFetch } from "@tauri-apps/plugin-http";
import { WebmcpProjects, connectAndListProjects } from "./WebmcpProjects";
import { setScope, saveGateConnection, getScope, onScopeChange, loadGateConnection } from "./webmcpScope";

// WebMCP mode + login (DoD #6). The LOCAL dashboard runs as a solo superadmin
// with NO login (dev/solo). WebMCP (cloud) mode authenticates against the
// codenexus.cloud identity (role + active webmcp seat) and is what the gate
// authorizes. This panel is the local↔webmcp switcher + the login/create-account
// popup. It talks ONLY to codenexus public JSON endpoints (no remote DB):
//   POST /api/dashboard/login   — verify creds → identity + webmcp entitlement
//   POST /api/auth/register     — create a webmcp account (becomes an org OWNER)
// Requires connect-src https://codenexus.cloud in the Tauri CSP.

const CODENEXUS_BASE = "https://codenexus.cloud";
const STORE_KEY = "aidocs.webmcp.session";

export type WebmcpUser = {
  id: string;
  email: string;
  name: string | null;
  role: string; // codenexus platform role (USER/ADMIN/SUPER_ADMIN)
};
// Multi-org (M1): a user can belong to several orgs with a TeamRole in each. The
// ACTIVE org is bound on the gate via org_select; this list is the picker/status.
export type WebmcpOrg = {
  id: string;
  name: string;
  slug: string;
  role: string; // TeamRole in this org: OWNER/ADMIN/MEMBER/VIEWER
  entitled: boolean;
  seats: number | null;
  members: number;
  isOwner: boolean;
};
export type WebmcpSession = {
  user: WebmcpUser;
  webmcp: { entitled: boolean };
  orgs: WebmcpOrg[];
};

export function loadSession(): WebmcpSession | null {
  try {
    const raw = localStorage.getItem(STORE_KEY);
    return raw ? (JSON.parse(raw) as WebmcpSession) : null;
  } catch {
    return null;
  }
}

async function postJson(path: string, body: unknown): Promise<{ ok: boolean; status: number; data: any }> {
  // Use the Tauri HTTP plugin (Rust-backed) — NOT the webview's native fetch:
  // a webview fetch to codenexus.cloud is cross-origin (and in `tauri dev` the
  // origin is http://127.0.0.1:1420, not the prod tauri:// origin), so native
  // fetch is CORS-blocked. The Rust-backed plugin makes the request server-side,
  // bypassing CORS entirely. Allowed by the http capability scope.
  const res = await tauriFetch(CODENEXUS_BASE + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  return { ok: res.ok, status: res.status, data };
}

// Social login (Google/GitHub) via codenexus. NextAuth social is cookie-based, so
// it can't reach the desktop directly: the agent opens the system browser to the
// codenexus /dashboard-auth bridge, the user signs in there, and the bridge bounces
// a short-lived signed code to the loopback (the same Rust webmcp_oauth_capture
// listener), which is exchanged at /api/dashboard/social-exchange for identity.
export async function connectViaSocial(provider: "google" | "github"): Promise<WebmcpSession> {
  const rnd = new Uint8Array(16);
  crypto.getRandomValues(rnd);
  const state = Array.from(rnd, (b) => b.toString(16).padStart(2, "0")).join("");
  const port = 8765;
  const capture = invoke<string>("webmcp_oauth_capture", { port, timeoutSecs: 300 });
  await invoke("open_url", {
    url: `${CODENEXUS_BASE}/dashboard-auth?provider=${provider}&port=${port}&state=${encodeURIComponent(state)}`,
  });
  const [code, gotState] = String(await capture).split("|");
  if (gotState !== state) throw new Error("Sign-in state mismatch (aborted)");
  if (!code) throw new Error("No authorization code returned");
  const res = await tauriFetch(`${CODENEXUS_BASE}/api/dashboard/social-exchange`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "Social sign-in failed");
  return { user: data.user, webmcp: data.webmcp, orgs: data.orgs ?? [] };
}

export function WebmcpModePanel() {
  const [session, setSession] = useState<WebmcpSession | null>(null);
  const [open, setOpen] = useState(false);
  // The badge reflects the active DASHBOARD SCOPE (where data is sourced),
  // distinct from whether a session exists. A user can be signed in but still
  // viewing Local scope until they flip the switch inside the modal.
  const [scope, setScopeState] = useState(() => getScope());
  useEffect(() => onScopeChange(() => setScopeState(getScope())), []);

  useEffect(() => {
    setSession(loadSession());
  }, []);

  const onWeb = scope === "web";

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        title={onWeb ? "WebMCP cloud scope — manage login / switch to Local" : "Local scope — log in to use WebMCP"}
        className={
          "hidden h-10 items-center gap-2 rounded-xl border px-3 text-sm transition lg:flex " +
          (onWeb
            ? "border-castle-allow/40 bg-castle-allow/10 text-white hover:bg-castle-allow/15"
            : "border-castle-line bg-black/20 text-castle-mute hover:bg-black/30 hover:text-slate-200")
        }
      >
        <span
          className={
            "h-2 w-2 rounded-full " + (onWeb ? "bg-castle-allow" : "bg-castle-mute/60")
          }
        />
        <span className="font-semibold">{onWeb ? "WebMCP" : "Local"}</span>
        {onWeb && session ? (
          <span className="max-w-[140px] truncate text-castle-mute">{session.user.email}</span>
        ) : null}
      </button>
      {open ? (
        <WebmcpModal
          session={session}
          onClose={() => setOpen(false)}
          onSession={(s) => {
            if (s) localStorage.setItem(STORE_KEY, JSON.stringify(s));
            else localStorage.removeItem(STORE_KEY);
            setSession(s);
          }}
        />
      ) : null}
    </>
  );
}

function WebmcpModal({
  session,
  onClose,
  onSession,
}: {
  session: WebmcpSession | null;
  onClose: () => void;
  onSession: (s: WebmcpSession | null) => void;
}) {
  const [tab, setTab] = useState<"login" | "register">("login");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function doLogin(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const { ok, data } = await postJson("/api/dashboard/login", { email, password });
      if (!ok) {
        setError(data?.error || "Login failed");
        return;
      }
      onSession({ user: data.user, webmcp: data.webmcp, orgs: data.orgs ?? [] });
      // Keep the modal open so the signed-in view (org roster / seats) shows
      // immediately; the user closes via ✕ or "Log out".
    } catch {
      setError("Could not reach codenexus.cloud");
    } finally {
      setBusy(false);
    }
  }

  async function doRegister(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const reg = await postJson("/api/auth/register", { name, email, password });
      if (!reg.ok) {
        setError(reg.data?.error || "Account creation failed");
        return;
      }
      // Newly registered: log straight in to pick up identity + entitlement.
      const { ok, data } = await postJson("/api/dashboard/login", { email, password });
      if (!ok) {
        setError("Account created — please log in.");
        setTab("login");
        return;
      }
      onSession({ user: data.user, webmcp: data.webmcp, orgs: data.orgs ?? [] });
      // Keep the modal open so the signed-in view (org roster / seats) shows
      // immediately; the user closes via ✕ or "Log out".
    } catch {
      setError("Could not reach codenexus.cloud");
    } finally {
      setBusy(false);
    }
  }

  async function doSocial(provider: "google" | "github") {
    setBusy(true);
    setError(null);
    try {
      onSession(await connectViaSocial(provider));
      // modal stays open → signed-in view shows the org roster
    } catch (e) {
      setError(e instanceof Error ? e.message : "Social sign-in failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 grid place-items-center bg-black/60 p-4"
      onClick={onClose}
      role="presentation"
    >
      <div
        className="w-full max-w-md rounded-2xl border border-castle-line bg-castle-panel p-6 shadow-castle-glow"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-1 flex items-center justify-between">
          <h2 className="text-lg font-black tracking-tight text-white">WebMCP access</h2>
          <button type="button" onClick={onClose} className="text-castle-mute hover:text-white">
            ✕
          </button>
        </div>
        <p className="mb-4 text-xs text-castle-mute">
          The local dashboard runs as a solo superadmin without login. Sign in with your
          codenexus.cloud account to use WebMCP (cloud) mode.
        </p>

        {session ? (
          <div className="space-y-3">
            <div className="rounded-xl border border-castle-line bg-black/20 p-3 text-sm">
              <div className="text-slate-200">{session.user.name || session.user.email}</div>
              <div className="text-xs text-castle-mute">{session.user.email}</div>
              <div className="mt-2 flex flex-wrap gap-2 text-[11px]">
                <span
                  className={
                    "rounded px-2 py-0.5 " +
                    (session.webmcp.entitled
                      ? "bg-castle-allow/15 text-castle-allow"
                      : "bg-castle-deny/15 text-castle-deny")
                  }
                >
                  {session.webmcp.entitled ? "webmcp enabled" : "no webmcp seat"}
                </span>
              </div>
              {!session.webmcp.entitled ? (
                <p className="mt-2 text-[11px] text-castle-deny">
                  No active WebMCP seat in any of your orgs. Ask an org owner/admin to add you,
                  or upgrade a plan on codenexus.cloud.
                </p>
              ) : null}
            </div>
            {session.orgs.length ? (
              <div className="rounded-xl border border-castle-line bg-black/20 p-3 text-sm">
                <div className="mb-2 font-semibold text-slate-200">Your organizations</div>
                <ul className="space-y-1">
                  {session.orgs.map((o) => (
                    <li key={o.id} className="flex items-center justify-between gap-2 text-xs">
                      <span className="truncate text-slate-300">{o.name}</span>
                      <span className="flex shrink-0 items-center gap-1">
                        <span className="rounded bg-white/[0.06] px-1.5 py-0.5 text-[10px] text-castle-mute">
                          {o.role.toLowerCase()}
                        </span>
                        <span className="rounded bg-white/[0.06] px-1.5 py-0.5 text-[10px] text-castle-mute">
                          {o.members} {o.members === 1 ? "member" : "members"}
                          {o.seats ? ` · ${o.seats} seats` : ""}
                        </span>
                        {o.entitled ? (
                          <span className="rounded bg-castle-allow/15 px-1.5 py-0.5 text-[10px] text-castle-allow">
                            webmcp
                          </span>
                        ) : null}
                      </span>
                    </li>
                  ))}
                </ul>
                <p className="mt-2 text-[10px] text-castle-mute">
                  Pick the active org in the WebMCP chat / dashboard via org_select. Org data via
                  the codenexus API (no direct DB).
                </p>
              </div>
            ) : null}
            {session.webmcp.entitled ? <WebmcpProjects /> : null}
            {session.webmcp.entitled ? <SwitchToWebmcpButton onSwitched={onClose} /> : null}
            <button
              type="button"
              onClick={() => {
                onSession(null);
                saveGateConnection(null);
                setScope("local");
                onClose();
              }}
              className="w-full rounded-xl border border-castle-line bg-black/20 px-4 py-2 text-sm text-slate-200 hover:bg-black/30"
            >
              Log out → return to Local mode
            </button>
          </div>
        ) : (
          <>
            <div className="mb-3 flex gap-1 rounded-xl border border-castle-line bg-black/20 p-1 text-sm">
              {(["login", "register"] as const).map((t) => (
                <button
                  key={t}
                  type="button"
                  onClick={() => {
                    setTab(t);
                    setError(null);
                  }}
                  className={
                    "flex-1 rounded-lg px-3 py-1.5 transition " +
                    (tab === t ? "bg-castle-allow/15 text-white" : "text-castle-mute hover:text-slate-200")
                  }
                >
                  {t === "login" ? "Log in" : "Create account"}
                </button>
              ))}
            </div>
            {error ? <p className="mb-2 text-xs text-castle-deny">{error}</p> : null}
            <form onSubmit={tab === "login" ? doLogin : doRegister} className="space-y-2">
              {tab === "register" ? (
                <input
                  className="w-full rounded-lg border border-castle-line bg-black/20 px-3 py-2 text-sm text-slate-100 placeholder:text-castle-mute"
                  placeholder="Name (optional)"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
              ) : null}
              <input
                className="w-full rounded-lg border border-castle-line bg-black/20 px-3 py-2 text-sm text-slate-100 placeholder:text-castle-mute"
                type="email"
                placeholder="Email"
                value={email}
                required
                onChange={(e) => setEmail(e.target.value)}
              />
              <input
                className="w-full rounded-lg border border-castle-line bg-black/20 px-3 py-2 text-sm text-slate-100 placeholder:text-castle-mute"
                type="password"
                placeholder={tab === "register" ? "Password (min 8)" : "Password"}
                value={password}
                required
                minLength={tab === "register" ? 8 : undefined}
                onChange={(e) => setPassword(e.target.value)}
              />
              <button
                type="submit"
                disabled={busy}
                className="w-full rounded-xl border border-castle-allow/40 bg-castle-allow/15 px-4 py-2 text-sm font-semibold text-white hover:bg-castle-allow/20 disabled:opacity-50"
              >
                {busy ? "…" : tab === "login" ? "Log in to WebMCP" : "Create account & sign in"}
              </button>
            </form>
            <div className="my-3 flex items-center gap-2 text-[10px] uppercase tracking-widest text-castle-mute">
              <span className="h-px flex-1 bg-castle-line" />
              or continue with
              <span className="h-px flex-1 bg-castle-line" />
            </div>
            <div className="grid grid-cols-2 gap-2">
              <button
                type="button"
                disabled={busy}
                onClick={() => doSocial("google")}
                className="flex items-center justify-center gap-2 rounded-lg border border-castle-line bg-black/20 px-3 py-2 text-sm text-slate-200 hover:bg-black/30 disabled:opacity-50"
              >
                <svg className="h-4 w-4" viewBox="0 0 24 24">
                  <path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z" />
                  <path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" />
                  <path fill="#FBBC05" d="M5.84 14.09A6.6 6.6 0 0 1 5.49 12c0-.73.13-1.43.35-2.09V7.07H2.18A11 11 0 0 0 1 12c0 1.78.43 3.45 1.18 4.93l3.66-2.84z" />
                  <path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84C6.71 7.31 9.14 5.38 12 5.38z" />
                </svg>
                Google
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => doSocial("github")}
                className="flex items-center justify-center gap-2 rounded-lg border border-castle-line bg-black/20 px-3 py-2 text-sm text-slate-200 hover:bg-black/30 disabled:opacity-50"
              >
                <svg className="h-4 w-4" fill="currentColor" viewBox="0 0 24 24">
                  <path fillRule="evenodd" clipRule="evenodd" d="M12 2C6.477 2 2 6.484 2 12.017c0 4.425 2.865 8.18 6.839 9.504.5.092.682-.217.682-.483 0-.237-.008-.868-.013-1.703-2.782.605-3.369-1.343-3.369-1.343-.454-1.158-1.11-1.466-1.11-1.466-.908-.62.069-.608.069-.608 1.003.07 1.531 1.032 1.531 1.032.892 1.53 2.341 1.088 2.91.832.092-.647.35-1.088.636-1.338-2.22-.253-4.555-1.113-4.555-4.951 0-1.093.39-1.988 1.029-2.688-.103-.253-.446-1.272.098-2.65 0 0 .84-.27 2.75 1.026A9.564 9.564 0 0 1 12 6.844c.85.004 1.705.115 2.504.337 1.909-1.296 2.747-1.027 2.747-1.027.546 1.379.202 2.398.1 2.651.64.7 1.028 1.595 1.028 2.688 0 3.848-2.339 4.695-4.566 4.943.359.309.678.92.678 1.855 0 1.338-.012 2.419-.012 2.747 0 .268.18.58.688.482A10.019 10.019 0 0 0 22 12.017C22 6.484 17.522 2 12 2z" />
                </svg>
                GitHub
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

// Switching the dashboard to WebMCP needs a live gate OAuth token (the cloud
// snapshot is fetched with it). Signing in establishes IDENTITY, not a gate
// token, so this button runs the PKCE OAuth flow first if no valid connection
// exists, then flips the scope. If already connected it just flips.
function SwitchToWebmcpButton({ onSwitched }: { onSwitched: () => void }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  async function go() {
    setBusy(true);
    setErr(null);
    try {
      if (!loadGateConnection()?.accessToken) {
        await connectAndListProjects(); // PKCE OAuth → persists the gate connection
      }
      setScope("web");
      onSwitched();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Could not connect to the gate");
    } finally {
      setBusy(false);
    }
  }
  return (
    <div>
      <button
        type="button"
        onClick={go}
        disabled={busy}
        className="w-full rounded-xl border border-castle-allow/40 bg-castle-allow/15 px-4 py-2 text-sm font-semibold text-white hover:bg-castle-allow/20 disabled:opacity-50"
      >
        {busy ? "Connecting to gate…" : "Switch dashboard to WebMCP (cloud) →"}
      </button>
      {err ? <p className="mt-1 text-xs text-castle-deny">{err}</p> : null}
    </div>
  );
}
