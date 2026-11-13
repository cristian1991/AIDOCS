/**
 * #1256: the gate's preflight switch, one per gate project. It stays off until it
 * is turned back on, and it changes the gate only: the operator's local preflight
 * is not reached by it. The server decides everything that matters (super_admin,
 * the single-use confirmation, the compare-and-set, the audit row); this card
 * shows the state and carries the operator's reason and yes.
 */
import { useEffect, useState } from "react";
import { ShieldAlert } from "lucide-react";
import { preflightSwitchSet, preflightSwitchStatus } from "./dashboardApi";
import { getScope } from "./webmcpScope";

type Load =
  | { state: "loading" }
  | { state: "not_allowed" }
  | { state: "failed"; message: string }
  | { state: "ready"; enabled: boolean };

/** While the gate's preflight is off. Every page shows it, in both web modes. */
export function PreflightOffBanner({ enabled }: { enabled?: boolean }) {
  if (enabled !== false || getScope() !== "web") return null;
  return (
    <div
      role="alert"
      className="flex items-center gap-2 border-b border-castle-deny/40 bg-castle-deny/15 px-4 py-2 text-xs font-bold text-castle-deny"
    >
      <ShieldAlert className="h-4 w-4 shrink-0" />
      <span>
        Preflight is off for this project. This is the gate's setting: prompts and messages from
        other agents on the gate are not checked by preflight until it is turned back on in Settings.
      </span>
    </div>
  );
}

export function PreflightSwitchCard({
  projectId,
  locked = false,
  onChanged,
}: {
  projectId: string;
  /** True while a project switch is loading: nothing may be changed. */
  locked?: boolean;
  onChanged?: () => void;
}) {
  const [load, setLoad] = useState<Load>({ state: "loading" });
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let live = true;
    setLoad({ state: "loading" });
    preflightSwitchStatus(projectId)
      .then((res) => {
        if (!live) return;
        if (res.ok && typeof res.enabled === "boolean") setLoad({ state: "ready", enabled: res.enabled });
        else if (res._error === "super_admin_required") setLoad({ state: "not_allowed" });
        else setLoad({ state: "failed", message: String(res._detail || res._error || "refused") });
      })
      .catch((err) => {
        if (live) setLoad({ state: "failed", message: String(err) });
      });
    return () => {
      live = false;
    };
  }, [projectId, nonce]);

  // A different project: nothing typed for the last one applies to it.
  useEffect(() => {
    setReason("");
    setMessage("");
  }, [projectId]);

  const canSubmit = load.state === "ready" && !!reason.trim() && !busy && !locked;

  async function change() {
    if (load.state !== "ready" || !canSubmit) return;
    setBusy(true);
    setMessage("");
    try {
      const res = await preflightSwitchSet({
        projectId,
        enabled: !load.enabled,
        expectedEnabled: load.enabled,
        reason: reason.trim(),
      });
      if (res.ok && typeof res.enabled === "boolean") {
        setLoad({ state: "ready", enabled: res.enabled });
        setReason("");
        onChanged?.();
      } else {
        setMessage(String(res._detail || res._error || "Refused."));
        // Someone else moved it, or the confirmation lapsed: show what is true now.
        setNonce((n) => n + 1);
      }
    } catch (err) {
      setMessage(String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mb-4 rounded-2xl border border-castle-deny/40 bg-castle-deny/[0.06] p-4">
      <div className="mb-1 flex items-center gap-2">
        <ShieldAlert className="h-4 w-4 text-castle-deny" />
        <span className="text-sm font-black uppercase tracking-widest text-castle-deny">
          Gate preflight
        </span>
        {load.state === "ready" && (
          <span className="rounded-md border border-castle-deny/40 px-2 py-0.5 text-[11px] font-bold text-slate-100">
            {load.enabled ? "On" : "Off"}
          </span>
        )}
      </div>
      <p className="mb-3 text-[11px] leading-relaxed text-castle-mute">
        This is the gate's setting for this project; preflight on your own machine is not changed by
        it. Off stops preflight on prompts and on messages from other agents, and stays off until it
        is turned back on. Other guards keep running. Each change is recorded with your reason.
      </p>
      {load.state === "loading" && <div className="text-[11px] text-castle-mute">Reading the gate...</div>}
      {load.state === "not_allowed" && (
        <div className="text-[11px] text-castle-mute">Only a super admin can change this.</div>
      )}
      {load.state === "failed" && <div className="text-[11px] text-castle-deny">{load.message}</div>}
      {load.state === "ready" && (
        <div className="flex flex-wrap items-center gap-2">
          <input
            type="text"
            aria-label="reason"
            placeholder="Why is this changing?"
            maxLength={500}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            className="min-w-[240px] flex-1 rounded-lg border border-castle-line bg-black/30 px-2 py-1.5 text-sm text-slate-100 placeholder:text-castle-mute focus:border-castle-allow/50 focus:outline-none"
          />
          <button
            type="button"
            onClick={() => void change()}
            disabled={!canSubmit}
            className="rounded-lg border border-castle-deny/40 bg-castle-deny/15 px-3 py-1.5 text-sm font-bold text-castle-deny hover:bg-castle-deny/25 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {load.enabled ? "Turn preflight off" : "Turn preflight back on"}
          </button>
        </div>
      )}
      {message && <div className="mt-2 text-[11px] text-castle-deny">{message}</div>}
    </div>
  );
}
