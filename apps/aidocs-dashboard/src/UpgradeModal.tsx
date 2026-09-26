import { useEffect, useState } from "react";
import { onUpgradeIntent, upgradeUrl, type Sku, type UpgradeIntent } from "./entitlements";

// The upgrade modal — one shared surface for both paid SKUs. Opened from anywhere
// via openUpgrade(sku) (gated buttons, empty-state CTAs, or a gate `upgrade_required`
// rejection). 120%: this only ROUTES to billing; it grants nothing. The gate stays
// the enforcer, so the moat survives even if this modal is deleted in devtools.

type Tier = {
  sku: Sku | "webmcp";
  name: string;
  tagline: string;
  price: string;
  features: string[];
  cta?: string;
};

const TIERS: Tier[] = [
  {
    sku: "webmcp",
    name: "WebAgent",
    tagline: "Connect ChatGPT or Claude to your project.",
    price: "Included",
    features: [
      "Governed code intelligence — catalog, search, read tools",
      "Live status + project snapshot",
      "Same gate security + audited tool usage as every plan",
      "Single operator",
    ],
  },
  {
    sku: "webmcp-seats",
    name: "WebAgent · Team",
    tagline: "Bring your team onto the same project.",
    price: "Per seat",
    cta: "Add WebAgent seats",
    features: [
      "Multiple operators on one project",
      "Org RBAC — assign owner / admin / member roles",
      "Shared sessions across your team",
    ],
  },
  {
    sku: "cloudagent",
    name: "CloudAgent",
    tagline: "Orchestrate agents in the cloud.",
    price: "Per seat + capacity",
    cta: "Activate CloudAgent",
    features: [
      "Cloud Conductor — runs lanes, dispatches agents",
      "No local machine required",
      "Lane timeline + approval gates",
    ],
  },
];

function UpgradeModal({ intent, onClose }: { intent: UpgradeIntent; onClose: () => void }) {
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-[120] flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm"
      onClick={onClose}
      role="presentation"
    >
      <div
        className="w-full max-w-3xl overflow-hidden rounded-2xl border border-castle-line bg-[#0a1310] shadow-2xl"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="Upgrade your plan"
      >
        <div className="flex items-start justify-between border-b border-castle-line px-6 py-4">
          <div>
            <h2 className="text-lg font-semibold text-slate-100">Upgrade your plan</h2>
            <p className="mt-0.5 text-sm text-castle-mute">
              {intent.reason || "Unlock collaboration and cloud orchestration."}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg px-2 py-1 text-castle-mute transition hover:bg-white/[0.06] hover:text-slate-200"
            aria-label="Close"
          >
            ✕
          </button>
        </div>

        <div className="grid gap-3 p-5 sm:grid-cols-3">
          {TIERS.map((tier) => {
            const focused = tier.sku === intent.sku;
            const isCurrent = tier.sku === "webmcp";
            return (
              <div
                key={tier.sku}
                className={
                  "flex flex-col rounded-xl border p-4 transition " +
                  (focused
                    ? "border-castle-allow/70 bg-castle-allow/[0.07] shadow-[0_0_0_1px_rgba(94,234,212,0.25)]"
                    : "border-castle-line bg-black/20")
                }
              >
                <div className="flex items-center justify-between">
                  <span className="text-sm font-semibold text-slate-100">{tier.name}</span>
                  {isCurrent ? (
                    <span className="rounded bg-white/[0.08] px-1.5 py-0.5 text-[10px] font-medium text-castle-mute">
                      Current
                    </span>
                  ) : focused ? (
                    <span className="rounded bg-castle-allow/20 px-1.5 py-0.5 text-[10px] font-medium text-castle-allow">
                      Recommended
                    </span>
                  ) : null}
                </div>
                <div className="mt-1 text-xs text-castle-mute">{tier.price}</div>
                <p className="mt-2 text-xs leading-relaxed text-slate-300">{tier.tagline}</p>
                <ul className="mt-3 flex-1 space-y-1.5">
                  {tier.features.map((f) => (
                    <li key={f} className="flex gap-1.5 text-[11px] leading-snug text-castle-mute">
                      <span className="text-castle-allow">✓</span>
                      <span>{f}</span>
                    </li>
                  ))}
                </ul>
                {tier.cta && tier.sku !== "webmcp" ? (
                  <a
                    href={upgradeUrl(tier.sku as Sku)}
                    target="_blank"
                    rel="noopener noreferrer"
                    className={
                      "mt-4 inline-flex items-center justify-center rounded-lg px-3 py-2 text-xs font-semibold transition " +
                      (focused
                        ? "bg-castle-allow/20 text-white hover:bg-castle-allow/30 border border-castle-allow/40"
                        : "border border-castle-line text-slate-200 hover:bg-white/[0.06]")
                    }
                  >
                    {tier.cta} →
                  </a>
                ) : (
                  <div className="mt-4 inline-flex items-center justify-center rounded-lg border border-transparent px-3 py-2 text-xs font-medium text-castle-mute">
                    Active
                  </div>
                )}
              </div>
            );
          })}
        </div>

        <div className="border-t border-castle-line px-6 py-3 text-[11px] leading-relaxed text-castle-mute">
          Security, RBAC enforcement, and audit apply on <strong className="text-slate-300">every</strong> plan.
          Upgrades add capacity and collaboration — they never bypass the gate.
        </div>
      </div>
    </div>
  );
}

/** Mount once near the app root. Listens for openUpgrade(sku) from anywhere. */
export function UpgradeModalHost() {
  const [intent, setIntent] = useState<UpgradeIntent | null>(null);
  useEffect(() => onUpgradeIntent(setIntent), []);
  if (!intent) return null;
  return <UpgradeModal intent={intent} onClose={() => setIntent(null)} />;
}
