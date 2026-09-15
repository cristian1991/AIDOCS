import { availableModes, conductorLockReason, type Mode } from "./webmcpScope";

const MODE_LABEL: Record<Mode, string> = {
  local: "Local",
  webmcp: "WebAgent",
  cloudagent: "CloudAgent",
};

// CloudAgent isn't runnable yet (pending ADB AI-agent + dockerization). Instead of a
// loud banner, it's a disabled tab with a small "soon" chip + a tooltip explaining why.
const COMING_SOON: ReadonlySet<Mode> = new Set<Mode>(["cloudagent"]);

/**
 * The ONE mode switcher used by both builds — a compact segmented control over
 * availableModes() (desktop: Local/WebAgent/CloudAgent; web: WebAgent/CloudAgent).
 * `onPick` fires only for selectable modes; coming-soon modes are inert.
 */
export function ModeSwitcher({
  mode,
  onPick,
}: {
  mode: Mode;
  onPick: (m: Mode) => void;
}) {
  return (
    <div className="inline-flex items-center gap-1 rounded-xl border border-castle-line bg-black/20 p-1">
      {availableModes().map((m) => {
        const active = m === mode;
        const soon = COMING_SOON.has(m);
        return (
          <button
            key={m}
            type="button"
            aria-pressed={active}
            disabled={soon}
            title={soon ? conductorLockReason("cloudagent") : `Switch to ${MODE_LABEL[m]}`}
            onClick={() => {
              if (!soon) onPick(m);
            }}
            className={
              "inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium transition " +
              (active
                ? "bg-castle-allow/15 text-white"
                : "text-castle-mute hover:text-slate-200") +
              (soon ? " cursor-not-allowed opacity-60 hover:text-castle-mute" : "")
            }
          >
            {MODE_LABEL[m]}
            {soon ? (
              <span className="rounded bg-white/10 px-1 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-castle-mute">
                soon
              </span>
            ) : null}
          </button>
        );
      })}
    </div>
  );
}
