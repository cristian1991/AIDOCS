import { useEffect, useRef, useState } from "react";
import { openUpgrade } from "./entitlements";

/** Initials for the avatar: "cristian.nazare@gmail.com" -> "CN". */
function initials(email?: string): string {
  if (!email) return "?";
  const local = email.split("@")[0] || email;
  const parts = local.split(/[._\-+]+/).filter(Boolean);
  const letters =
    parts.length >= 2 ? parts[0][0] + parts[1][0] : local.slice(0, 2);
  return letters.toUpperCase();
}

/** Signed-in user avatar with a dropdown (account/setup links + sign out).
 *  Replaces the old "Connected to the gate as <email>" text + bare button. */
export function UserMenu({
  email,
  scope,
  onSignOut,
}: {
  email?: string;
  scope?: string;
  onSignOut: () => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function onDoc(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const avatar = (
    <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-castle-allow/90 to-emerald-500/80 text-sm font-semibold text-black">
      {initials(email)}
    </span>
  );

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
        title={email || "Account"}
        className="flex items-center justify-center rounded-full ring-1 ring-castle-allow/40 transition hover:brightness-110 focus:outline-none focus:ring-2 focus:ring-castle-allow/70"
      >
        {avatar}
      </button>

      {open ? (
        <div
          role="menu"
          className="absolute right-0 z-50 mt-2 w-64 overflow-hidden rounded-xl border border-castle-line bg-[#0b1411]/95 shadow-2xl backdrop-blur"
        >
          <div className="flex items-center gap-3 border-b border-castle-line px-4 py-3">
            {avatar}
            <div className="min-w-0">
              <div className="truncate text-sm font-medium text-slate-100">
                {email || "Signed in"}
              </div>
              {scope ? (
                <div className="truncate text-xs text-castle-mute">{scope}</div>
              ) : null}
            </div>
          </div>

          <nav className="py-1 text-sm">
              <button
                role="menuitem"
                type="button"
                onClick={() => { setOpen(false); openUpgrade("cloudagent"); }}
                className="block w-full px-4 py-2 text-left font-medium text-castle-allow transition hover:bg-white/[0.06]"
              >
                Upgrade plan
              </button>
            <a
              role="menuitem"
              href="https://codenexus.cloud"
              target="_blank"
              rel="noopener noreferrer"
              className="block px-4 py-2 text-slate-200 transition hover:bg-white/[0.06]"
              onClick={() => setOpen(false)}
            >
              Account &amp; console
            </a>
            <a
              role="menuitem"
              href="https://github.com/settings/tokens"
              target="_blank"
              rel="noopener noreferrer"
              className="block px-4 py-2 text-slate-200 transition hover:bg-white/[0.06]"
              onClick={() => setOpen(false)}
            >
              GitHub token (private imports)
            </a>
          </nav>

          <div className="border-t border-castle-line py-1">
            <button
              role="menuitem"
              type="button"
              onClick={() => {
                setOpen(false);
                onSignOut();
              }}
              className="block w-full px-4 py-2 text-left text-sm text-rose-300 transition hover:bg-rose-500/10"
            >
              Sign out
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
