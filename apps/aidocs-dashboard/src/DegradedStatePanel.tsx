import { invoke } from "@tauri-apps/api/core";
import { useState } from "react";
import type { DashboardSnapshot } from "./dashboardApi";

/**
 * SEC-005 (2026-04-23): degraded-state visibility for the operator.
 *
 * Top-bar badge variant (compact): rendered inline with the managed-mode
 *   indicator. Flips to RED when snapshot.degraded_state.degraded is true.
 *
 * Right-panel strip variant (full): rendered always-visible when
 *   degraded, shows reason + time + action buttons.
 *
 * Both variants read from the dashboard_snapshot's degraded_state field
 * — no extra MCP call needed.
 */

type Props = {
    snapshot: DashboardSnapshot | null;
    projectRoot: string | null;
    onReload: () => void;
};

export function DegradedBadge({ snapshot }: { snapshot: DashboardSnapshot | null }) {
    const degraded = !!snapshot?.degraded_state?.degraded;
    return (
        <div
            className={
                degraded
                    ? "mode-badge mode-badge-degraded"
                    : "mode-badge mode-badge-managed"
            }
            title={
                degraded
                    ? `Degraded: ${snapshot?.degraded_state?.reason || "unknown reason"}`
                    : "Managed mode healthy"
            }
            style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                padding: "4px 10px",
                borderRadius: 12,
                fontSize: 11,
                fontWeight: 700,
                letterSpacing: 0.3,
                textTransform: "uppercase",
                background: degraded ? "#5a1a1a" : "#1a3a28",
                color: degraded ? "#ff8a80" : "#6bd49a",
                border: degraded ? "1px solid #a33" : "1px solid #2a5",
            }}
        >
            <span
                style={{
                    display: "inline-block",
                    width: 8,
                    height: 8,
                    borderRadius: 8,
                    background: degraded ? "#ff4444" : "#36c86f",
                }}
            />
            {degraded ? "Degraded" : snapshot?.managed_mode?.active ? "Managed" : "Strict"}
        </div>
    );
}

/** Human age of the degraded condition ("2m ago", "5h ago", "1d 6h ago"). */
export function degradedAge(iso: string, now: number = Date.now()): string {
    const t = Date.parse(iso);
    if (!iso || Number.isNaN(t)) return "";
    const mins = Math.max(0, Math.floor((now - t) / 60000));
    if (mins < 1) return "just now";
    if (mins < 60) return `${mins}m ago`;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.floor(hours / 24);
    return `${days}d ${hours % 24}h ago`;
}

/** Transient prompt-transaction degradation: Reconnect has no bearing on it. */
function isPromptTransactionKind(reason: string): boolean {
    return reason.startsWith("prompt_submit_transaction_degraded");
}

/** A refusal comes back as data ({ok:false,...}); a throw is an Error/string. */
function outcomeError(res: unknown): string | null {
    const r = res as { ok?: boolean; message?: string; reason?: string; blocked_by?: string } | null;
    if (r && r.ok === false) {
        return r.message || r.reason || r.blocked_by || "action refused";
    }
    return null;
}

