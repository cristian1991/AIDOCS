import { useState } from "react";

import {
  describeHost,
  hostBindingsForSession,
  sessionCliCommands,
  sessionDisplayName,
  sessionTitleIsMissing,
  type HostAgent,
  type SessionRow,
} from "./sessionDetail";

export type SessionDetailModalProps = {
  session: SessionRow | null;
  agents?: HostAgent[];
  projectRoot: string | null;
  close: () => void;
  /** Load this session in the dashboard (selection only — not a managed bind). */
  onLoad: (sessionId: string) => void;
  /** Bind managed mode to this session. Absent in read-only contexts. */
  onConnect?: (sessionId: string) => void;
};

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: "flex", gap: 10, padding: "3px 0", fontSize: "0.78rem" }}>
      <span style={{ minWidth: 116, opacity: 0.6, flexShrink: 0 }}>{label}</span>
      <span style={{ wordBreak: "break-word" }}>{children}</span>
    </div>
  );
}

function CopyRow({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div style={{ display: "flex", gap: 8, alignItems: "flex-start", marginTop: 4 }}>
      <code
        style={{
          flex: 1,
          padding: "6px 8px",
          borderRadius: 6,
          background: "rgba(255,255,255,0.05)",
          fontSize: "0.72rem",
          lineHeight: 1.5,
          overflowX: "auto",
          whiteSpace: "pre",
        }}
      >
        {value}
      </code>
      <button
        type="button"
        className="action-button action-button-small"
        onClick={() => {
          void navigator.clipboard.writeText(value);
          setCopied(true);
          window.setTimeout(() => setCopied(false), 1200);
        }}
      >
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}

export function SessionDetailModal({
  session,
  agents,
  projectRoot,
  close,
  onLoad,
  onConnect,
}: SessionDetailModalProps) {
  if (!session) return null;

  const name = sessionDisplayName(session);
  const untitled = sessionTitleIsMissing(session);
  const bindings = hostBindingsForSession(agents, session.session_id);
  const commands = sessionCliCommands(projectRoot, session.session_id);

  return (
    <div
      className="modal-backdrop"
      role="dialog"
      aria-modal="true"
      aria-label={`Session details for ${name}`}
      onClick={close}
    >
      <div className="modal-panel" onClick={(event) => event.stopPropagation()}>
        <div className="page-header modal-header">
          <div>
            <div className="section-label">Session</div>
            <h3 style={{ marginBottom: 0 }}>
              {name}
              {untitled ? (
                <span style={{ marginLeft: 8, fontSize: "0.65rem", opacity: 0.55, fontWeight: 400 }}>
                  (untitled — showing its id)
                </span>
              ) : null}
            </h3>
          </div>
          <button className="action-button action-button-small modal-close" type="button" onClick={close}>
            Close
          </button>
        </div>

        <div style={{ padding: "10px 4px" }}>
          <Field label="Session id">
            <code style={{ fontSize: "0.72rem" }}>{session.session_id}</code>
          </Field>
          <Field label="Status">{session.status || "unknown"}</Field>
          <Field label="Last updated">{session.last_updated || "n/a"}</Field>
          <Field label="Goal">{session.goal || <span style={{ opacity: 0.5 }}>none recorded</span>}</Field>
          <Field label="State">
            {[
              session.managed ? "managed bind" : null,
              session.selected ? "selected" : null,
              session.owner_granted ? "owner grant" : null,
            ]
              .filter(Boolean)
              .join(" · ") || <span style={{ opacity: 0.5 }}>no bind or grant</span>}
          </Field>
          {/* `owner` is a WRITE-PROVENANCE field, not an identity: the restamp
              paths store their own function name in it (values like
              'managed_mode_connect_restamp' outnumber real agent names in the
              ledger). Labelled for what it is rather than presented as an
              owner, so nobody reads a code path as a person. */}
          <Field label="Stamped by">
            {session.owner || <span style={{ opacity: 0.5 }}>unstamped</span>}
            <span style={{ marginLeft: 6, opacity: 0.45, fontSize: "0.68rem" }}>
              (ledger write provenance, not an owner)
            </span>
          </Field>
        </div>

        <div style={{ padding: "6px 4px" }}>
          <div className="section-label" style={{ marginBottom: 4 }}>
            Host bindings ({bindings.length})
          </div>
          {bindings.length ? (
            <div className="flat-table">
              {bindings.map((agent) => (
                <div
                  key={`${agent.host_session_id}:${agent.role}:${agent.last_updated ?? ""}`}
                  style={{
                    display: "flex",
                    gap: 10,
                    alignItems: "center",
                    padding: "5px 0",
                    fontSize: "0.74rem",
                  }}
                >
                  <span
                    title={agent.live ? "Host is live" : "Host is not live"}
                    style={{
                      padding: "1px 6px",
                      borderRadius: 8,
                      fontSize: "0.62rem",
                      fontWeight: 700,
                      textTransform: "uppercase",
                      background: agent.live ? "#123a1d" : "#3a2012",
                      color: agent.live ? "#7fe0a0" : "#e0a87f",
                      border: `1px solid ${agent.live ? "#2f6b40" : "#6b452f"}`,
                    }}
                  >
                    {agent.live ? "live" : "idle"}
                  </span>
                  <code style={{ fontSize: "0.7rem" }}>{describeHost(agent)}</code>
                  <span style={{ opacity: 0.6 }}>{agent.role}</span>
                  {/* pid is only present for workers whose in-worker plugin
                      stamped it; older rows never recorded one at all. */}
                  <span style={{ opacity: 0.5, fontSize: "0.68rem" }}>
                    {agent.pid ? `pid ${agent.pid}` : "pid not stamped"}
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <p style={{ fontSize: "0.74rem", opacity: 0.55, margin: "2px 0 0" }}>
              No host has bound this session. Host type and id are recorded when an agent
              attaches, so a session that has never been worked shows none.
            </p>
          )}
        </div>

        <div style={{ padding: "10px 4px 4px" }}>
          <div className="section-label" style={{ marginBottom: 2 }}>
            Connect via CLI
          </div>
          {commands.map((cmd) => (
            <div key={cmd.command} style={{ marginBottom: 8 }}>
              <div style={{ fontSize: "0.72rem", opacity: 0.7 }}>{cmd.label}</div>
              <CopyRow value={cmd.command} />
              {cmd.note ? (
                <div style={{ fontSize: "0.68rem", opacity: 0.5, marginTop: 3 }}>{cmd.note}</div>
              ) : null}
            </div>
          ))}
        </div>

        <div
          style={{
            display: "flex",
            gap: 8,
            justifyContent: "flex-end",
            padding: "8px 4px 2px",
            borderTop: "1px solid rgba(255,255,255,0.08)",
          }}
        >
          <button
            type="button"
            className="action-button action-button-small"
            onClick={() => {
              onLoad(session.session_id);
              close();
            }}
          >
            Load in dashboard
          </button>
          {onConnect ? (
            <button
              type="button"
              className="action-button action-button-small"
              title="Bind managed mode to this session (membership-gated)"
              onClick={() => {
                onConnect(session.session_id);
                close();
              }}
            >
              Bind managed mode
            </button>
          ) : null}
        </div>
      </div>
    </div>
  );
}
