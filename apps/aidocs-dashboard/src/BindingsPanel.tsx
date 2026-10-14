import { useState } from "react";
import { bindingApprove, type HostBindingRow } from "./dashboardApi";
import { pendingCount } from "./operatorBinding";

/**
 * Host-operator bindings surface (Empire directive 2026-07-17). Pending bindings
 * get a one-click "Bind to me" (rides the cached operator token through the
 * SAME audited approve path); approved bindings are listed for context. A badge
 * shows the pending count so a new pairing request is visible at a glance.
 */
export function BindingsPanel({
  bindings,
  projectRoot,
  onChanged,
}: {
  bindings: HostBindingRow[];
  projectRoot?: string;
  onChanged?: () => void;
}) {
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const pending = bindings.filter((b) => b.status === "pending");
  const approved = bindings.filter((b) => b.status === "approved");
  const pendingN = pendingCount(bindings);

  async function approve(bindingId: string) {
    if (busyId) return;
    setBusyId(bindingId);
    setError(null);
    try {
      const res = await bindingApprove(bindingId, projectRoot);
      if (!res.ok) {
        setError(res.message || res.reason || "approval failed");
      } else {
        onChanged?.();
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyId(null);
    }
  }

  return (
    <section className="flex flex-col gap-3" aria-label="Host bindings">
      <div className="flex items-center gap-2">
        <h3 className="text-sm font-bold uppercase tracking-widest text-castle-mute">Bindings</h3>
        {pendingN > 0 ? (
          <span
            aria-label={`${pendingN} pending binding${pendingN === 1 ? "" : "s"}`}
            className="rounded-full border border-castle-allow/40 bg-castle-allow/15 px-2 py-0.5 text-[11px] font-bold text-castle-allow"
          >
            {pendingN} pending
          </span>
        ) : null}
      </div>

      {error ? (
        <div role="alert" className="rounded-lg border border-castle-deny/40 bg-castle-deny/10 px-3 py-2 text-sm text-castle-deny">
          {error}
        </div>
      ) : null}

      {pending.length === 0 && approved.length === 0 ? (
        <div className="text-sm text-castle-mute">No pending or approved bindings for this project.</div>
      ) : null}

      {pending.map((b) => (
        <div key={b.binding_id} className="flex items-center justify-between gap-3 rounded-lg border border-castle-allow/20 bg-castle-bg px-3 py-2">
          <div className="min-w-0 text-left">
            <div className="truncate text-sm font-semibold text-white">{b.host_kind}</div>
            <div className="truncate text-xs text-castle-mute">session {b.host_session_id}</div>
          </div>
          <button
            type="button"
            disabled={busyId !== null}
            onClick={() => void approve(b.binding_id)}
            className="shrink-0 rounded-lg border border-castle-allow/40 bg-castle-allow/15 px-3 py-1.5 text-xs font-semibold text-castle-allow transition hover:bg-castle-allow/25 disabled:opacity-50"
          >
            {busyId === b.binding_id ? "Binding…" : "Bind to me"}
          </button>
        </div>
      ))}

      {approved.map((b) => (
        <div key={b.binding_id} className="flex items-center justify-between gap-3 rounded-lg border border-castle-line/40 bg-castle-bg px-3 py-2">
          <div className="min-w-0 text-left">
            <div className="truncate text-sm font-semibold text-white">{b.host_kind}</div>
            <div className="truncate text-xs text-castle-mute">session {b.host_session_id}</div>
          </div>
          <span className="shrink-0 text-xs font-semibold text-castle-allow">bound</span>
        </div>
      ))}
    </section>
  );
}