export function DegradedStrip({ snapshot, projectRoot, onReload }: Props) {
    const [clearing, setClearing] = useState(false);
    const [reconnecting, setReconnecting] = useState(false);
    const [detailOpen, setDetailOpen] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const degraded = snapshot?.degraded_state?.degraded;
    if (!degraded) return null;

    const sessionId = snapshot?.selected_session_id || "";
    const reason = snapshot?.degraded_state?.reason || "unknown";
    const when = snapshot?.degraded_state?.degraded_at || "";
    const eventId = snapshot?.degraded_state?.last_failure_event_id || "";
    const age = degradedAge(when);
    const canReconnect = !isPromptTransactionKind(reason);

    async function doAcknowledge() {
        if (!projectRoot || !sessionId) return;
        setClearing(true);
        setError(null);
        try {
            // Gated CLI: marks the condition resolved/acknowledged in the
            // audit trail (prior reason + event id kept); never deletes it.
            const res = await invoke("clear_degraded_state", {
                projectRoot,
                sessionId,
            });
            const failure = outcomeError(res);
            if (failure) {
                setError(failure);
            } else {
                onReload();
            }
        } catch (err) {
            setError(err instanceof Error ? err.message : String(err));
        } finally {
            setClearing(false);
        }
    }

    async function doReconnect() {
        if (!projectRoot || !sessionId) return;
        setReconnecting(true);
        setError(null);
        try {
            // session_connect is an MCP tool; we can invoke it
            // via toggle_managed_mode which the dashboard already wires.
            const res = await invoke("toggle_managed_mode", {
                projectRoot,
                sessionId,
                activate: true,
            });
            const failure = outcomeError(res);
            if (failure) {
                setError(failure);
                return;
            }
            // Reconnect succeeded: acknowledge the flag through the same
            // gated path, and show its refusal if it has one.
            const cleared = await invoke("clear_degraded_state", {
                projectRoot,
                sessionId,
            });
            const clearFailure = outcomeError(cleared);
            if (clearFailure) {
                setError(clearFailure);
            } else {
                onReload();
            }
        } catch (err) {
            setError(err instanceof Error ? err.message : String(err));
        } finally {
            setReconnecting(false);
        }
    }

    function doRecheck() {
        // Re-read the server's current degraded state. A condition that has
        // recovered resolves itself server-side on the next clean prompt.
        setError(null);
        onReload();
    }

    return (
        <div
            role="alert"
            aria-live="polite"
            style={{
                padding: 10,
                border: "1px solid #a33",
                background: "#2a1010",
                color: "#f5c9c9",
                marginTop: 12,
                marginBottom: 12,
                borderRadius: 6,
                fontSize: 12,
            }}
        >
            <div
                style={{
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "baseline",
                    marginBottom: 6,
                }}
            >
                <strong style={{ color: "#ff8a80" }}>⚠ DEGRADED</strong>
                <span style={{ opacity: 0.7, fontFamily: "monospace" }}>
                    {when}
                    {age ? ` · ${age}` : ""}
                </span>
            </div>
            <div style={{ marginBottom: 4 }}>
                <strong>Reason:</strong>{" "}
                <span style={{ fontFamily: "monospace", opacity: 0.9 }}>{reason}</span>
            </div>
            {sessionId ? (
                <div style={{ marginBottom: 4 }}>
                    <strong>Affected:</strong>{" "}
                    <span style={{ fontFamily: "monospace", opacity: 0.9 }}>{sessionId}</span>
                </div>
            ) : null}
            {eventId ? (
                <div style={{ marginBottom: 8 }}>
                    <strong>Event:</strong>{" "}
                    <span style={{ fontFamily: "monospace", opacity: 0.8 }}>{eventId}</span>
                </div>
            ) : null}
            {error ? (
                <div
                    role="alert"
                    style={{ marginBottom: 8, color: "#ffb4a8", fontFamily: "monospace" }}
                >
                    {error}
                </div>
            ) : null}
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                <button
                    type="button"
                    className="action-button"
                    onClick={doRecheck}
                    title="Re-read the current degraded state from the server"
                    style={{ fontSize: 11 }}
                >
                    Recheck
                </button>
                {canReconnect ? (
                    <button
                        type="button"
                        className="action-button"
                        disabled={reconnecting}
                        onClick={doReconnect}
                        style={{ fontSize: 11 }}
                    >
                        {reconnecting ? "Reconnecting..." : "Reconnect"}
                    </button>
                ) : null}
                <button
                    type="button"
                    className="action-button"
                    disabled={clearing}
                    onClick={doAcknowledge}
                    title="Mark this condition acknowledged (the audit record is kept)"
                    style={{ fontSize: 11 }}
                >
                    {clearing ? "Acknowledging..." : "Acknowledge"}
                </button>
                <button
                    type="button"
                    className="action-button"
                    onClick={() => setDetailOpen((v) => !v)}
                    style={{ fontSize: 11 }}
                >
                    {detailOpen ? "Hide Details" : "View Details"}
                </button>
            </div>
            {detailOpen ? (
                <pre
                    style={{
                        marginTop: 8,
                        padding: 6,
                        background: "#1a0808",
                        border: "1px solid #553",
                        fontSize: 11,
                        maxHeight: 160,
                        overflow: "auto",
                        whiteSpace: "pre-wrap",
                    }}
                >
                    {JSON.stringify(snapshot?.degraded_state, null, 2)}
                </pre>
            ) : null}
        </div>
    );
}
