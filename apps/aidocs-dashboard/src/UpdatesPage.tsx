/**
 * UpdatesPage — auto-update checker surface (Empire directive 2026-07-06).
 *
 * Shows the running AIDOCS version vs the release channel's latest, the
 * service watchdog health, and a real "Check now" lever (runs the same
 * check_for_update the watchdog runs on start + every 6h). CHECK-ONLY by
 * law (aidocs-doctrine §XXIV): this page never installs anything — when an
 * update is available it says so and names the operator path. No fake
 * levers: on the web build the local service is unreachable and the page
 * says exactly that instead of pretending.
 */
import { useCallback, useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { isWebBuild } from "./webmcpScope";

type UpdateState = {
  current?: string | null;
  latest?: string | null;
  update_available?: boolean;
  channel?: string | null;
  checked_at?: string | null;
  release_url?: string | null;
  error?: string | null;
};

type ServiceState = {
  status?: string;
  port?: number;
  pid?: number;
  daemon_alive?: boolean;
  started_at?: string;
  update?: UpdateState;
};

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div style={{ display: "flex", gap: 12, padding: "6px 0", borderBottom: "1px solid #1e293b" }}>
      <div style={{ width: 180, color: "#94a3b8", fontSize: 13 }}>{label}</div>
      <div style={{ fontSize: 13 }}>{value}</div>
    </div>
  );
}

export function UpdatesPage() {
  const [service, setService] = useState<ServiceState | null>(null);
  const [update, setUpdate] = useState<UpdateState | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadStatus = useCallback(async () => {
    try {
      const res = (await invoke("update_status")) as { service?: ServiceState };
      setService(res?.service ?? null);
      setUpdate(res?.service?.update ?? null);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  const checkNow = useCallback(async () => {
    setBusy(true);
    try {
      const res = (await invoke("update_check")) as { update?: UpdateState };
      setUpdate(res?.update ?? null);
      setError(null);
      await loadStatus();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }, [loadStatus]);

  useEffect(() => {
    if (!isWebBuild()) void loadStatus();
  }, [loadStatus]);

  if (isWebBuild()) {
    return (
      <section className="page">
        <section className="flat-panel">
          <div className="section-label">Updates</div>
          <p>
            Update checking runs against the LOCAL aidocs service watchdog and is available in the
            desktop dashboard only. This web view has no access to the local daemon — that is a
            truthful limitation, not a broken button.
          </p>
        </section>
      </section>
    );
  }

  const available = update?.update_available === true;
  return (
    <section className="page">
      <section className="flat-panel">
        <div className="section-label">AIDOCS Updates</div>
        {available ? (
          <div style={{ background: "#052e1b", border: "1px solid #10b981", borderRadius: 8, padding: "10px 14px", margin: "10px 0", fontSize: 13 }}>
            Update available: <b>{update?.latest}</b> (running {update?.current}).
            {update?.release_url ? (
              <>
                {" "}Release notes: <a href={update.release_url} target="_blank" rel="noreferrer">{update.release_url}</a>.
              </>
            ) : null}
            {" "}This checker never installs — update via the operator path (deploy gate / verified installer);
            the watchdog drains and restarts the daemon automatically once new code lands.
          </div>
        ) : (
          <div style={{ color: "#94a3b8", fontSize: 13, margin: "10px 0" }}>
            {update?.checked_at
              ? update?.error
                ? `Last check failed (${update.error}) — result is stale, not green.`
                : `Up to date as of ${update.checked_at}.`
              : "No check recorded yet — the watchdog checks on start and every 6 hours, or press Check now."}
          </div>
        )}
        <Row label="Running version" value={update?.current || service?.update?.current || "unknown"} />
        <Row label="Latest release" value={update?.latest || "unknown"} />
        <Row label="Channel" value={update?.channel || "default (GitHub releases)"} />
        <Row label="Last checked" value={update?.checked_at || "never"} />
        {update?.error ? <Row label="Check error" value={<span style={{ color: "#f87171" }}>{update.error}</span>} /> : null}
        <div style={{ marginTop: 12 }}>
          <button onClick={() => void checkNow()} disabled={busy} style={{ borderRadius: 8, border: "1px solid #334155", padding: "6px 14px", fontSize: 13, cursor: busy ? "wait" : "pointer" }}>
            {busy ? "Checking…" : "Check now"}
          </button>
        </div>
      </section>
      <section className="flat-panel" style={{ marginTop: 12 }}>
        <div className="section-label">Local Service (watchdog)</div>
        <Row label="Status" value={service?.status || "unknown"} />
        <Row label="Daemon alive" value={service?.daemon_alive ? "yes" : "no"} />
        <Row label="Port" value={service?.port ?? "—"} />
        <Row label="Started" value={service?.started_at || "—"} />
        <p style={{ color: "#64748b", fontSize: 12, marginTop: 10 }}>
          The watchdog restarts the daemon on crash (capped backoff, crash-loop breaker) and
          drains+restarts when new code lands (release-marker watch) — so every aidocs-enabled
          project picks up updates automatically. Manage with <code>aidocs service start|stop|status</code>.
        </p>
        {error ? <p style={{ color: "#f87171", fontSize: 12 }}>{error}</p> : null}
      </section>
    </section>
  );
}
