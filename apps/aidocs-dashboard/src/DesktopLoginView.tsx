import { useState } from "react";
import { operatorLogin } from "./dashboardApi";
import logoUrl from "./cn-logo.svg";

/**
 * Desktop operator sign-in (Empire directive 2026-07-17: 1 dashboard = 1 user =
 * bind). Shown whenever no valid operator token resolves — replaces the old
 * fake-connected state. Email + password authenticate via the password-gated
 * `dashboard-login` CLI; the bearer token never crosses back to the frontend.
 */
export function DesktopLoginView({
  projectRoot,
  onAuthenticated,
}: {
  projectRoot?: string;
  onAuthenticated: () => void;
}) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const res = await operatorLogin(email.trim(), password, projectRoot);
      if (res.ok) {
        setPassword("");
        onAuthenticated();
      } else {
        setError(res.message || res.reason || "invalid email or password");
      }
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
      <form onSubmit={submit} className="flex w-full max-w-sm flex-col gap-3 text-left">
        <label className="text-xs font-semibold uppercase tracking-widest text-castle-mute">
          Email
          <input
            type="email"
            autoComplete="username"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            aria-label="Email"
            className="mt-1 w-full rounded-lg border border-castle-allow/30 bg-castle-bg px-3 py-2 text-sm text-white outline-none focus:border-castle-allow"
          />
        </label>
        <label className="text-xs font-semibold uppercase tracking-widest text-castle-mute">
          Password
          <input
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            aria-label="Password"
            className="mt-1 w-full rounded-lg border border-castle-allow/30 bg-castle-bg px-3 py-2 text-sm text-white outline-none focus:border-castle-allow"
          />
        </label>
        {error ? (
          <div role="alert" className="rounded-lg border border-castle-deny/40 bg-castle-deny/10 px-3 py-2 text-sm text-castle-deny">
            {error}
          </div>
        ) : null}
        <button
          type="submit"
          disabled={busy || !email.trim() || !password}
          className="mt-1 rounded-xl border border-castle-allow/40 bg-castle-allow/15 px-5 py-2.5 text-sm font-semibold text-castle-allow transition hover:bg-castle-allow/25 disabled:opacity-50"
        >
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
