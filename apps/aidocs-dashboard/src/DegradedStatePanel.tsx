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

export function DegradedStrip({ snapshot, projectRoot, onReload }: Props) {
    const [clearing, setClearing] = useState(false);
    const [reconnecting, setReconnecting] = useState(false);
    const [detailOpen, setDetailOpen] = useState(false);

    const degraded = snapshot?.degraded_state?.degraded;
    if (!degraded) return null;

    const sessionId = snapshot?.selected_session_id || "";
    const reason = snapshot?.degraded_state?.reason || "unknown";
    const when = snapshot?.degraded_state?.degraded_at || "";
    const eventId = snapshot?.degraded_state?.last_failure_event_id || "";

    async function doClearState() {
        if (!projectRoot || !sessionId) return;
        setClearing(true);
        try {
            await invoke("clear_degraded_state", {
                projectRoot,
                sessionId,
            });
            onReload();
        } finally {
            setClearing(false);
        }
    }

    async function doReconnect() {
        if (!projectRoot || !sessionId) return;
        setReconnecting(true);
        try {
            // session_connect is an MCP tool; we can invoke it
            // via toggle_managed_mode which the dashboard already wires.
            await invoke("toggle_managed_mode", {
                projectRoot,
                sessionId,
                activate: true,
            });
            // Clear degraded AFTER a successful reconnect — the session
            // may already be healthy and the flag should follow.
            await invoke("clear_degraded_state", {
                projectRoot,
                sessionId,
            });
            onReload();
        } finally {
            setReconnecting(false);
        }
    }

    function doRetry() {
        // Retry is context-sensitive; without a specific failed action
        // queued, Retry currently behaves as "reload snapshot."
        // Extended retry semantics land when we wire the attempted-
        // action store in a follow-up ticket.
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
                <span style={{ opacity: 0.7, fontFamily: "monospace" }}>{when}</span>
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
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                <button
                    type="button"
                    className="action-button"
                    onClick={doRetry}
                    style={{ fontSize: 11 }}
                >
                    Retry
                </button>
                <button
                    type="button"
                    className="action-button"
                    disabled={reconnecting}
                    onClick={doReconnect}
                    style={{ fontSize: 11 }}
                >
                    {reconnecting ? "Reconnecting..." : "Reconnect"}
                </button>
                <button
                    type="button"
                    className="action-button"
                    disabled={clearing}
                    onClick={doClearState}
                    style={{ fontSize: 11 }}
                >
                    {clearing ? "Clearing..." : "Clear State"}
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
