import { useCallback, useEffect, useRef, useState } from "react";
import { conductorStart, conductorSend, conductorStop } from "./dashboardApi";
import { DiffViewer } from "./DiffViewer";
import type { ConductorPageProps } from "./dashboardTypes";
import { formatTimestamp } from "./dashboardUtils";

function MiniDropdown({ value, options, onChange }: { value: string; options: Array<{ value: string; label: string }>; onChange: (v: string) => void }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const handler = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);
  const selected = options.find((o) => o.value === value);
  return (
    <div ref={ref} style={{ position: "relative" }}>
      <button type="button" onClick={() => setOpen(!open)} className="conductor-header-btn" style={{ minWidth: "80px", textAlign: "left", position: "relative", paddingRight: "20px" }}>
        {selected?.label ?? value} <span style={{ position: "absolute", right: "6px", fontSize: "0.65rem", color: "var(--text-faint)" }}>▾</span>
      </button>
      {open && (
        <div style={{ position: "absolute", top: "100%", left: 0, zIndex: 50, background: "var(--bg-3)", border: "1px solid var(--line)", borderRadius: "var(--radius-sm)", marginTop: "2px", boxShadow: "0 8px 24px rgba(0,0,0,0.4)", minWidth: "100%" }}>
          {options.map((opt) => (
            <button key={opt.value} type="button" onClick={() => { onChange(opt.value); setOpen(false); }} style={{ display: "block", width: "100%", padding: "5px 8px", border: 0, background: opt.value === value ? "rgba(140, 224, 175, 0.12)" : "transparent", color: opt.value === value ? "var(--accent-bright)" : "var(--text)", cursor: "pointer", textAlign: "left", fontSize: "0.8rem", fontFamily: "inherit" }}>
              {opt.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function laneStatus(laneId: string, runnableLaneIds: string[], blockedReasons: Record<string, string[]>) {
  if (runnableLaneIds.includes(laneId)) return { label: "running", className: "lane-status-ready" };
  if (blockedReasons[laneId]?.length) return { label: "blocked", className: "lane-status-blocked" };
  return { label: "pending", className: "lane-status-pending" };
}

function ConductorChat() {
  const [mode, setMode] = useState<"normal" | "parallel">("normal");
  const [backend, setBackend] = useState<"claude" | "codex">("claude");
  const [running, setRunning] = useState(false);
  const [messages, setMessages] = useState<Array<{ role: string; text: string }>>([]);
  const [input, setInput] = useState("");
  const [starting, setStarting] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const scrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, []);

  async function handleStart() {
    setStarting(true);
    try {
      await conductorStart(undefined, backend);
      setRunning(true);
      setMessages([{ role: "system", text: `Conductor started — ${mode} mode, ${backend} backend` }]);
    } catch (err) {
      setMessages((prev) => [...prev, { role: "error", text: String(err) }]);
    }
    setStarting(false);
  }

  async function handleSend() {
    if (!input.trim()) return;
    const msg = input.trim();
    setInput("");
    setMessages((prev) => [...prev, { role: "user", text: msg }]);
    try {
      await conductorSend(msg);
    } catch (err) {
      setMessages((prev) => [...prev, { role: "error", text: String(err) }]);
    }
    setTimeout(scrollToBottom, 50);
  }

  async function handleStop() {
    try {
      await conductorStop();
      setRunning(false);
      setMessages((prev) => [...prev, { role: "system", text: "Conductor stopped" }]);
    } catch (err) {
      setMessages((prev) => [...prev, { role: "error", text: String(err) }]);
    }
  }

  return (
    <div className="conductor-chat-panel">
      <div className="conductor-chat-header">
        <div className="section-label" style={{ marginRight: "auto" }}>Conductor</div>
        {running ? (
          <button type="button" className="conductor-header-btn" onClick={handleStop}>Stop</button>
        ) : (
          <>
            <div className="conductor-mode-toggle">
              <button type="button" className={mode === "normal" ? "active" : ""} onClick={() => setMode("normal")}>Normal</button>
              <button type="button" className={mode === "parallel" ? "active" : ""} onClick={() => setMode("parallel")}>Parallel</button>
            </div>
            <MiniDropdown value={backend} options={[{ value: "claude", label: "Claude" }, { value: "codex", label: "Codex" }]} onChange={(v) => setBackend(v as "claude" | "codex")} />
            <button type="button" className="conductor-header-btn" disabled={starting} onClick={handleStart}>
              {starting ? "..." : "Start"}
            </button>
          </>
        )}
      </div>
      <div className="conductor-chat-messages">
        {messages.length === 0 && !running && (
          <div className="conductor-empty" style={{ padding: "20px", textAlign: "center", color: "var(--text-faint)" }}>
            <p style={{ margin: "4px 0" }}><strong style={{ color: "var(--text)" }}>Normal</strong> — one agent, serial lane execution</p>
            <p style={{ margin: "4px 0" }}><strong style={{ color: "var(--text)" }}>Parallel</strong> — multi-agent, concurrent lanes with isolation</p>
          </div>
        )}
        {messages.map((msg, i) => (
          <div key={i} className={`conductor-msg conductor-msg-${msg.role}`}>
            <strong>{msg.role}</strong>
            <span>{msg.text}</span>
          </div>
        ))}
        <div ref={messagesEndRef} />
      </div>
      <div style={{ display: "flex", gap: "6px", alignItems: "flex-end", padding: "4px 0" }}>
        <textarea
          placeholder={running ? "Send command to conductor..." : "Start the conductor first"}
          disabled={!running}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleSend(); } }}
          rows={1}
          style={{ flex: 1, resize: "vertical", minHeight: "34px", maxHeight: "120px", background: "var(--bg-3)", color: "var(--text)", border: "1px solid var(--line)", borderRadius: "var(--radius-sm)", padding: "6px 10px", fontFamily: "inherit", fontSize: "0.82rem" }}
          onDrop={(e) => {
            const files = e.dataTransfer?.files;
            if (files?.length) {
              e.preventDefault();
              const names = Array.from(files).map((f) => f.name).join(", ");
              setInput((prev) => prev + (prev ? "\n" : "") + `[Attached: ${names}]`);
            }
          }}
        />
        <button type="button" className="conductor-header-btn" style={{ minHeight: "34px" }} disabled={!running || !input.trim()} onClick={handleSend}>
          Send
        </button>
      </div>
    </div>
  );
}

function ToolCallsPanel({ recentExecution }: { recentExecution: ConductorPageProps["recentExecution"] }) {
  const [selectedEvent, setSelectedEvent] = useState<ConductorPageProps["recentExecution"][number] | null>(null);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === "Escape") setSelectedEvent(null); };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, []);

  return (
    <>
      <div className="conductor-tools-panel">
        <div className="section-label">Recent Tool Calls</div>
        <div className="mock-tool-feed">
          {recentExecution.slice(0, 8).map((event) => {
            const payload = (event.payload ?? {}) as Record<string, unknown>;
            const tokenEstimate = Number(payload.tokens_in_estimate ?? 0) + Number(payload.tokens_out_estimate ?? 0);
            return (
              <button key={event.event_id} type="button" className="mock-tool-row" onClick={() => setSelectedEvent(event)}>
                <div className="mock-tool-name">{event.capability_name ?? event.event_kind}</div>
                <div className="mock-tool-action">{event.action_kind ?? event.event_kind}</div>
                <div className="mock-tool-meta">{tokenEstimate > 0 ? `~${tokenEstimate} tok` : event.status ?? "unknown"}</div>
              </button>
            );
          })}
        </div>
      </div>
      {selectedEvent ? (() => {
        const payload = (selectedEvent.payload ?? {}) as Record<string, unknown>;
        const oldText = typeof payload.old_str === "string" ? payload.old_str : typeof payload.old_content === "string" ? payload.old_content : null;
        const newText = typeof payload.new_str === "string" ? payload.new_str : typeof payload.new_content === "string" ? payload.new_content : null;
        return (
          <div className="modal-backdrop" onClick={() => setSelectedEvent(null)}>
            <div className="modal-panel tool-detail-modal" onClick={(e) => e.stopPropagation()}>
              <div className="page-header modal-header">
                <div>
                  <div className="section-label">Conductor Tool Call</div>
                  <h3>{selectedEvent.capability_name ?? selectedEvent.event_kind}</h3>
                </div>
                <button className="action-button action-button-small modal-close" type="button" onClick={() => setSelectedEvent(null)}>Close</button>
              </div>
              <div className="tool-detail-body">
                <div className="tool-detail-row"><span>Action</span><strong>{selectedEvent.action_kind ?? selectedEvent.event_kind}</strong></div>
                <div className="tool-detail-row"><span>Status</span><strong>{selectedEvent.status ?? "unknown"}</strong></div>
                <div className="tool-detail-row"><span>Observed</span><strong>{formatTimestamp(selectedEvent.observed_at)}</strong></div>
                {payload.args_preview ? <div className="tool-detail-code"><div className="section-label">Arguments</div><pre>{typeof payload.args_preview === "object" ? JSON.stringify(payload.args_preview, null, 2) : String(payload.args_preview)}</pre></div> : null}
                {oldText && newText ? <div className="tool-detail-diff"><div className="section-label">Diff</div><DiffViewer original={oldText} modified={newText} /></div> : null}
                {!oldText && !newText && payload && Object.keys(payload).length > 0 ? <div className="tool-detail-code"><div className="section-label">Payload</div><pre>{JSON.stringify(payload, null, 2)}</pre></div> : null}
              </div>
            </div>
          </div>
        );
      })() : null}
    </>
  );
}

