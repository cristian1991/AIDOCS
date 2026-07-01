import { beginLogin } from "./platform/webAuth";
import logoUrl from "./cn-logo.svg";

/**
 * Signed-out landing for the web build. Replaces the old behavior of dropping the
 * unauthenticated user straight into the full shell (blank "No projects available"
 * with a broken header connection-panel). A clean, centered sign-in card; the
 * App gates to this whenever the web build has no live gate connection.
 */
export function LoginPage() {
  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-6 bg-castle-bg px-4 text-center">
      <div className="flex items-center gap-3">
        <div className="relative grid h-12 w-12 place-items-center rounded-xl border border-castle-allow/50 bg-castle-bg shadow-castle-glow">
          <div className="absolute inset-0.5 rounded-lg border border-castle-allow/20" />
          <img src={logoUrl} alt="CodeNexus" className="h-8 w-8" />
        </div>
        <div className="text-left">
          <div className="text-xl font-black leading-tight tracking-tight text-white">AIDOCS</div>
          <div className="text-[11px] font-bold uppercase tracking-widest text-castle-mute">
            Empire console
          </div>
        </div>
      </div>
      <p className="max-w-sm text-sm leading-relaxed text-castle-mute">
        Sign in with your CodeNexus account to manage your organization's projects,
        agents, skills, and policy.
      </p>
      <button
        type="button"
        onClick={() => void beginLogin()}
        className="rounded-xl border border-castle-allow/40 bg-castle-allow/15 px-5 py-2.5 text-sm font-semibold text-castle-allow transition hover:bg-castle-allow/25"
      >
        Sign in with CodeNexus
      </button>
      <a
        href="https://codenexus.cloud"
        className="text-xs text-castle-mute underline hover:text-slate-300"
      >
        ← Back to CodeNexus
      </a>
    </div>
  );
}
