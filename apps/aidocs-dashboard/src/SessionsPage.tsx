import { useState } from "react";
import { configEditingAvailable } from "./entitlements";
import { GateHealthCard } from "./GateHealthCard";
import type { SessionsPageProps } from "./dashboardTypes";
import {
  actionAuthorityLabel,
  actionResultNotice,
  type ActionNotice,
  type AuthorityResult,
} from "./authorityPresentation";
import { SessionDetailModal } from "./SessionDetailModal";
import { describeHost, hostBindingsForSession, sessionDisplayName, type SessionRow } from "./sessionDetail";

// Delete/Connect are mutating, admin-gated session actions — label them so the
// operator sees the authority required (not a bare "Delete"/"Connect").
const DELETE_AUTH = actionAuthorityLabel("delete_session");
const CONNECT_AUTH = actionAuthorityLabel("connect_session");

const NOTICE_COLOR: Record<ActionNotice["severity"], string> = {
  info: "#34d399",
  warning: "#f59e0b",
  danger: "#f87171",
};

function lifecycleLabel(status: string | null) {
  if (!status) return "unknown";
  if (status === "active") return "open";
  return status;
}

export function SessionsPage({ sessions, connectedAgents, sessionValue, onSelectSession, onDeleteSession, projectRoot, deletingSessionId, degradedState, gateHealth, onCreateSession, onConnectSession }: SessionsPageProps) {
  const [newTitle, setNewTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<ActionNotice | null>(null);
  // Row click opens details rather than silently changing the dashboard's
  // selection: the popup carries the ids, the host binding and the copyable
  // CLI command, and offers "Load in dashboard" as an explicit action.
  const [detailSessionId, setDetailSessionId] = useState<string | null>(null);
  const detailSession =
    (sessions as SessionRow[] | undefined)?.find((s) => s.session_id === detailSessionId) ?? null;

  /** Host cell for the sessions table: the live binding if there is one, else
   *  the most recent, else an explicit "no host". Never fabricates a value —
   *  a session that has never been worked genuinely has no host. */
  function hostCellFor(sessionId: string) {
    const bindings = hostBindingsForSession(connectedAgents?.agents, sessionId);
    if (!bindings.length) return <span style={{ opacity: 0.45 }}>no host</span>;
    const primary = bindings[0];
    const extra = bindings.length > 1 ? ` +${bindings.length - 1}` : "";
    return (
      <span title={`${describeHost(primary)} · ${primary.role}${primary.live ? " · live" : ""}`}>
        <span style={{ opacity: primary.live ? 1 : 0.6 }}>{describeHost(primary)}</span>
        {extra ? <span style={{ opacity: 0.5 }}>{extra}</span> : null}
      </span>
    );
  }

  async function handleCreate() {
    if (!onCreateSession || !newTitle.trim()) return;
    setBusy(true);
    try {
      const res = (await onCreateSession(newTitle.trim())) as AuthorityResult;
      const sid = (res.session_id as string | undefined) ?? newTitle.trim();
      setNotice(actionResultNotice(res, `Created session: ${sid}`));
      if (res.ok !== false && !res.blocked_by) setNewTitle("");
    } catch (e) {
      setNotice({ ok: false, severity: "danger", text: String(e) });
    } finally {
      setBusy(false);
    }
  }

  async function handleConnect(sessionId: string) {
    if (!onConnectSession) return;
    setBusy(true);
    try {
      const res = (await onConnectSession(sessionId)) as AuthorityResult;
      setNotice(actionResultNotice(res, `Connected to session: ${sessionId}`));
    } catch (e) {
      setNotice({ ok: false, severity: "danger", text: String(e) });
    } finally {
      setBusy(false);
    }
  }
  // SEC-005: degraded_state in the snapshot refers to the currently
  // selected session. Render a red chip on that session's row so the
  // sessions list mirrors the sidebar badge + right-panel strip.
  const selectedDegraded = !!degradedState?.degraded;
  return (
    <section className="page">
      {/* Gate liveness — FIRST thing on the page the operator uses to stamp a
          session, because a session stamped while the gate is dead is an
          ungoverned session that looks perfectly normal. Renders green ONLY on
          an explicit ok; unknown is amber, never a pass. */}
      <GateHealthCard health={gateHealth} />
      {/* Create-session action (mutating, admin-gated). Its result renders the
          shared authority notice — owner_grant/ownership_degraded on success,
          the operator_auth/refusal banner on a refused create. */}
      {onCreateSession && (
        <div style={{ display: "flex", gap: 6, alignItems: "center", marginBottom: 8 }}>
          <input
            className="session-create-input"
            placeholder="New session title…"
            value={newTitle}
            onChange={(e) => setNewTitle(e.target.value)}
            disabled={busy}
            style={{ fontSize: "0.72rem", padding: "3px 6px" }}
          />
          <button
            type="button"
            className="action-button action-button-compact"
            style={{ fontSize: "0.72rem" }}
            disabled={busy || !newTitle.trim() || !configEditingAvailable()}
            onClick={handleCreate}
            title={`${actionAuthorityLabel("create_session").label} action — requires an authenticated operator`}
          >
            Create
            <span style={{ marginLeft: 4, opacity: 0.6, fontSize: "0.6rem" }}>
              {actionAuthorityLabel("create_session").label}
            </span>
          </button>
        </div>
      )}
      {notice && (
        <div
          role="status"
          style={{ marginBottom: 8, fontSize: "0.72rem", color: NOTICE_COLOR[notice.severity] }}
        >
          {notice.text}
        </div>
      )}
      <div className="flat-table">
        <div className="table-head session-table-row" aria-hidden="true">
          <span>Session</span>
          <span>Status</span>
          {/* Was "Owner", which showed sessions.owner — a WRITE-PROVENANCE
              field holding the name of the code path that last stamped the row
              ('managed_mode_connect_restamp' and friends outnumber real agent
              names in the ledger). Host identity is the useful answer to "whose
              session is this", and it is recorded properly, so the column now
              shows host type + id. The raw provenance value is still visible in
              the detail popup, labelled for what it is. */}
          <span title="Host type and host session id bound to this session">Host</span>
          <span>Updated</span>
          <span>Actions</span>
        </div>
        {sessions.map((session) => (
          <div
            key={session.session_id}
            className={session.session_id === sessionValue ? "table-row session-table-row is-selected" : "table-row session-table-row"}
          >
            <button
              className="session-select-button"
              type="button"
              onClick={() => setDetailSessionId(session.session_id)}
              title="Show session details, host binding and connect command"
              style={{ display: "contents" }}
            >
              <span className="session-table-cell session-table-cell-title">
                {/* sessionDisplayName, NOT `title ?? session_id`: the ledger
                    stores "" for untitled sessions and `??` only falls back on
                    null/undefined, so a blank title rendered a NAMELESS ROW
                    whose delete dialog still knew its id. */}
                {sessionDisplayName(session)}
                {session.managed ? <span className="setting-own" style={{ marginLeft: "8px" }} title="Managed mode is currently bound to this session">managed</span> : null}
                {session.owner_granted ? <span className="setting-inherited" style={{ marginLeft: "8px", opacity: 0.85 }} title="A session-scoped owner grant exists for this session">owned</span> : null}
                {session.selected ? <span className="setting-inherited" style={{ marginLeft: "8px", opacity: 1 }} title="Currently selected in the dashboard">current</span> : null}
                {session.session_id === sessionValue && selectedDegraded ? (
                  <span
                    style={{
                      marginLeft: "8px",
                      padding: "2px 6px",
                      borderRadius: 8,
                      fontSize: "0.65rem",
                      fontWeight: 700,
                      letterSpacing: 0.3,
                      textTransform: "uppercase",
                      background: "#5a1a1a",
                      color: "#ff8a80",
                      border: "1px solid #a33",
                    }}
                    title={`Degraded: ${degradedState?.reason || "unknown reason"} (failure event ${degradedState?.last_failure_event_id || "n/a"})`}
                  >
                    ⚠ degraded
                  </span>
                ) : null}
              </span>
              <span className="session-table-cell" title={session.status === "active" ? "Open session lifecycle state" : undefined}>{lifecycleLabel(session.status)}</span>
              <span className="session-table-cell">{hostCellFor(session.session_id)}</span>
              <span className="session-table-cell">{session.last_updated ?? "n/a"}</span>
            </button>
            <span className="session-table-cell session-table-cell-actions">
              {onConnectSession && (
                <button
                  type="button"
                  className="action-button action-button-compact"
                  style={{ fontSize: "0.72rem", marginRight: 4 }}
                  disabled={busy}
                  onClick={() => handleConnect(session.session_id)}
                  title={`${CONNECT_AUTH.label} action — bind managed mode (membership-gated)`}
                >
                  Connect
                </button>
              )}
              <button
                type="button"
                className="action-button action-button-compact"
                style={{ fontSize: "0.72rem" }}
                disabled={deletingSessionId === session.session_id || !configEditingAvailable()}
                onClick={() => onDeleteSession(session.session_id)}
                title={`${DELETE_AUTH.label} action — requires an authenticated operator`}
              >
                {deletingSessionId === session.session_id ? "..." : "Delete"}
                <span style={{ marginLeft: 4, opacity: 0.6, fontSize: "0.6rem" }}>
                  {DELETE_AUTH.label}
                </span>
              </button>
            </span>
          </div>
        ))}
      </div>
        <SessionDetailModal
          session={detailSession}
          agents={connectedAgents?.agents}
          projectRoot={projectRoot ?? null}
          close={() => setDetailSessionId(null)}
          onLoad={onSelectSession}
          onConnect={onConnectSession ? (sid) => void handleConnect(sid) : undefined}
        />
        <div style={{ marginTop: 18 }}>
          <div className="table-head" style={{ marginBottom: 6 }}>
            Connected agents ({connectedAgents?.live_count ?? 0})
          </div>
          <div className="flat-table">
            <div className="table-head session-table-row" aria-hidden="true">
              <span>Host actor</span>
              <span>Role</span>
              <span>Work session</span>
              <span>Host</span>
              <span>Status</span>
            </div>
            {(connectedAgents?.agents ?? []).map((agent) => (
              <div key={agent.host_session_id} className="table-row session-table-row">
                <span className="session-table-cell session-table-cell-title">{agent.host_session_id}</span>
                <span className="session-table-cell">{agent.role || "conductor"}</span>
                <span className="session-table-cell">{agent.session_id || "unbound"}</span>
                <span className="session-table-cell">{agent.host_kind || "unknown"}</span>
                <span className="session-table-cell">{agent.live ? "live" : "offline"}</span>
              </div>
            ))}
            {!connectedAgents?.agents?.length ? (
              <div className="table-row"><span className="session-table-cell">No live host actors reported.</span></div>
            ) : null}
          </div>
          <p style={{ marginTop: 6, fontSize: "0.68rem", opacity: 0.7 }}>
            Host actors are shown separately from durable work sessions; a host ID is never a selectable session ID.
          </p>
        </div>

    </section>
  );
}
