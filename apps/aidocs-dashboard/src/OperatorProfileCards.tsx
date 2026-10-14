/**
 * OperatorProfileCards — first real operator profile cards for the two
 * dangerous doctrine areas (Breakglass & Flavor, Authority Exceptions /
 * Border Law). These NEVER expose raw per-key toggles: a change is applied
 * as one confirmed action through operatorSurfaceApply, which the backend
 * requires to carry the exact confirm token AND a non-empty reason. A
 * confirmed action may write the profile's own dashboard-only/security-
 * sensitive member keys, but can never reach a service-managed or
 * deprecated key.
 */
import { useEffect, useState } from "react";
import { ShieldAlert } from "lucide-react";
import type {
  OperatorProfileSummary,
  OperatorSurfaceResult,
} from "./dashboardApi";
import {
  operatorSurfaceApply,
  operatorSurfaceList,
  operatorSurfaceStatus,
} from "./dashboardApi";
import { profileConfirmToken } from "./settingsRouting";

const DANGEROUS_IDS = ["breakglass_flavor", "authority_border"] as const;

interface CardState {
  open: boolean;
  valuesJson: string;
  reason: string;
  confirm: string;
  busy: boolean;
  result: OperatorSurfaceResult | null;
  members: OperatorSurfaceResult | null;
}

const EMPTY: CardState = {
  open: false,
  valuesJson: "{\n  \n}",
  reason: "",
  confirm: "",
  busy: false,
  result: null,
  members: null,
};

export function OperatorProfileCards({ scope = "global" }: { scope?: string }) {
  const [profiles, setProfiles] = useState<OperatorProfileSummary[]>([]);
  const [cards, setCards] = useState<Record<string, CardState>>({});

  useEffect(() => {
    let live = true;
    operatorSurfaceList()
      .then((res) => {
        if (!live) return;
        const dangerous = (res.profiles ?? []).filter((p) =>
          (DANGEROUS_IDS as readonly string[]).includes(p.id),
        );
        setProfiles(dangerous);
        setCards(Object.fromEntries(dangerous.map((p) => [p.id, { ...EMPTY }])));
      })
      .catch(() => setProfiles([]));
    return () => {
      live = false;
    };
  }, []);

  function patch(id: string, next: Partial<CardState>) {
    setCards((prev) => ({ ...prev, [id]: { ...prev[id], ...next } }));
  }

  async function toggleOpen(p: OperatorProfileSummary) {
    const cur = cards[p.id] ?? EMPTY;
    const opening = !cur.open;
    patch(p.id, { open: opening });
    if (opening && !cur.members) {
      try {
        const st = await operatorSurfaceStatus(p.id);
        patch(p.id, { members: st });
      } catch {
        /* status optional */
      }
    }
  }

  async function apply(p: OperatorProfileSummary) {
    const c = cards[p.id];
    patch(p.id, { busy: true, result: null });
    try {
      const res = await operatorSurfaceApply({
        profileId: p.id,
        valuesJson: c.valuesJson,
        confirm: c.confirm,
        reason: c.reason,
        scope,
      });
      patch(p.id, { result: res });
    } catch (e) {
      patch(p.id, { result: { ok: false, message: String(e) } });
    } finally {
      patch(p.id, { busy: false });
    }
  }

  if (profiles.length === 0) return null;

  return (
    <div className="mb-6 grid gap-3 md:grid-cols-2">
      {profiles.map((p) => {
        const c = cards[p.id] ?? EMPTY;
        const expected = profileConfirmToken(p.id);
        const ready = c.confirm.trim() === expected && c.reason.trim().length > 0;
        return (
          <div
            key={p.id}
            className="overflow-hidden rounded-2xl border border-castle-deny/30 bg-castle-deny/[0.04]"
          >
            <button
              type="button"
              onClick={() => toggleOpen(p)}
              className="flex w-full items-center gap-2 px-4 py-3 text-left"
            >
              <ShieldAlert className="h-4 w-4 shrink-0 text-castle-deny" />
              <span className="flex-1">
                <span className="block text-sm font-black text-slate-100">
                  {p.title}
                </span>
                <span className="block text-[11px] text-castle-mute">
                  {p.danger.toUpperCase()} · confirmed action only
                </span>
              </span>
            </button>
            {c.open && (
              <div className="border-t border-castle-deny/20 bg-black/20 p-4">
                <div className="mb-2 text-[11px] text-castle-mute">
                  Member keys:{" "}
                  <span className="font-mono">{p.keys.join(", ")}</span>
                </div>
                <label className="mb-1 block text-[10px] font-bold uppercase tracking-widest text-castle-mute">
                  Values (JSON of member keys)
                </label>
                <textarea
                  value={c.valuesJson}
                  onChange={(e) => patch(p.id, { valuesJson: e.target.value })}
                  rows={4}
                  className="mb-2 w-full rounded-lg border border-castle-line bg-black/30 px-2 py-1.5 font-mono text-xs text-slate-100"
                />
                <input
                  value={c.reason}
                  onChange={(e) => patch(p.id, { reason: e.target.value })}
                  placeholder="reason (required)"
                  className="mb-2 w-full rounded-lg border border-castle-line bg-black/30 px-2 py-1.5 text-xs text-slate-100"
                />
                <input
                  value={c.confirm}
                  onChange={(e) => patch(p.id, { confirm: e.target.value })}
                  placeholder={expected}
                  className="mb-2 w-full rounded-lg border border-castle-deny/40 bg-black/30 px-2 py-1.5 font-mono text-xs text-castle-deny"
                />
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    disabled={!ready || c.busy}
                    onClick={() => apply(p)}
                    className="rounded-lg border border-castle-deny/50 bg-castle-deny/15 px-3 py-1.5 text-xs font-bold text-castle-deny hover:bg-castle-deny/25 disabled:opacity-40"
                  >
                    {c.busy ? "Applying..." : "Apply (confirmed)"}
                  </button>
                  <span className="text-[10px] text-castle-mute">
                    Echo the exact phrase + a reason to enable Apply.
                  </span>
                </div>
                {c.result && (
                  <div
                    className={
                      "mt-2 text-[11px] " +
                      (c.result.ok ? "text-castle-allow" : "text-castle-deny")
                    }
                  >
                    {c.result.ok
                      ? "Applied."
                      : c.result.message || c.result.error || "Refused."}
                  </div>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
