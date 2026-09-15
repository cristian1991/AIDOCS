import { useState } from "react";
import { dashboardAuthStatus } from "./dashboardApi";
import { beginLogin } from "./platform/webAuth";
import logoUrl from "./cn-logo.svg";

/**
 * Desktop operator sign-in — CODENEXUS ONLY (operator ruling 2026-07-25).
 *
 * "local dashboard" means THIS APP (the Tauri exe) as opposed to the web
 * dashboard in a browser. It does NOT mean a local account. There is no local
 * account, no local password, and no local identity authority: accounts, perms
 * and projects live on CodeNexus (#207), and this screen authenticates against
 * CodeNexus like everything else.
 *
 * WHAT THIS REPLACED, AND WHY IT WAS BROKEN BY CONSTRUCTION:
 * this screen used to offer email+password, which the `dashboard-login` CLI
 * validates against a LOCAL bcrypt store. But `--method codenexus` requires a
 * gate token and takes NO password, so there was never an email+password path
 * to CodeNexus. For an operator whose account lives in the cloud the form could
 * not succeed with the CORRECT password — and it reported "invalid email or
 * password", sending them to reset a credential that was never being checked.
 * A later fix made the message honest ("this looks like a CodeNexus identity —
 * sign in with CodeNexus") and thereby made things worse in one exact way: it
 * named the exit while offering no way to take it. That is what this fixes.
 *
 * THE MACHINERY IS NOT NEW — it was merely unreachable from here. On desktop,
 * beginLogin() runs the tested loopback PKCE flow, and `webmcp_oauth_complete`
 * performs the code->token exchange IN THE RUST KERNEL, so the principal's email
 * arrives from the gate over TLS and never through this webview. That is what
 * lets the local mint trust it (#404: no token without a verified principal),
 * and it stamps the operator token this app then rides for 30 days (#509).
 *
 * DO NOT re-add a password field here. A locally-hashed credential would make
 * this machine an identity authority — the second-source-of-truth that #207
 * forbids and that #516 removed at real cost.
 */
export function DesktopLoginView({
  projectRoot,
  onAuthenticated,
}: {
  projectRoot?: string;
  onAuthenticated: () => void;
}) {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  void projectRoot; // kept for call-site compatibility; the gate resolves identity

  async function signIn() {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      // Desktop branch: renews first when a refresh token is still live, so an
      // expired access token costs a token-endpoint call rather than a browser
      // round-trip (#92 "renew, never re-wall").
      await beginLogin();
      // VERIFY BEFORE CLAIMING (#509, found live 2026-07-25). onAuthenticated()
      // flips the shell's desktopAuthed to true, and it used to be called
      // unconditionally right here — OPTIMISTICALLY, without checking that a local
      // operator token actually exists. When minting failed the shell therefore
      // rendered the dashboard, the auth probe then answered an affirmative false,
      // and the operator watched the connect overlay come back after ~0.5s with NO
      // error at all. That flash was this optimism.
      //
      // dashboard_auth_status is the same authority the shell polls, so asking it
      // here means we only ever claim what that authority will confirm a moment
      // later. If it says no, say so instead of flashing.
      const st = await dashboardAuthStatus(projectRoot);
      if (!st?.authenticated) {
        throw new Error(
          "CodeNexus signed you in, but this app did not receive a local operator " +
            "token, so it cannot stay signed in. Nothing was minted locally — the " +
            "sign-in did not fail, the local half simply never happened. " +
            "(dashboard_auth_status still reports not-authenticated.)",
        );
      }
      onAuthenticated();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-6 bg-castle-bg px-4 text-center">
      <div className="flex items-center gap-3">
        <div className="relative grid h-12 w-12 place-items-center rounded-xl border border-castle-allow/50 bg-castle-bg shadow-castle-glow">
          <div className="absolute inset-0.5 rounded-lg border border-castle-allow/20" />
          <img src={logoUrl} alt="AIDOCS" className="h-8 w-8" />
        </div>
        <div className="text-left">
          <div className="text-xl font-black leading-tight tracking-tight text-white">AIDOCS</div>
          <div className="text-[11px] font-bold uppercase tracking-widest text-castle-mute">
            Operator console
          </div>
        </div>
      </div>

      <div className="flex w-full max-w-sm flex-col gap-4">
        <p className="text-sm leading-relaxed text-castle-mute">
          Sign in with your CodeNexus account. Your accounts, permissions and
          projects live on CodeNexus — this app holds no separate login.
        </p>

        {error ? (
          <div
            role="alert"
            className="rounded-lg border border-castle-deny/40 bg-castle-deny/10 px-3 py-2 text-left text-sm text-castle-deny"
          >
            {error}
          </div>
        ) : null}

        <button
          type="button"
          onClick={() => void signIn()}
          disabled={busy}
          aria-label="Continue with CodeNexus"
          className="rounded-xl border border-castle-allow/40 bg-castle-allow/15 px-5 py-3 text-sm font-semibold text-castle-allow transition hover:bg-castle-allow/25 disabled:opacity-50"
        >
          {busy ? "Opening CodeNexus…" : "Continue with CodeNexus"}
        </button>

        <p className="text-[11px] leading-relaxed text-castle-mute">
          A browser window opens for CodeNexus to verify you. The session it
          returns is valid for 30 days, so local work keeps running offline.
        </p>
      </div>
    </div>
  );
}
