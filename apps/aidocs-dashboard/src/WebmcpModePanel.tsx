import { useCallback, useEffect, useState } from "react";

import { operatorLogout } from "./dashboardApi";
import { beginLogin } from "./platform/webAuth";
import { ModeSwitcher } from "./ModeSwitcher";
import { UserMenu } from "./UserMenu";
import { WebModePanel } from "./WebModePanel";
import {
  getMode,
  loadGateConnection,
  loadGateConnectionIgnoringExpiry,
  onScopeChange,
  saveGateConnection,
  setMode,
  type Mode,
} from "./webmcpScope";

// Desktop mode + account control (operator ruling 2026-08-31).
//
// WHAT THIS FILE WAS, AND WHY IT HAD TO GO
// ----------------------------------------
// It rendered ONE header button labelled "Local"/"WebAgent" whose only action
// was to open a "WebAgent access" modal — an email+password form posting to
// `https://codenexus.cloud/api/dashboard/login`, plus Google/GitHub buttons and
// a "Create account" tab. Three separate defects lived in that shape:
//
//   1. THE BUTTON THAT SAYS "LOCAL" OPENED A LOGIN PROMPT. The operator's words:
//      "when i click 'local' i see this BS". Nothing about selecting a mode
//      requires a credential, and the modal fired unconditionally.
//   2. IT WAS A SECOND IDENTITY STORE. The form's result was written to
//      `localStorage["aidocs.webmcp.session"]`, a key NOTHING else in the app
//      reads: not `webmcpScope`'s gate connection, not `dashboard_auth_status`,
//      not the machine cache. So an operator who had ALREADY completed the
//      CodeNexus OAuth sign-in still had `session === null` here and was shown
//      the credential form again — "i don't want to log in 400009342u9453789543789
//      times. once."
//   3. IT WAS A LOCAL AUTHORITY DOOR. A password box on this machine is the
//      second source of truth #207 forbids and #516 removed at real cost.
//      Operator ruling: "there is no 'locally created user' — users are stored
//      on codenexus.cloud, not logged in no access to aidocs"; "having a local
//      operator makes the whole process swiss cheese".
//
// The panel's copy even asserted "The local dashboard runs as a solo superadmin
// without login." That sentence was the DEFECT, not the contract. Local means
// THIS APP (the Tauri exe) rather than the browser console — it has never meant
// a local account, and it must not grant authority to whoever holds the exe.
//
// WHAT IT IS NOW
// --------------
// The same two controls the web build already had, and nothing else:
//   * ModeSwitcher — picks Local / WebAgent / CloudAgent. Pure state, no auth.
//   * UserMenu     — the signed-in identity + the SIGN OUT that never existed
//                    ("where the fuck is logout?"). The revoke machinery has
//                    been in the kernel the whole time (`operator_logout` ->
//                    revoke_cached_operator_token: revokes the token row and
//                    clears BOTH the in-process cache and the shared machine
//                    cache); it simply had no button anywhere in the UI.
// Signed out, the panel offers exactly ONE door: the CodeNexus browser OAuth
// flow, the same `beginLogin()` the login wall uses. There is no other door.

const LEGACY_SESSION_KEY = "aidocs.webmcp.session";

/** The identity to show. Prefers the LIVE connection, falls back to the stored
 *  (possibly expired) record so an aged-out access token still renders "you",
 *  never an anonymous shell — expiry is not a sign-out (#92). */
function connectionIdentity(): { email?: string; scope?: string; live: boolean } {
  const live = loadGateConnection();
  if (live) return { email: live.email, scope: live.scope, live: true };
  const stale = loadGateConnectionIgnoringExpiry();
  return { email: stale?.email, scope: stale?.scope, live: false };
}

