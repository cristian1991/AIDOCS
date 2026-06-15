import { useEffect, useMemo, useRef, useState } from "react";
import { DiffViewer } from "./DiffViewer";
import { formatTimestamp } from "./dashboardUtils";
import type { ExecutionPageProps } from "./dashboardTypes";
import {
  eventAuthority,
  actionAuthorityLabel,
  type AuthorityEvent,
} from "./authorityPresentation";

function FilterDropdown({ label, value, options, onChange }: {
  label: string;
  value: string;
  options: string[];
  onChange: (value: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  return (
    <div ref={ref} style={{ position: "relative", flex: 1, minWidth: "120px" }}>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        style={{
          background: "var(--bg-3)", color: "var(--text)", border: "1px solid var(--line)",
          borderRadius: "var(--radius-sm)", padding: "5px 24px 5px 8px", fontSize: "0.8rem",
          cursor: "pointer", width: "100%", textAlign: "left", fontFamily: "inherit",
          position: "relative",
        }}
      >
        {value === "all" ? label : value}
        <span style={{ position: "absolute", right: "8px", top: "50%", transform: "translateY(-50%)", color: "var(--text-faint)", fontSize: "0.7rem" }}>▾</span>
      </button>
      {open && (
        <div style={{
          position: "absolute", top: "100%", left: 0, right: 0, zIndex: 50,
          background: "var(--bg-3)", border: "1px solid var(--line)", borderRadius: "var(--radius-sm)",
          maxHeight: "200px", overflow: "auto", marginTop: "2px",
          boxShadow: "0 8px 24px rgba(0,0,0,0.4)",
        }}>
          {options.map((opt) => (
            <button
              key={opt}
              type="button"
              onClick={() => { onChange(opt); setOpen(false); }}
              style={{
                display: "block", width: "100%", padding: "5px 8px", border: 0,
                background: opt === value ? "rgba(140, 224, 175, 0.12)" : "transparent",
                color: opt === value ? "var(--accent-bright)" : "var(--text)",
                cursor: "pointer", textAlign: "left", fontSize: "0.8rem", fontFamily: "inherit",
              }}
            >
              {opt}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

type ExecutionFilterState = {
  eventKind: string;
  status: string;
  failuresOnly: boolean;
};

// SEC-005 (2026-04-23): event kinds that represent enforcement
// failures or degraded subsystem signals. Rows matching any of these
// get red-highlighted in the stream and surface when the operator
// flips the Failures-Only toggle. The set is deliberately tight —
// routine blocks are already colored via existing isBlocked logic.
const FAILURE_EVENT_KINDS: ReadonlySet<string> = new Set([
  "prompt_mutation_failed",
  "prompt_mutation_rolled_back",
  "enforcement_disabled_bypass",
  "tool_call_failed",
  "lane_tool_block",
  "raw_tool_block",
  "raw_shell_block",
  "edit_redirect_block",
  "judge_block",
  "rbac_denied",
  "path_zone_block",
]);

function isFailureEvent(event: ExecutionPageProps["recentExecution"][number]): boolean {
  const kind = String(event.event_kind ?? "");
  if (FAILURE_EVENT_KINDS.has(kind)) return true;
  const status = String(event.status ?? "");
  if (status === "failed" || status === "rolled_back" || status === "blocked") return true;
  return false;
}

function eventLabel(event: ExecutionPageProps["recentExecution"][number]) {
  return event.capability_name ?? event.action_kind ?? event.event_kind;
}

function eventLane(event: ExecutionPageProps["recentExecution"][number]) {
  const payload = (event.payload ?? {}) as Record<string, unknown>;
  return String(payload.lane_id ?? payload.target_lane_id ?? payload.paused_lane_id ?? payload.conflicting_lane_id ?? event.target_entity ?? "").trim();
}

function eventStatus(event: ExecutionPageProps["recentExecution"][number]) {
  return String(event.status ?? "unknown");
}

function detailValue(value: unknown) {
  if (value == null) return null;
  if (typeof value === "string") return value;
  return JSON.stringify(value, null, 2);
}

function payloadString(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function routingAudit(payload: Record<string, unknown>) {
  const logicalMode = payloadString(payload.logical_mode);
  const nativeMode = payloadString(payload.native_mode);
  const fallbackUsed = payload.fallback_used === true;
  return { logicalMode, nativeMode, fallbackUsed, visible: !!logicalMode || !!nativeMode || fallbackUsed };
}

function AuditBadges({ logicalMode, nativeMode, fallbackUsed }: { logicalMode: string; nativeMode: string; fallbackUsed: boolean }) {
  if (!logicalMode && !nativeMode && !fallbackUsed) return null;
  return (
    <span style={{ display: "inline-flex", flexWrap: "wrap", gap: 4, alignItems: "center" }}>
      {logicalMode ? <small className="execution-event-tokens">L:{logicalMode}</small> : null}
      {nativeMode ? <small className="execution-event-tokens">N:{nativeMode}</small> : null}
      {fallbackUsed ? <small className="execution-event-lane" style={{ color: "#fbbf24" }}>fallback</small> : null}
    </span>
  );
}

export function ExecutionPage({ recentExecution, onClearToolCalls, clearing }: ExecutionPageProps) {
    const [filters, setFilters] = useState<ExecutionFilterState>({ eventKind: "all", status: "all", failuresOnly: false });
    const [selectedEvent, setSelectedEvent] = useState<ExecutionPageProps["recentExecution"][number] | null>(null);

  const eventKinds = useMemo(() => ["all", ...Array.from(new Set(recentExecution.map((event) => event.event_kind))).sort()], [recentExecution]);
  const statuses = useMemo(() => ["all", ...Array.from(new Set(recentExecution.map(eventStatus))).sort()], [recentExecution]);

    useEffect(() => {
      const handler = (e: KeyboardEvent) => { if (e.key === "Escape") setSelectedEvent(null); };
      document.addEventListener("keydown", handler);
      return () => document.removeEventListener("keydown", handler);
    }, []);

    const filtered = useMemo(() => recentExecution.filter((event) => {
    if (filters.eventKind !== "all" && event.event_kind !== filters.eventKind) return false;
    if (filters.status !== "all" && eventStatus(event) !== filters.status) return false;
    if (filters.failuresOnly && !isFailureEvent(event)) return false;
    return true;
  }), [filters, recentExecution]);

    const totalEvents = recentExecution.length;
    const toolCalls = recentExecution.filter((e) => e.event_kind === "tool_call_completed" || e.event_kind === "tool_call_started").length;
    const blocked = recentExecution.filter((e) => e.status === "blocked" || e.event_kind?.includes("block")).length;
    const failed = recentExecution.filter((e) => e.status === "failed" || e.event_kind === "tool_call_failed").length;
    const guardFindings = recentExecution.filter((e) => e.event_kind?.includes("output_guard")).length;
    const totalTokens = recentExecution.reduce((sum, e) => {
      const p = (e.payload ?? {}) as Record<string, unknown>;
      return sum + Number(p.tokens_in_estimate ?? 0) + Number(p.tokens_out_estimate ?? 0);
    }, 0);

    return (
      <section className="page page-config">
          <div className="page-fixed-header" style={{ display: "flex", flexDirection: "column", gap: "8px", width: "100%" }}>
            <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(6, 1fr)", gap: "8px", flex: 1 }}>
                <div className="flat-panel" style={{ padding: "6px 10px", textAlign: "center" }}>
                  <div style={{ fontSize: "0.68rem", color: "var(--text-faint)", textTransform: "uppercase" }}>Events</div>
                  <strong style={{ fontSize: "1.1rem" }}>{totalEvents}</strong>
                </div>
                <div className="flat-panel" style={{ padding: "6px 10px", textAlign: "center" }}>
                  <div style={{ fontSize: "0.68rem", color: "var(--text-faint)", textTransform: "uppercase" }}>Tool Calls</div>
                  <strong style={{ fontSize: "1.1rem" }}>{toolCalls}</strong>
                </div>
                <div className="flat-panel" style={{ padding: "6px 10px", textAlign: "center" }}>
                  <div style={{ fontSize: "0.68rem", color: blocked > 0 ? "#f87171" : "var(--text-faint)", textTransform: "uppercase" }}>Blocked</div>
                  <strong style={{ fontSize: "1.1rem", color: blocked > 0 ? "#f87171" : "inherit" }}>{blocked}</strong>
                </div>
                <div className="flat-panel" style={{ padding: "6px 10px", textAlign: "center" }}>
                  <div style={{ fontSize: "0.68rem", color: failed > 0 ? "#fbbf24" : "var(--text-faint)", textTransform: "uppercase" }}>Failed</div>
                  <strong style={{ fontSize: "1.1rem", color: failed > 0 ? "#fbbf24" : "inherit" }}>{failed}</strong>
                </div>
                <div className="flat-panel" style={{ padding: "6px 10px", textAlign: "center" }}>
                  <div style={{ fontSize: "0.68rem", color: guardFindings > 0 ? "#f87171" : "var(--text-faint)", textTransform: "uppercase" }}>Guard</div>
                  <strong style={{ fontSize: "1.1rem", color: guardFindings > 0 ? "#f87171" : "inherit" }}>{guardFindings}</strong>
                </div>
                <div className="flat-panel" style={{ padding: "6px 10px", textAlign: "center" }}>
                  <div style={{ fontSize: "0.68rem", color: "var(--text-faint)", textTransform: "uppercase" }}>Tokens</div>
                  <strong style={{ fontSize: "1.1rem" }}>{totalTokens > 1000 ? `${(totalTokens / 1000).toFixed(1)}k` : totalTokens}</strong>
                </div>
              </div>
              <button
                type="button"
                className="action-button action-button-compact"
                disabled={clearing}
                onClick={onClearToolCalls}
                style={{ fontSize: "0.72rem", whiteSpace: "nowrap" }}
                title={`${actionAuthorityLabel("clear_tool_calls").label} action — clears recorded tool calls`}
              >
                {clearing ? "..." : "Reset"}
                <span
                  className="execution-event-tokens"
                  style={{ marginLeft: 4, opacity: 0.7 }}
                >
                  {actionAuthorityLabel("clear_tool_calls").label}
                </span>
              </button>
            </div>
            <div style={{ display: "flex", gap: "6px", width: "100%" }}>
              <FilterDropdown label="Event Kind" value={filters.eventKind} options={eventKinds} onChange={(v) => setFilters((f) => ({ ...f, eventKind: v }))} />
              <FilterDropdown label="Status" value={filters.status} options={statuses} onChange={(v) => setFilters((f) => ({ ...f, status: v }))} />
              {/* SEC-005 (2026-04-23) Failures-Only toggle */}
              <button
                type="button"
                onClick={() => setFilters((f) => ({ ...f, failuresOnly: !f.failuresOnly }))}
                style={{
                  background: filters.failuresOnly ? "#5a1a1a" : "var(--bg-3)",
                  color: filters.failuresOnly ? "#ff8a80" : "var(--text)",
                  border: filters.failuresOnly ? "1px solid #a33" : "1px solid var(--line)",
                  borderRadius: "var(--radius-sm)",
                  padding: "5px 10px",
                  fontSize: "0.78rem",
                  cursor: "pointer",
                  fontFamily: "inherit",
                  whiteSpace: "nowrap",
                }}
                title="Show only enforcement failures / degraded / block events"
              >
                {filters.failuresOnly ? "● Failures Only" : "Failures Only"}
              </button>
            </div>
          </div>
        <div className="table-head execution-table-wide-row" aria-hidden="true" style={{ padding: "4px 0", width: "100%", flex: "0 0 auto" }}>
          <span>Event</span>
          <span>Action</span>
          <span>Status</span>
          <span>Observed</span>
        </div>
        <div className="page-scroll-region">
          <div className="flat-table">
            {filtered.map((event) => {
              const lane = eventLane(event);
              const payload = (event.payload ?? {}) as Record<string, unknown>;
              const audit = routingAudit(payload);
              const indent = lane ? " execution-event-child" : "";
              const status = eventStatus(event);
              const isBlocked = status === "blocked" || event.event_kind?.includes("block");
              const isFailed = status === "failed" || event.event_kind === "tool_call_failed";
              const isGuard = event.event_kind?.includes("guard");
              // SEC-005: explicit degraded/mutation-failure events get
              // the same red treatment as blocked rows so operators
              // spot enforcement degradation in the stream without
              // flipping the Failures-Only filter.
              const isDegradedEnforcement =
                event.event_kind === "prompt_mutation_failed"
                || event.event_kind === "prompt_mutation_rolled_back"
                || event.event_kind === "enforcement_disabled_bypass";
              const rowColor = (isBlocked || isDegradedEnforcement)
                ? "#f87171"
                : isFailed
                  ? "#fbbf24"
                  : isGuard
                    ? "#c084fc"
                    : undefined;
              return (
                <button
                  key={event.event_id}
                  type="button"
                  className={`feed-row feed-row-wide execution-table-wide-row execution-event-row${indent}`}
                  onClick={() => setSelectedEvent(event)}
                  style={rowColor ? { borderLeft: `3px solid ${rowColor}`, paddingLeft: "8px" } : undefined}
                >
                  <strong>{eventLabel(event)}</strong>
                  <span>{event.action_kind ?? event.event_kind}</span>
                  <span style={rowColor ? { color: rowColor, fontWeight: 600 } : undefined}>{status}</span>
                  <time>{formatTimestamp(event.observed_at)}</time>
                  {lane ? <small className="execution-event-lane">lane: {lane}</small> : null}
                  <AuditBadges logicalMode={audit.logicalMode} nativeMode={audit.nativeMode} fallbackUsed={audit.fallbackUsed} />
                  {(event.event_kind === "control_plane_mutation" ||
                    event.event_kind === "config_set") && (() => {
                    const a = eventAuthority(event as unknown as AuthorityEvent);
                    return (
                      <small
                        className="execution-event-tokens"
                        title={`actor: ${a.actor} · scope: ${a.scope}`}
                      >
                        {a.actor} · {a.scope}
                        {a.blastRadius ? ` · ${a.blastRadius.label}` : ""}
                        {a.reason ? ` · ${a.reason}` : ""}
                      </small>
                    );
                  })()}
                  {payload.tokens_in_estimate || payload.tokens_out_estimate ? (
                    <small className="execution-event-tokens">~{Number(payload.tokens_in_estimate ?? 0) + Number(payload.tokens_out_estimate ?? 0)} tokens</small>
                  ) : null}
                </button>
              );
            })}
          </div>
        </div>
      {selectedEvent ? (() => {
        const payload = (selectedEvent.payload ?? {}) as Record<string, unknown>;
        const audit = routingAudit(payload);
        const oldText = typeof payload.old_str === "string" ? payload.old_str : typeof payload.old_content === "string" ? payload.old_content : null;
        const newText = typeof payload.new_str === "string" ? payload.new_str : typeof payload.new_content === "string" ? payload.new_content : null;
        return (
          <div className="modal-backdrop" onClick={() => setSelectedEvent(null)}>
            <div className="modal-panel tool-detail-modal" onClick={(e) => e.stopPropagation()}>
              <div className="page-header modal-header">
                <div>
                  <div className="section-label">Execution Event</div>
                  <h3>{eventLabel(selectedEvent)}</h3>
                </div>
                <button className="action-button action-button-small modal-close" type="button" onClick={() => setSelectedEvent(null)}>Close</button>
              </div>
              <div className="tool-detail-body">
                <div className="tool-detail-row"><span>Event</span><strong>{selectedEvent.event_kind}</strong></div>
                <div className="tool-detail-row"><span>Action</span><strong>{selectedEvent.action_kind ?? "n/a"}</strong></div>
                <div className="tool-detail-row"><span>Status</span><strong>{eventStatus(selectedEvent)}</strong></div>
                <div className="tool-detail-row"><span>Session</span><strong>{selectedEvent.session_id ?? "n/a"}</strong></div>
                <div className="tool-detail-row"><span>Observed</span><strong>{formatTimestamp(selectedEvent.observed_at)}</strong></div>
                {eventLane(selectedEvent) ? <div className="tool-detail-row"><span>Lane</span><strong>{eventLane(selectedEvent)}</strong></div> : null}
                {audit.visible ? <div className="tool-detail-row"><span>Routing</span><strong>{[audit.logicalMode ? `logical ${audit.logicalMode}` : "", audit.nativeMode ? `native ${audit.nativeMode}` : "", audit.fallbackUsed ? "fallback" : ""].filter(Boolean).join(" · ")}</strong></div> : null}
                {payload.args_preview ? <div className="tool-detail-code"><div className="section-label">Arguments</div><pre>{detailValue(payload.args_preview)}</pre></div> : null}
                {oldText && newText ? (
                  <div className="tool-detail-diff">
                    <div className="section-label">Diff</div>
                    <DiffViewer original={oldText} modified={newText} />
                  </div>
                ) : payload.result_preview || payload.result_summary ? (
                  <div className="tool-detail-code"><div className="section-label">Result</div><pre>{detailValue(payload.result_preview ?? payload.result_summary)}</pre></div>
                ) : payload && Object.keys(payload).length > 0 ? (
                  <div className="tool-detail-code"><div className="section-label">Payload</div><pre>{JSON.stringify(payload, null, 2)}</pre></div>
                ) : null}
              </div>
            </div>
          </div>
        );
      })() : null}
    </section>
  );
}

