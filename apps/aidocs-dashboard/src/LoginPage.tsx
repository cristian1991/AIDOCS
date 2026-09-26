import { useState } from "react";
import { beginLogin } from "./platform/webAuth";
import { isWebBuild } from "./webmcpScope";
import logoUrl from "./cn-logo.svg";

/**
 * Signed-out landing. Two identities (#471 shell-identity fix):
 *  - WEB build: the CodeNexus-hosted console — CodeNexus copy + the
 *    "Back to CodeNexus" escape hatch.
 *  - DESKTOP build: a LOCAL app on this computer. It must not present a
 *    cloud identity or a "Back to CodeNexus" link; cloud sign-in is an
 *    affordance for the cloud modes, not the app's identity.
 * The sign-in button is single-flight: clicks are ignored while an OAuth
 * attempt is pending (each extra click used to open another browser window).
 */
export function LoginPage() {
  const web = isWebBuild();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const signIn = async () => {
    if (busy) return; // single-flight guard (#471 OAuth cascade)
    setBusy(true);
    setError(null);
    try {
      await beginLogin();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Sign-in failed");
    } finally {
      setBusy(false);
    }
  };

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
            {web ? "Empire console" : "This computer"}
          </div>
        </div>
      </div>
      <p className="max-w-sm text-sm leading-relaxed text-castle-mute">
        {web
          ? "Sign in with your CodeNexus account to manage your organization's projects, agents, skills, and policy."
          : "Sign in to connect this computer's AIDOCS to your cloud projects. Signing in opens your browser once."}
      </p>
      {error ? (
        <p role="alert" className="max-w-sm text-xs leading-relaxed text-red-400">
          {error}
        </p>
      ) : null}
      <button
        type="button"
        onClick={() => void signIn()}
        disabled={busy}
        className="rounded-xl border border-castle-allow/40 bg-castle-allow/15 px-5 py-2.5 text-sm font-semibold text-castle-allow transition hover:bg-castle-allow/25 disabled:opacity-50"
      >
        {busy ? "Waiting for browser sign-in…" : web ? "Sign in with CodeNexus" : "Sign in"}
      </button>
      {web ? (
        <a
          href="https://codenexus.cloud"
          className="text-xs text-castle-mute underline hover:text-slate-300"
        >
          ← Back to CodeNexus
        </a>
      ) : null}
    </div>
  );
}