export function WebmcpModePanel() {
  // WEB build: render the browser PKCE mode/connect panel — the desktop Tauri flow
  // below never runs in a browser. __AIDOCS_WEB__ is a compile-time constant, so hook
  // order stays consistent within each build.
  if (__AIDOCS_WEB__) return <WebModePanel />;

  const [mode, setModeState] = useState<Mode>(() => getMode());
  const [identity, setIdentity] = useState(() => connectionIdentity());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const resync = useCallback(() => {
    setModeState(getMode());
    setIdentity(connectionIdentity());
  }, []);
  useEffect(() => onScopeChange(resync), [resync]);

  // Selecting a mode is a VIEW choice, not an authentication event. It writes
  // the mode and stops. Whether the gate session behind WebAgent is still live
  // is reported (below) — it is never demanded here, and never with a form.
  function pick(m: Mode) {
    setError(null);
    setMode(m);
    setModeState(m);
  }

  async function signIn() {
    if (busy) return; // single-flight (#471 OAuth cascade)
    setBusy(true);
    setError(null);
    try {
      await beginLogin();
      resync();
    } catch (e) {
      // Surface and STOP. Never loop against a rejected credential: the gate
      // runs CrowdSec's http-generic-401-bf and seven 401s ban this machine's
      // IP for four hours — which also blocks the sign-in that would fix it.
      setError(e instanceof Error ? e.message : "Sign-in failed");
    } finally {
      setBusy(false);
    }
  }

  async function signOut() {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      // 1. The kernel: revoke the token row, clear the in-process cache AND the
      //    shared machine cache (~/.aidocs/operator_token.json).
      await operatorLogout();
    } catch (e) {
      // A revoke that could not reach the CLI must still clear THIS app's
      // session — otherwise "sign out" silently leaves you signed in.
      setError(
        (e instanceof Error ? e.message : String(e)) +
          " — the local token may still be cached; run `aidocs dashboard-auth-logout`.",
      );
    } finally {
      // 2. The webview: drop the gate connection. Removing the record (rather
      //    than expiring it) restores the first-login wall (#404) — an expired
      //    record renews, a MISSING one re-walls, which is what sign-out means.
      saveGateConnection(null);
      // 3. Sweep the retired second-identity record so a stale row from an
      //    older build can never present a signed-in identity again.
      try {
        localStorage.removeItem(LEGACY_SESSION_KEY);
      } catch {
        /* private-mode storage — nothing to sweep */
      }
      setMode("local");
      resync();
      setBusy(false);
    }
  }

  const needsLiveGate = mode !== "local";
  const signedIn = Boolean(identity.email) || Boolean(loadGateConnectionIgnoringExpiry());

  return (
    <div className="hidden items-center gap-2 lg:flex">
      <ModeSwitcher mode={mode} onPick={pick} />
      {signedIn ? (
        <UserMenu
          email={identity.email}
          scope={identity.scope}
          onSignOut={() => void signOut()}
        />
      ) : (
        <button
          type="button"
          disabled={busy}
          onClick={() => void signIn()}
          className="h-10 rounded-xl border border-castle-allow/40 bg-castle-allow/15 px-3 text-sm font-semibold text-castle-allow transition hover:bg-castle-allow/25 disabled:opacity-50"
        >
          {busy ? "Opening CodeNexus…" : "Sign in with CodeNexus"}
        </button>
      )}
      {/* An aged-out gate session in a cloud mode is STATED, not silently
          re-prompted. The operator decides when to spend a browser round-trip;
          the app never opens one behind their back. */}
      {signedIn && needsLiveGate && !identity.live ? (
        <button
          type="button"
          disabled={busy}
          onClick={() => void signIn()}
          title="The CodeNexus gate session has expired. Cloud calls will fail until it is renewed."
          className="h-10 rounded-xl border border-castle-warn/40 bg-castle-warn/10 px-3 text-xs font-semibold text-castle-warn transition hover:bg-castle-warn/20 disabled:opacity-50"
        >
          {busy ? "Reconnecting…" : "Gate session expired — reconnect"}
        </button>
      ) : null}
      {error ? (
        <span role="alert" className="max-w-[280px] truncate text-xs text-castle-deny" title={error}>
          {error}
        </span>
      ) : null}
    </div>
  );
}
