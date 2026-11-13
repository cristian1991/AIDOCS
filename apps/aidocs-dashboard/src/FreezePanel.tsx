/**
 * FreezePanel — War 0 (2026-07-13): control-plane visibility for session
 * freezes + pending escalations, with recovery actions.
 *
 * The witnessed deadlock: a frozen web session was INVISIBLE to the operator
 * (no freeze read in the dashboard) and unclearable over the web. This panel
 * renders on BOTH shells through the one adapter seam:
 *   desktop → Tauri commands freeze_list / escalation_list / clear_freeze
 *   web     → GATE_TOOL freeze_list / escalation_list / clear_freeze
 *             (org OWNER/ADMIN only; clear_freeze runs the gate's consumable
 *             two-phase confirm via gateCallConfirming)
 *
 * Renders NOTHING when there are no active freezes and no pending
 * escalations — it is an alert strip, not a permanent fixture.
 */
import { useCallback, useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { CastleButton, CastlePill } from "./CastleShell";

export type FreezeRow = {
  freeze_id: string;
  session_id: string;
  kind: string;
  fingerprint_phrase?: string;
  frozen_at?: string;
  expires_at?: string | null;
  user_id?: string;
};

export type EscalationRow = {
  request_id: string;
  requester_label?: string;
  session_id?: string;
  gate_permission?: string;
  gate_phrase?: string;
  created_at?: string;
  expires_at?: string;
};

type ListResult<T> = { ok?: boolean; items?: T[] };

export type FreezePanelProps = {
  projectRoot?: string | null;
  /** Approve/Deny reuse the app's existing authenticated escalation wiring. */
  onApproveEscalation?: (requestId: string) => void | Promise<void>;
  onDenyEscalation?: (requestId: string) => void | Promise<void>;
  /** Fired after a successful clear so the wrapper can refresh the snapshot. */
  onCleared?: () => void;
};

export function FreezePanel({
  projectRoot,
  onApproveEscalation,
  onDenyEscalation,
  onCleared,
}: FreezePanelProps) {
  const [freezes, setFreezes] = useState<FreezeRow[]>([]);
  const [escalations, setEscalations] = useState<EscalationRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [fz, esc] = await Promise.all([
        invoke<ListResult<FreezeRow>>("freeze_list", {
          projectRoot: projectRoot ?? null,
        }),
        invoke<ListResult<EscalationRow>>("escalation_list", {
          projectRoot: projectRoot ?? null,
        }),
      ]);
      setFreezes(fz?.items ?? []);
      setEscalations(esc?.items ?? []);
      setError(null);
    } catch {
      // Fail closed + quiet: a non-admin (or a shell without the commands)
      // simply sees no panel — never an error toast on load.
      setFreezes([]);
      setEscalations([]);
    }
  }, [projectRoot]);

  useEffect(() => {
    void load();
    const t = setInterval(() => void load(), 15000);
    return () => clearInterval(t);
  }, [load]);

  async function clearOne(freezeId: string) {
    const reason = window.prompt(
      `Reason for clearing freeze ${freezeId} (required, audit trail):`,
      "",
    );
    if (!reason || !reason.trim()) return;
    setBusyId(freezeId);
    try {
      const r = await invoke<Record<string, unknown>>("clear_freeze", {
        projectRoot: projectRoot ?? null,
        freeze_id: freezeId,
        freezeId,
        reason: reason.trim(),
      });
      if (r && r["ok"] === false && (r["error"] || r["blocked_by"])) {
        setError(String(r["error"] || r["blocked_by"]));
      } else {
        setError(null);
        onCleared?.();
      }
      await load();
    } catch (e) {
      setError(`Clear failed: ${String(e)}`);
    } finally {
      setBusyId(null);
    }
  }

  const pendingEsc = escalations.filter(
    (e) => !freezes.some((f) => f.freeze_id === e.request_id),
  );

  if (!freezes.length && !pendingEsc.length && !error) return null;

  return (
    <div className="mx-4 mt-3 rounded-2xl border border-castle-warn/40 bg-castle-warn/[0.06] p-3">
      <div className="flex items-center justify-between pb-2">
        <h3 className="text-[10px] font-black uppercase tracking-widest text-castle-warn">
          Session Freezes &amp; Escalations
        </h3>
        <CastlePill tone={freezes.length ? "danger" : "warn"}>
          {freezes.length + pendingEsc.length}
        </CastlePill>
      </div>
      {error && (
        <div className="pb-2 text-[11px] text-castle-deny">{error}</div>
      )}
      <div className="space-y-2">
        {freezes.map((f) => (
          <div
            key={f.freeze_id}
            className="rounded-xl border border-castle-deny/30 bg-castle-deny/5 p-2.5"
          >
            <div className="flex items-center gap-2">
              <strong className="truncate text-sm text-slate-100">
                {f.kind}
              </strong>
              <code className="text-[11px] text-castle-mute">{f.freeze_id}</code>
              <span className="ml-auto text-[11px] text-castle-mute">
                session <code>{f.session_id}</code>
              </span>
            </div>
            {f.frozen_at && (
              <div className="mt-1 text-[11px] text-castle-mute">
                frozen {f.frozen_at}
                {f.user_id ? ` · ${f.user_id}` : ""}
              </div>
            )}
            <div className="mt-2">
              <CastleButton
                tone="danger"
                className="!py-1.5 !text-xs"
                disabled={busyId === f.freeze_id}
                onClick={() => void clearOne(f.freeze_id)}
                title="Clear this freeze (org admin; audited; no grant minted)"
              >
                {busyId === f.freeze_id ? "Clearing…" : "Clear freeze"}
              </CastleButton>
            </div>
          </div>
        ))}
        {pendingEsc.map((e) => (
          <div
            key={e.request_id}
            className="rounded-xl border border-castle-warn/30 bg-castle-warn/5 p-2.5"
          >
            <div className="flex items-center gap-2">
              <strong className="truncate text-sm text-slate-100">
                {e.gate_permission || "escalation"}
              </strong>
              <code className="text-[11px] text-castle-mute">{e.request_id}</code>
            </div>
            <div className="mt-1 truncate text-[11px] text-castle-mute">
              from <em>{e.requester_label || "unknown"}</em>
              {e.session_id ? ` · session ${e.session_id}` : ""}
            </div>
            <div className="mt-2 flex items-center gap-1">
              <CastleButton
                tone="primary"
                className="flex-1 !py-1.5 !text-xs"
                disabled={!onApproveEscalation}
                onClick={() => void onApproveEscalation?.(e.request_id)}
              >
                Approve
              </CastleButton>
              <CastleButton
                tone="danger"
                className="flex-1 !py-1.5 !text-xs"
                disabled={!onDenyEscalation}
                onClick={() => void onDenyEscalation?.(e.request_id)}
              >
                Deny
              </CastleButton>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