export function ConductorPage({ selectedSession, progressPercent, conductorLanes, runnableLaneIds, blockedReasons, recentExecution }: ConductorPageProps) {
  const hasLanes = conductorLanes.length > 0;
  return (
    <section className="page page-conductor">
      <div className="conductor-layout-3col">
        <div className="conductor-lanes-panel">
          <div className="section-label">Lanes {hasLanes ? `(${conductorLanes.length})` : ""}</div>
          {hasLanes ? conductorLanes.map((lane, i) => {
            const status = laneStatus(lane.lane_id, runnableLaneIds, blockedReasons);
            const reasons = blockedReasons[lane.lane_id] ?? [];
            return (
              <div key={lane.lane_id} className={`conductor-lane ${status.className}`}>
                <div className="conductor-lane-header">
                  <span className={`conductor-lane-dot ${status.className}`} />
                  <strong>{i + 1}. {lane.name || lane.lane_id}</strong>
                  <span className="conductor-lane-badge">{status.label}</span>
                </div>
                {lane.depends_on?.length ? <small className="conductor-lane-deps">depends: {lane.depends_on.join(", ")}</small> : null}
                {reasons.length ? <small className="conductor-lane-blocked">{reasons.join(" · ")}</small> : null}
              </div>
            );
          }) : <div className="conductor-empty-mini">No lanes yet</div>}
        </div>
        <ToolCallsPanel recentExecution={recentExecution} />
        <ConductorChat />
      </div>
      {hasLanes && (
        <div className="conductor-summary">
          <div className="summary-row"><span>Progress</span><strong>{progressPercent}%</strong></div>
          <div className="summary-row"><span>Total lanes</span><strong>{conductorLanes.length}</strong></div>
          <div className="summary-row"><span>Runnable</span><strong>{runnableLaneIds.length}</strong></div>
          <div className="summary-row"><span>Blocked</span><strong>{Object.keys(blockedReasons).length}</strong></div>
        </div>
      )}
    </section>
  );
}
