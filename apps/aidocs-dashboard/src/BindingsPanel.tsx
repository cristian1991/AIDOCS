import { useState } from "react";
import { bindingApprove, type HostBindingRow } from "./dashboardApi";
import { pendingCount } from "./operatorBinding";

/**
 * Host-operator bindings surface (Empire directive 2026-07-17). Pending bindings
 * get a one-click "Bind to me" (rides the cached operator token through the
 * SAME audited approve path); approved bindings are listed for context.
 *
 * Rendered as a collapsible right-edge tab: absent when there is nothing to
 * show, a small nub carrying the pending count otherwise, sliding open to the
 * full panel on click and closable again.
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
  const [open, setOpen] = useState(false);
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

  // Nothing to act on or show: the tab does not exist at all.
  if (pending.length === 0 && approved.length === 0) return null;

  if (!open) {
    return (
      <button
        type="button"
        aria-expanded={false}
        aria-label={pendingN > 0 ? `Bindings, ${pendingN} pending` : "Bindings"}
        onClick={() => setOpen(true)}
        className="fixed right-0 top-1/3 z-40 flex items-center gap-1 rounded-l-full border border-r-0 border-castle-allow/40 bg-castle-panel px-2 py-2 text-[11px] font-bold text-castle-allow shadow-lg transition hover:bg-castle-allow/15"
      >
        <span aria-hidden="true">{pendingN > 0 ? pendingN : "•"}</span>
      </button>
    );
  }

  return (
    <aside
      className="fixed right-0 top-1/4 z-40 flex max-h-[60vh] w-[320px] flex-col gap-3 overflow-y-auto rounded-l-xl border border-r-0 border-castle-line bg-castle-panel p-4 shadow-2xl"
      aria-label="Host bindings"
    >
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
        <button
          type="button"
          aria-expanded={true}
          aria-label="Close bindings"
          onClick={() => setOpen(false)}
          className="ml-auto rounded-md border border-castle-line/60 px-2 py-0.5 text-xs text-castle-mute hover:text-white"
        >
          ✕
        </button>
      </div>

      {error ? (
        <div role="alert" className="rounded-lg border border-castle-deny/40 bg-castle-deny/10 px-3 py-2 text-sm text-castle-deny">
          {error}
        </div>
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
    </aside>
  );
}
