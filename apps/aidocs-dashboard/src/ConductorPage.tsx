import React, { useCallback, useEffect, useRef, useState } from "react";
import { conductorStart, conductorSend, conductorStop, conductorOutput, saveConfigSetting } from "./dashboardApi";
import type { ConductorOutputLine } from "./dashboardApi";
import { DiffViewer } from "./DiffViewer";
import type { ConductorPageProps } from "./dashboardTypes";
import { formatTimestamp } from "./dashboardUtils";

const TASK_TYPES = ["implement", "refactor", "design", "test", "docs", "research", "debug", "review", "deploy"];
const HOST_OPTIONS = [
  { value: "", label: "default" },
  { value: "claude", label: "Claude Code" },
  { value: "codex", label: "Codex" },
  { value: "opencode", label: "OpenCode" },
];
const MODEL_OPTIONS = [
  { value: "", label: "default" },
  { group: "Anthropic", options: [
    { value: "opus", label: "Opus (latest)" },
    { value: "sonnet", label: "Sonnet (latest)" },
    { value: "haiku", label: "Haiku (latest)" },
    { value: "claude-opus-4-6", label: "Opus 4.6" },
    { value: "claude-sonnet-4-6", label: "Sonnet 4.6" },
    { value: "claude-haiku-4-5", label: "Haiku 4.5" },
  ]},
  { group: "OpenAI", options: [
    { value: "o3", label: "o3" },
    { value: "o4-mini", label: "o4-mini" },
    { value: "gpt-4.1", label: "GPT-4.1" },
  ]},
  { group: "Google", options: [
    { value: "gemini-2.5-pro", label: "Gemini 2.5 Pro" },
    { value: "gemini-2.5-flash", label: "Gemini 2.5 Flash" },
  ]},
  { group: "xAI", options: [
    { value: "grok-3", label: "Grok 3" },
  ]},
];

type RouteConfig = { host: string; model: string };

function TaskRoutingPanel({ routing, onChange }: { routing: Record<string, RouteConfig>; onChange: (r: Record<string, RouteConfig>) => void }) {
  const [expanded, setExpanded] = useState(false);

  function setHost(taskType: string, host: string) {
    const next = { ...routing };
    if (!host) { delete next[taskType]; } else { next[taskType] = { ...(next[taskType] || { host: "", model: "" }), host }; }
    onChange(next);
  }

  function setModel(taskType: string, model: string) {
    const next = { ...routing };
    if (!next[taskType]) return;
    next[taskType] = { ...next[taskType], model };
    onChange(next);
  }

  const configuredCount = Object.keys(routing).length;

  return (
    <div className="routing-panel">
      <button type="button" className="routing-toggle" onClick={() => setExpanded(!expanded)}>
        <span className="section-label" style={{ margin: 0 }}>Task Routing</span>
        {configuredCount > 0 && <span className="routing-badge">{configuredCount} rules</span>}
        <span className="routing-chevron">{expanded ? "\u25B4" : "\u25BE"}</span>
      </button>
      {expanded && (
        <div className="routing-grid">
          <span className="routing-header">Task</span>
          <span className="routing-header">Host</span>
          <span className="routing-header">Model</span>
          {TASK_TYPES.map((t) => {
            const route = routing[t] || { host: "", model: "" };
            return (
              <React.Fragment key={t}>
                <span className="routing-task">{t}</span>
                <select className="routing-select" value={route.host} onChange={(e) => setHost(t, e.target.value)}>
                  {HOST_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                </select>
                <select className="routing-select" value={route.model} onChange={(e) => setModel(t, e.target.value)} disabled={!route.host}>
                  <option value="">default</option>
                  {MODEL_OPTIONS.map((item, idx) =>
                    "group" in item && item.options ? (
                      <optgroup key={idx} label={item.group}>
                        {item.options.map((o: { value: string; label: string }) => (
                          <option key={o.value} value={o.value}>{o.label}</option>
                        ))}
                      </optgroup>
                    ) : (
                      <option key={item.value} value={item.value}>{item.label}</option>
                    )
                  )}
                </select>
              </React.Fragment>
            );
          })}
        </div>
      )}
    </div>
  );
}

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
  const lastTimestampRef = useRef(0);
  const autoScrollRef = useRef(true);

  const scrollToBottom = useCallback(() => {
    if (autoScrollRef.current) {
      messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, []);

  // Poll conductor output when running
  useEffect(() => {
    if (!running) return;
    const interval = setInterval(async () => {
      try {
        const result = await conductorOutput(lastTimestampRef.current);
        if (result.lines.length > 0) {
          const newMessages = result.lines.map((line: ConductorOutputLine) => ({
            role: line.stream === "stderr" ? "error" : "agent",
            text: line.text,
          }));
          setMessages((prev) => [...prev, ...newMessages]);
          lastTimestampRef.current = result.lines[result.lines.length - 1].timestamp;
          setTimeout(scrollToBottom, 50);
        }
        if (!result.running) {
          setRunning(false);
          setMessages((prev) => [...prev, { role: "system", text: "Conductor process exited." }]);
        }
      } catch {
        // ignore polling errors
      }
    }, 1500);
    return () => clearInterval(interval);
  }, [running, scrollToBottom]);

  async function handleStart() {
    setStarting(true);
    try {
      await conductorStart(undefined, backend);
      setRunning(true);
      lastTimestampRef.current = 0;
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
  const [routing, setRouting] = useState<Record<string, RouteConfig>>({});

  // Routing persists in component state — saved to config on change

  function handleRoutingChange(newRouting: Record<string, RouteConfig>) {
    setRouting(newRouting);
    const flat: Record<string, string> = {};
    Object.entries(newRouting).forEach(([k, v]) => { flat[k] = v.model ? v.host + "/" + v.model : v.host; });
    saveConfigSetting("conductor.task_routing", JSON.stringify(flat), undefined, "session").catch(() => {});
  }

  return (
    <section className="page page-conductor">
      <TaskRoutingPanel routing={routing} onChange={handleRoutingChange} />
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
