/**
 * GovernedBashPanel — ONE operator action over the native-shell pilot.
 *
 * King re-seal 2026-05-30: there is no path/hash/signature wizard. The
 * operator presses one button — "Allow shell tools validated and supported
 * by AIDOCS" — and the backend auto-discovers + attests the canonical
 * provider (generated pin, no typed input). The UI never infers "enabled"
 * from flags; it renders the backend's read-only live_execution_posture and
 * shows ENABLED only when `verified` is true. When no system provider can
 * auto-enroll, the backend returns a SIGNED candidate approval card; the
 * operator approves exactly one offer (echoed back verbatim) — never a
 * typed path or hash.
 */
import { useCallback, useEffect, useState } from "react";
import { ShieldCheck, ShieldAlert, Loader2, TerminalSquare } from "lucide-react";
import {
  governedBashStatus,
  governedBashEnable,
  governedBashDisable,
  type GovernedBashPosture,
  type GovernedBashApprovalCard,
  type GovernedBashCandidateCard,
} from "./dashboardApi";

const CHECK_LABELS: Record<string, string> = {
  trusted_roots_configured: "Trusted install root configured",
  provider_path_resolved: "Provider path resolved",
  provider_identity: "Provider identity verified (under trusted root)",
  bounded_probe: "Bounded execution probe passed",
  hash_pin: "SHA-256 hash pin",
  os_signature: "OS code signature",
};

const FLAG_LABELS: Record<string, string> = {
  shell_enforcement_live: "Shell enforcement live",
  native_shell_provider_enabled: "Native provider enabled",
  native_shell_readonly_enabled: "Governed read-only class enabled",
};

function CheckRow({ label, value }: { label: string; value: boolean | null | string }) {
  if (typeof value === "string") return null;
  const skipped = value === null;
  const ok = value === true;
  return (
    <div className="flex items-center gap-2 py-1 text-xs">
      <span
        className={
          "grid h-4 w-4 place-items-center rounded-full text-[10px] font-bold " +
          (skipped
            ? "bg-castle-line/40 text-castle-mute"
            : ok
            ? "bg-castle-allow/20 text-castle-allow"
            : "bg-red-500/20 text-red-300")
        }
      >
        {skipped ? "–" : ok ? "✓" : "✕"}
      </span>
      <span className={skipped ? "text-castle-mute" : "text-slate-200"}>{label}</span>
      {skipped ? <span className="text-castle-mute/60">(skipped)</span> : null}
    </div>
  );
}

export function GovernedBashPanel({ projectRoot }: { projectRoot: string | null }) {
  const [posture, setPosture] = useState<GovernedBashPosture | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [approval, setApproval] = useState<GovernedBashApprovalCard | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const p = await governedBashStatus(projectRoot ?? undefined);
      setPosture(p);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [projectRoot]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // THE one action — no path / hash / signature inputs.
  async function doAllow() {
    setBusy(true);
    setError(null);
    setApproval(null);
    try {
      const res = await governedBashEnable({ projectRoot: projectRoot ?? undefined });
      if (res.posture) setPosture(res.posture);
      if (!res.ok) {
        if (res.requires_operator_approval && res.approval_card) {
          setApproval(res.approval_card);
        } else {
          setError(res.message || res.reason || "Could not enable.");
        }
      }
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  // Approve exactly ONE server-issued signed candidate card (echoed verbatim).
  async function approveCard(card: GovernedBashCandidateCard) {
    setBusy(true);
    setError(null);
    try {
      const res = await governedBashEnable({
        projectRoot: projectRoot ?? undefined,
        approvalCardJson: JSON.stringify(card),
      });
      if (res.posture) setPosture(res.posture);
      if (!res.ok) {
        setError(res.message || res.reason || "Approval rejected.");
      } else {
        setApproval(null);
      }
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function doDisable() {
    setBusy(true);
    setError(null);
    try {
      const res = await governedBashDisable(projectRoot ?? undefined);
      if (res.posture) setPosture(res.posture);
      setApproval(null);
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  const verified = !!posture?.verified;
  const live = posture?.live_execution_posture;

  return (
    <div className="mb-4 rounded-2xl border border-castle-line bg-castle-card">
      <div className="flex items-center gap-3 border-b border-castle-line px-4 py-3">
        <TerminalSquare className="h-4 w-4 text-castle-mute" />
        <div className="flex-1">
          <div className="text-sm font-bold text-slate-100">Governed Bash</div>
          <div className="text-[11px] text-castle-mute">
            One verified switch over the native-shell pilot
          </div>
        </div>
        <div
          className={
            "flex items-center gap-2 rounded-xl border px-3 py-1.5 text-xs font-bold " +
            (verified
              ? "border-castle-allow/40 bg-castle-allow/10 text-castle-allow"
              : "border-castle-line bg-black/30 text-castle-mute")
          }
          title={
            verified
              ? "Backend verified the complete security posture"
              : "Not enabled — posture not fully verified"
          }
        >
          {loading ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : verified ? (
            <ShieldCheck className="h-3.5 w-3.5" />
          ) : (
            <ShieldAlert className="h-3.5 w-3.5" />
          )}
          {loading ? "Checking…" : verified ? "ENABLED" : "DISABLED"}
        </div>
      </div>

      <div className="px-4 py-3">
        {posture ? (
          <>
            <div className="grid grid-cols-1 gap-x-8 sm:grid-cols-2">
              <div>
                <div className="mb-1 text-[10px] font-bold uppercase tracking-widest text-castle-mute">
                  Security checks
                </div>
                {Object.entries(CHECK_LABELS).map(([k, label]) => (
                  <CheckRow key={k} label={label} value={posture.checks[k] ?? false} />
                ))}
              </div>
              <div>
                <div className="mb-1 text-[10px] font-bold uppercase tracking-widest text-castle-mute">
                  Enforcement flags
                </div>
                {Object.entries(FLAG_LABELS).map(([k, label]) => (
                  <CheckRow key={k} label={label} value={!!posture.flags[k]} />
                ))}
                <div className="mt-2 break-all text-[11px] text-castle-mute">
                  provider: <code className="text-slate-300">{posture.provider_path || "—"}</code>
                </div>
              </div>
            </div>

            {/* READ-ONLY live execution posture — the single authority the
                PreToolUse adapter + CLI consume. The UI renders it; it never
                re-derives. */}
            {live ? (
              <div className="mt-3 rounded-xl border border-castle-line bg-black/20 px-3 py-2 text-[11px]">
                <div className="mb-1 font-bold uppercase tracking-widest text-castle-mute">
                  Live execution posture (read-only)
                </div>
                <div className="text-slate-300">
                  route=<code>{live.route ?? "—"}</code> · ok=
                  <code>{String(live.ok)}</code>
                </div>
                {live.reason ? <div className="text-castle-mute">{live.reason}</div> : null}
              </div>
            ) : null}

            {error ? (
              <div className="mt-3 rounded-lg border border-red-500/40 bg-red-500/10 px-3 py-2 text-xs text-red-200">
                {error}
              </div>
            ) : null}

            <div className="mt-3 flex items-center gap-2">
              {verified ? (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void doDisable()}
                  className="rounded-lg border border-castle-line px-3 py-1.5 text-xs font-bold text-castle-mute hover:bg-white/[0.04] hover:text-slate-200 disabled:opacity-40"
                >
                  {busy ? "Working…" : "Disable"}
                </button>
              ) : (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void doAllow()}
                  className="rounded-lg border border-castle-allow/35 bg-castle-allow/10 px-3 py-1.5 text-xs font-bold text-castle-allow hover:bg-castle-allow/20 disabled:opacity-40"
                >
                  {busy ? "Validating…" : "Allow shell tools validated and supported by AIDOCS"}
                </button>
              )}
              <button
                type="button"
                disabled={loading}
                onClick={() => void refresh()}
                className="rounded-lg border border-castle-line px-3 py-1.5 text-xs font-bold text-castle-mute hover:bg-white/[0.04] hover:text-slate-200 disabled:opacity-40"
              >
                Re-check
              </button>
            </div>

            {/* Server-issued signed candidate approval card — shown ONLY when
                no system provider auto-enrolled. The operator approves one
                exact offer; the UI never types a path or hash. */}
            {approval && !verified ? (
              <div className="mt-3 space-y-2 rounded-xl border border-amber-500/30 bg-amber-500/[0.06] p-3">
                <div className="text-xs font-bold text-amber-200">{approval.title}</div>
                <div className="text-[11px] text-castle-mute">{approval.detail}</div>
                {approval.candidate_cards.length === 0 ? (
                  <div className="text-[11px] text-castle-mute">
                    No approvable candidates were found on this host.
                  </div>
                ) : (
                  approval.candidate_cards.map((c) => (
                    <div
                      key={c.nonce}
                      className="flex items-center gap-2 rounded-lg border border-castle-line bg-black/20 px-3 py-2"
                    >
                      <div className="flex-1 break-all">
                        <code className="text-[11px] text-slate-300">{c.provider_path}</code>
                        <div className="text-[10px] text-castle-mute">
                          sha256 {c.sha256.slice(0, 16)}… · expires {c.expiry}
                        </div>
                      </div>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => void approveCard(c)}
                        className="rounded-lg border border-castle-allow/35 bg-castle-allow/10 px-3 py-1.5 text-xs font-bold text-castle-allow hover:bg-castle-allow/20 disabled:opacity-40"
                      >
                        Approve this exact provider
                      </button>
                    </div>
                  ))
                )}
              </div>
            ) : null}
          </>
        ) : (
          <div className="py-4 text-center text-xs text-castle-mute">
            {loading ? "Loading posture…" : error || "Posture unavailable."}
          </div>
        )}
      </div>
    </div>
  );
}
