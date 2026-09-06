import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { conductorStart, conductorSend, conductorStop, conductorOutput, conductorStatus, saveConfigSetting, opencodeModels, msgSend, msgInbox } from "./dashboardApi";
import type { ConductorOutputLine, MsgRoleApi } from "./dashboardApi";
import { DiffViewer } from "./DiffViewer";
import type { ConductorPageProps } from "./dashboardTypes";
import { formatTimestamp } from "./dashboardUtils";
import {
  CLAUDE_MODEL_OPTIONS,
  CODEX_MODEL_OPTIONS,
  CONDUCTOR_HOST_OPTIONS,
  HOST_OPTIONS,
  TASK_TYPES,
  THINK_MODE_OPTIONS,
  type DropdownOption,
} from "./dashboardCatalog";

type RouteConfig = { host: string; model: string; think_mode: "off" | "low" | "medium" | "high" };

function getModelOptionsForBackend(
  backend: string,
  opencodeModelOptions: Array<DropdownOption>,
): Array<DropdownOption> {
  if (backend === "claude") return CLAUDE_MODEL_OPTIONS;
  if (backend === "codex") return CODEX_MODEL_OPTIONS;
  if (backend === "opencode") return opencodeModelOptions;
  return [{ value: "", label: "default" }];
}

function MiniDropdown({ value, options, onChange, disabled = false, compact = false }: { value: string; options: Array<DropdownOption>; onChange: (v: string) => void; disabled?: boolean; compact?: boolean }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  useEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);

  const selected = options.find((o) => o.value === value);
  return (
    <div ref={ref} style={{ position: "relative" }}>
      <button
        type="button"
        disabled={disabled}
        onClick={() => setOpen((current) => !current)}
        className={compact ? "routing-select" : "conductor-header-btn"}
        style={{ minWidth: compact ? "100%" : "80px", width: compact ? "100%" : undefined, textAlign: "left", position: "relative", paddingRight: "20px" }}
      >
        {selected?.label ?? value ?? "default"}
        <span style={{ position: "absolute", right: "6px", fontSize: "0.65rem", color: "var(--text-faint)" }}>▾</span>
      </button>
      {open && !disabled ? (
        <div style={{ position: "absolute", top: "100%", left: 0, zIndex: 50, background: "var(--bg-3)", border: "1px solid var(--line)", borderRadius: "var(--radius-sm)", marginTop: "2px", boxShadow: "0 8px 24px rgba(0,0,0,0.4)", minWidth: "100%" }}>
          {options.map((opt) => (
            <button
              key={opt.value}
              type="button"
              onClick={() => {
                onChange(opt.value);
                setOpen(false);
              }}
              style={{ display: "block", width: "100%", padding: "5px 8px", border: 0, background: opt.value === value ? "rgba(140, 224, 175, 0.12)" : "transparent", color: opt.value === value ? "var(--accent-bright)" : "var(--text)", cursor: "pointer", textAlign: "left", fontSize: "0.8rem", fontFamily: "inherit" }}
            >
              {opt.label}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function TaskRoutingPanel({ routing, onChange, open, onClose }: { routing: Record<string, RouteConfig>; onChange: (r: Record<string, RouteConfig>) => void; open: boolean; onClose: () => void }) {
  const [opencodeModelOptions, setOpencodeModelOptions] = useState<Array<DropdownOption>>([{ value: "", label: "default" }]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    opencodeModels()
      .then((result) => {
        if (cancelled) return;
        setOpencodeModelOptions([{ value: "", label: "default" }, ...result.models.map((entry) => ({ value: entry, label: entry }))]);
      })
      .catch(() => {
        if (cancelled) return;
        setOpencodeModelOptions([
          { value: "", label: "default" },
          { value: "openai/gpt-5.4", label: "openai/gpt-5.4" },
          { value: "openai/gpt-5.3-codex", label: "openai/gpt-5.3-codex" },
          { value: "opencode/big-pickle", label: "opencode/big-pickle" },
          { value: "opencode/minimax-m2.5-free", label: "opencode/minimax-m2.5-free" },
          { value: "opencode/nemotron-3-super-free", label: "opencode/nemotron-3-super-free" },
        ]);
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  if (!open) return null;

  function setHost(taskType: string, host: string) {
    const next = { ...routing };
    if (!host) {
      delete next[taskType];
    } else {
      next[taskType] = { ...(next[taskType] || { host: "", model: "", think_mode: "medium" }), host, model: "" };
    }
    onChange(next);
  }

  function setModel(taskType: string, model: string) {
    const next = { ...routing };
    if (!next[taskType]) return;
    next[taskType] = { ...next[taskType], model };
    onChange(next);
  }

  function setThinkMode(taskType: string, think_mode: string) {
    const next = { ...routing };
    if (!next[taskType]) return;
    next[taskType] = { ...next[taskType], think_mode: think_mode as RouteConfig["think_mode"] };
    onChange(next);
  }

  return (
    <div className="routing-modal-backdrop" onClick={onClose}>
      <div className="routing-modal" onClick={(e) => e.stopPropagation()}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "12px" }}>
          <span className="section-label" style={{ margin: 0 }}>Task Routing</span>
          <button type="button" className="action-button action-button-compact" onClick={onClose}>Close</button>
        </div>
        <div className="routing-grid">
          <span className="routing-header">Task</span>
          <span className="routing-header">Host</span>
          <span className="routing-header">Model</span>
          <span className="routing-header">Think</span>
          {TASK_TYPES.map((taskType) => {
            const route = routing[taskType] || { host: "", model: "", think_mode: "medium" as const };
            const modelOptions = getModelOptionsForBackend(route.host, opencodeModelOptions);
            return (
              <React.Fragment key={taskType}>
                <span className="routing-task">{taskType}</span>
                <MiniDropdown value={route.host} options={HOST_OPTIONS} onChange={(value) => setHost(taskType, value)} compact />
                <MiniDropdown value={modelOptions.some((option) => option.value === route.model) ? route.model : ""} options={modelOptions} onChange={(value) => setModel(taskType, value)} compact disabled={!route.host} />
                <MiniDropdown value={route.think_mode} options={THINK_MODE_OPTIONS} onChange={(value) => setThinkMode(taskType, value)} compact disabled={!route.host} />
              </React.Fragment>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function laneStatus(laneId: string, runnableLaneIds: string[], blockedReasons: Record<string, string[]>) {
  if (runnableLaneIds.includes(laneId)) return { label: "running", className: "lane-status-ready" };
  if (blockedReasons[laneId]?.length) return { label: "blocked", className: "lane-status-blocked" };
  return { label: "pending", className: "lane-status-pending" };
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
    <div style={{ display: "flex", flexWrap: "wrap", gap: 4, justifyContent: "flex-end" }}>
      {logicalMode ? <small className="execution-event-tokens">L:{logicalMode}</small> : null}
      {nativeMode ? <small className="execution-event-tokens">N:{nativeMode}</small> : null}
      {fallbackUsed ? <small className="execution-event-lane" style={{ color: "#fbbf24" }}>fallback</small> : null}
    </div>
  );
}

// Message-substrate shared types + role chip. Lives here for slice B1
// — should be extracted on a follow-up that has file-create perms.
// Phoenix 2026-05-12: renamed from cerberus_* (Empire directive).
type MsgRole = MsgRoleApi;
type MsgRecipient = "conductor" | "co-conductor" | "both";

const MSG_ROLE_LABEL: Record<MsgRole, string> = {
  // Wire key `king` is the message-API role id (compat); the DISPLAY
  // name follows the Empire ruling 2026-07-18 — the operator's seat is
  // the Emperor's.
  king: "Emperor",
  conductor: "Conductor",
  "co-conductor": "Co-Conductor",
};
const MSG_ROLE_BG: Record<MsgRole, string> = {
  king: "rgba(245, 197, 24, 0.18)",
  conductor: "rgba(140, 224, 175, 0.18)",
  "co-conductor": "rgba(120, 170, 255, 0.18)",
};
const MSG_ROLE_FG: Record<MsgRole, string> = {
  king: "#f5c518",
  conductor: "var(--accent-bright)",
  "co-conductor": "#78aaff",
};

function MsgRoleChip({ role }: { role: MsgRole }) {
  return (
    <span
      style={{
        display: "inline-block",
        padding: "1px 6px",
        marginRight: "6px",
        borderRadius: "var(--radius-sm)",
        background: MSG_ROLE_BG[role],
        color: MSG_ROLE_FG[role],
        fontSize: "0.65rem",
        fontWeight: 600,
        textTransform: "uppercase",
        letterSpacing: "0.04em",
      }}
    >
      {MSG_ROLE_LABEL[role]}
    </span>
  );
}

function recipientToRoles(recipient: MsgRecipient): MsgRole[] {
  if (recipient === "both") return ["conductor", "co-conductor"];
  return [recipient];
}

type ChatMessage = { role: string; text: string; msgRole?: MsgRole };
type ConductorScopeState = {
  messages: ChatMessage[];
  lastTimestamp: number;
  running: boolean;
  backend: "claude" | "codex" | "opencode";
  model: string;
};

const conductorScopes: Map<string, ConductorScopeState> = new Map();

function scopeKey(projectRoot: string | null, sessionId: string | null): string | null {
  if (!projectRoot || !sessionId) return null;
  return `${projectRoot}|${sessionId}`;
}

function getOrCreateScope(key: string, fallbackBackend: "claude" | "codex" | "opencode", fallbackModel: string): ConductorScopeState {
  let entry = conductorScopes.get(key);
  if (!entry) {
    entry = {
      messages: [],
      lastTimestamp: 0,
      running: false,
      backend: fallbackBackend,
      model: fallbackModel,
    };
    conductorScopes.set(key, entry);
  }
  return entry;
}

function ConductorChat({
  defaultBackend,
  defaultModels,
  projectRoot,
  sessionId,
}: {
  defaultBackend: "claude" | "codex" | "opencode";
  defaultModels: Record<string, string>;
  projectRoot: string | null;
  sessionId: string | null;
}) {
  const key = scopeKey(projectRoot, sessionId);
  const initialScope = key ? getOrCreateScope(key, defaultBackend, defaultModels[defaultBackend] ?? "") : null;

  const [backend, setBackend] = useState<"claude" | "codex" | "opencode">(initialScope?.backend ?? defaultBackend);
  const [model, setModel] = useState(initialScope?.model ?? defaultModels[defaultBackend] ?? "");
  const [opencodeModelOptions, setOpencodeModelOptions] = useState<Array<DropdownOption>>([{ value: "", label: "default" }]);
  const [running, setRunning] = useState(initialScope?.running ?? false);
  const [messages, setMessages] = useState<ChatMessage[]>(initialScope?.messages ?? []);
  const [input, setInput] = useState("");
  const [starting, setStarting] = useState(false);
  const [awaitingReply, setAwaitingReply] = useState(false);
  const [recipient, setRecipient] = useState<MsgRecipient>("conductor");
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const lastTimestampRef = useRef(initialScope?.lastTimestamp ?? 0);
  const autoScrollRef = useRef(true);
  const pollErrorShownRef = useRef(false);

  const persistScope = useCallback(
    (patch: Partial<ConductorScopeState>) => {
      if (!key) return;
      const entry = conductorScopes.get(key);
      if (!entry) return;
      Object.assign(entry, patch);
    },
    [key],
  );

  const appendMessages = useCallback(
    (incoming: ChatMessage[]) => {
      if (!incoming.length) return;
      setMessages((prev) => {
        const next = [...prev];
        for (const item of incoming) {
          const last = next[next.length - 1];
          if (last && last.role === item.role && last.text === item.text) continue;
          next.push(item);
        }
        if (key) {
          const entry = conductorScopes.get(key);
          if (entry) entry.messages = next;
        }
        return next;
      });
    },
    [key],
  );

  const scrollToBottom = useCallback(() => {
    if (autoScrollRef.current) messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, []);

  // Re-hydrate when (project, session) changes.
  useEffect(() => {
    if (!key) {
      setMessages([]);
      setRunning(false);
      lastTimestampRef.current = 0;
      return;
    }
    const scope = getOrCreateScope(key, defaultBackend, defaultModels[defaultBackend] ?? "");
    setMessages(scope.messages);
    setRunning(scope.running);
    setBackend(scope.backend);
    setModel(scope.model);
    lastTimestampRef.current = scope.lastTimestamp;
    pollErrorShownRef.current = false;
    setAwaitingReply(false);

    // Sync live status from backend (process may have exited while we were away).
    if (projectRoot && sessionId) {
      conductorStatus(projectRoot, sessionId)
        .then((result) => {
          if (scopeKey(projectRoot, sessionId) !== key) return;
          const liveRunning = !!result.running;
          setRunning(liveRunning);
          persistScope({ running: liveRunning });
          if (liveRunning) {
            if (result.backend) setBackend(result.backend as "claude" | "codex" | "opencode");
            if (typeof result.model === "string") setModel(result.model);
          }
        })
        .catch(() => {});
    }
  }, [key, projectRoot, sessionId, defaultBackend, defaultModels, persistScope]);

  useEffect(() => {
    if (backend !== "opencode") return;
    let cancelled = false;
    opencodeModels()
      .then((result) => {
        if (cancelled) return;
        setOpencodeModelOptions([{ value: "", label: "default" }, ...result.models.map((entry) => ({ value: entry, label: entry }))]);
      })
      .catch(() => {
        if (cancelled) return;
        setOpencodeModelOptions([
          { value: "", label: "default" },
          { value: "openai/gpt-5.4", label: "openai/gpt-5.4" },
          { value: "openai/gpt-5.3-codex", label: "openai/gpt-5.3-codex" },
        ]);
      });
    return () => {
      cancelled = true;
    };
  }, [backend]);

  const backendModelOptions = useMemo(
    () => getModelOptionsForBackend(backend, opencodeModelOptions),
    [backend, opencodeModelOptions],
  );

  // Persist backend/model selection per scope whenever it changes while idle.
  useEffect(() => {
    if (running) return;
    persistScope({ backend, model });
  }, [backend, model, running, persistScope]);

  // Output polling.
  useEffect(() => {
    if (!running || !projectRoot || !sessionId) return;
    const pollKey = key;
    const interval = setInterval(async () => {
      if (!projectRoot || !sessionId || scopeKey(projectRoot, sessionId) !== pollKey) return;
      try {
        const result = await conductorOutput(projectRoot, sessionId, lastTimestampRef.current);
        if (result.lines.length > 0) {
          const newMessages = result.lines.map((line: ConductorOutputLine) => ({
            role: line.stream === "stderr" ? "error" : "agent",
            text: line.text,
          }));
          appendMessages(newMessages);
          setAwaitingReply(false);
          lastTimestampRef.current = result.lines[result.lines.length - 1].timestamp;
          persistScope({ lastTimestamp: lastTimestampRef.current });
          setTimeout(scrollToBottom, 50);
        }
        if (!result.running) {
          setRunning(false);
          setAwaitingReply(false);
          persistScope({ running: false });
          appendMessages([{ role: "system", text: "Conductor process exited." }]);
        }
      } catch (err) {
        if (!pollErrorShownRef.current) {
          pollErrorShownRef.current = true;
          setAwaitingReply(false);
          appendMessages([{ role: "error", text: `Conductor output polling failed: ${String(err)}` }]);
        }
      }
    }, 1500);
    return () => clearInterval(interval);
  }, [running, projectRoot, sessionId, key, appendMessages, scrollToBottom, persistScope]);

  async function handleStart() {
    if (!projectRoot || !sessionId) {
      appendMessages([{ role: "error", text: "Pick a project and session before starting the conductor." }]);
      return;
    }
    setStarting(true);
    try {
      await conductorStart(projectRoot, sessionId, backend, model || undefined);
      setRunning(true);
      lastTimestampRef.current = 0;
      pollErrorShownRef.current = false;
      setAwaitingReply(false);
      const startMsg: ChatMessage = {
        role: "system",
        text: `Conductor started — ${backend}${model ? ` (${model})` : ""} — ${projectRoot} @ ${sessionId}`,
      };
      setMessages([startMsg]);
      persistScope({ messages: [startMsg], running: true, lastTimestamp: 0, backend, model });
    } catch (err) {
      appendMessages([{ role: "error", text: String(err) }]);
    }
    setStarting(false);
  }

  async function handleSend() {
    if (!input.trim() || !projectRoot || !sessionId) return;
    const msg = input.trim();
    setInput("");
    const useMsg = recipient !== "conductor";
    appendMessages([{ role: "user", text: msg, msgRole: "king" }]);
    setAwaitingReply(true);
    try {
      if (useMsg) {
        await msgSend(projectRoot, sessionId, recipientToRoles(recipient), msg);
        // msg substrate is async — replies surface via the Empire inbox poller.
        setAwaitingReply(false);
      } else {
        await conductorSend(projectRoot, sessionId, msg);
      }
    } catch (err) {
      setAwaitingReply(false);
      appendMessages([{ role: "error", text: String(err) }]);
    }
    setTimeout(scrollToBottom, 50);
  }

  // Empire inbox poller — drains messages addressed to `king` every 5s
  // and appends them with the sender's role chip.
  useEffect(() => {
    if (!projectRoot || !sessionId) return;
    const pollKey = key;
    let cancelled = false;
    const tick = async () => {
      if (cancelled || !projectRoot || !sessionId) return;
      if (scopeKey(projectRoot, sessionId) !== pollKey) return;
      try {
        const result = await msgInbox(projectRoot, sessionId, "king");
        if (!result.messages.length) return;
        appendMessages(
          result.messages.map((m) => ({
            role: "agent",
            text: m.body,
            msgRole: m.from_role,
          })),
        );
        setTimeout(scrollToBottom, 50);
      } catch {
        // Polling errors are silent — the conductor pipe handles its own UX.
      }
    };
    const interval = setInterval(tick, 5000);
    void tick();
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [projectRoot, sessionId, key, appendMessages, scrollToBottom]);

  async function handleStop() {
    if (!projectRoot || !sessionId) return;
    try {
      await conductorStop(projectRoot, sessionId);
      setRunning(false);
      setAwaitingReply(false);
      persistScope({ running: false });
      appendMessages([{ role: "system", text: "Conductor stopped" }]);
    } catch (err) {
      appendMessages([{ role: "error", text: String(err) }]);
    }
  }

  const disabled = !projectRoot || !sessionId;

  return (
    <div className="conductor-chat-panel">
      <div className="conductor-chat-header">
        <div className="conductor-chat-header-row">
          <div className="section-label" style={{ marginRight: "auto" }}>Conductor</div>
          {running ? (
            <button type="button" className="conductor-header-btn" onClick={handleStop}>Stop</button>
          ) : (
            <button type="button" className="conductor-header-btn" disabled={starting || disabled} onClick={handleStart}>
              {starting ? "..." : "Start"}
            </button>
          )}
        </div>
        {!running ? (
          <div className="conductor-chat-header-row conductor-chat-header-controls">
            <MiniDropdown value={backend} options={CONDUCTOR_HOST_OPTIONS} onChange={(v) => setBackend(v as "claude" | "codex" | "opencode")} />
            <MiniDropdown value={backendModelOptions.some((option) => option.value === model) ? model : ""} options={backendModelOptions} onChange={setModel} />
          </div>
        ) : null}
      </div>
      <div className="conductor-chat-messages">
        {messages.length === 0 && !running ? (
          <div className="conductor-empty" style={{ padding: "20px", textAlign: "center", color: "var(--text-faint)" }}>
            <p style={{ margin: "4px 0" }}>
              {disabled ? "Select a project and session to start a conductor." : "Choose a backend and start the conductor."}
            </p>
          </div>
        ) : null}
        {messages.map((msg, i) => (
          <div key={i} className={`conductor-msg conductor-msg-${msg.role}`}>
            {msg.msgRole ? <MsgRoleChip role={msg.msgRole} /> : null}
            <strong>
              {msg.role === "agent" && msg.msgRole ? MSG_ROLE_LABEL[msg.msgRole] ?? "agent" : msg.role}
            </strong>
            <span>{msg.text}</span>
          </div>
        ))}
        {awaitingReply ? (
          <div className="conductor-msg conductor-msg-system conductor-msg-pending">
            <strong>agent</strong>
            <span>Thinking...</span>
          </div>
        ) : null}
        <div ref={messagesEndRef} />
      </div>
      <div style={{ display: "flex", gap: "4px", alignItems: "center", padding: "4px 0 0", flexWrap: "wrap" }}>
        <span style={{ fontSize: "0.65rem", color: "var(--text-faint)", textTransform: "uppercase", letterSpacing: "0.04em", marginRight: "4px" }}>To</span>
        {(["conductor", "co-conductor", "both"] as MsgRecipient[]).map((opt) => {
          const active = recipient === opt;
          return (
            <button
              key={opt}
              type="button"
              onClick={() => setRecipient(opt)}
              style={{
                padding: "2px 8px",
                borderRadius: "var(--radius-sm)",
                border: "1px solid var(--line)",
                background: active ? "rgba(140, 224, 175, 0.16)" : "transparent",
                color: active ? "var(--accent-bright)" : "var(--text-faint)",
                fontSize: "0.7rem",
                fontFamily: "inherit",
                cursor: "pointer",
                textTransform: "capitalize",
              }}
            >
              {opt === "co-conductor" ? "Co-Conductor" : opt === "both" ? "Both" : "Conductor"}
            </button>
          );
        })}
      </div>
      <div style={{ display: "flex", gap: "6px", alignItems: "flex-end", padding: "4px 0" }}>
        <textarea
          placeholder={running ? "Send command to conductor..." : disabled ? "Select a session first" : recipient !== "conductor" ? "Send message..." : "Start the conductor first"}
          disabled={disabled || (!running && recipient === "conductor")}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              handleSend();
            }
          }}
          rows={1}
          style={{ flex: 1, resize: "vertical", minHeight: "34px", maxHeight: "120px", background: "var(--bg-3)", color: "var(--text)", border: "1px solid var(--line)", borderRadius: "var(--radius-sm)", padding: "6px 10px", fontFamily: "inherit", fontSize: "0.82rem" }}
        />
        <button type="button" className="conductor-header-btn" style={{ minHeight: "34px" }} disabled={disabled || !input.trim() || (!running && recipient === "conductor")} onClick={handleSend}>
          Send
        </button>
      </div>
    </div>
  );
}


function ToolCallsPanel({ recentExecution, selectedSessionId }: { recentExecution: ConductorPageProps["recentExecution"]; selectedSessionId: string | null }) {
  const [selectedEvent, setSelectedEvent] = useState<ConductorPageProps["recentExecution"][number] | null>(null);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") setSelectedEvent(null);
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, []);

  const emptyLabel = selectedSessionId
    ? "No recorded AIDOCS MCP tool calls in this session."
    : "No recorded AIDOCS MCP tool calls in this project.";

  return (
    <>
      <div className="conductor-tools-panel">
        <div className="section-label">Recent Tool Calls</div>
        <div className="mock-tool-feed">
          {recentExecution.length === 0 ? (
            <div className="conductor-empty-mini">{emptyLabel}</div>
          ) : recentExecution.slice(0, 8).map((event) => {
              const payload = (event.payload ?? {}) as Record<string, unknown>;
              const audit = routingAudit(payload);
              const tokenEstimate = Number(payload.tokens_in_estimate ?? 0) + Number(payload.tokens_out_estimate ?? 0);
              return (
                <button key={event.event_id} type="button" className="mock-tool-row" onClick={() => setSelectedEvent(event)}>
                  <div>
                    <div className="mock-tool-name">{event.capability_name ?? event.event_kind}</div>
                    <div className="mock-tool-action">{event.action_kind ?? event.event_kind}</div>
                  </div>
                  <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 4 }}>
                    <div className="mock-tool-meta">{tokenEstimate > 0 ? `~${tokenEstimate} tok` : event.status ?? "unknown"}</div>
                    <AuditBadges logicalMode={audit.logicalMode} nativeMode={audit.nativeMode} fallbackUsed={audit.fallbackUsed} />
                  </div>
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
                  <div className="section-label">Conductor Tool Call</div>
                  <h3>{selectedEvent.capability_name ?? selectedEvent.event_kind}</h3>
                </div>
                <button className="action-button action-button-small modal-close" type="button" onClick={() => setSelectedEvent(null)}>Close</button>
              </div>
              <div className="tool-detail-body">
                <div className="tool-detail-row"><span>Action</span><strong>{selectedEvent.action_kind ?? selectedEvent.event_kind}</strong></div>
                <div className="tool-detail-row"><span>Status</span><strong>{selectedEvent.status ?? "unknown"}</strong></div>
                <div className="tool-detail-row"><span>Observed</span><strong>{formatTimestamp(selectedEvent.observed_at)}</strong></div>
                {audit.visible ? <div className="tool-detail-row"><span>Routing</span><strong>{[audit.logicalMode ? `logical ${audit.logicalMode}` : "", audit.nativeMode ? `native ${audit.nativeMode}` : "", audit.fallbackUsed ? "fallback" : ""].filter(Boolean).join(" · ")}</strong></div> : null}
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

export function ConductorPage({ progressPercent, conductorLanes, runnableLaneIds, blockedReasons, recentExecution, configEntries, selectedSessionId, projectRoot, sessionId }: ConductorPageProps) {
  const hasLanes = conductorLanes.length > 0;
  const [routing, setRouting] = useState<Record<string, RouteConfig>>({});
  const [routingOpen, setRoutingOpen] = useState(false);
  const [routingError, setRoutingError] = useState<string | null>(null);
  const conductorDefaults = useMemo(() => {
    const entry = (path: string) => configEntries?.find((item) => item.path === path);
    return {
      backend: ((entry("conductor.backend")?.current_value as string | undefined) ?? "claude") as "claude" | "codex" | "opencode",
      models: {
        claude: String(entry("conductor.claude_model")?.current_value ?? ""),
        codex: String(entry("conductor.codex_model")?.current_value ?? ""),
        opencode: String(entry("conductor.opencode_model")?.current_value ?? ""),
      },
    };
  }, [configEntries]);

  useEffect(() => {
    if (!configEntries) return;
    const entry = configEntries.find((e) => e.path === "conductor.task_routing");
    if (!entry?.current_value) {
      setRoutingError(null);
      return;
    }
    try {
      const raw = typeof entry.current_value === "string" ? JSON.parse(entry.current_value) : entry.current_value;
      if (raw && typeof raw === "object") {
        const loaded: Record<string, RouteConfig> = {};
        Object.entries(raw as Record<string, string>).forEach(([k, v]) => {
          const parts = String(v).split("/");
          loaded[k] = { host: parts[0], model: parts.slice(1).join("/"), think_mode: "medium" };
        });
        setRouting(loaded);
        setRoutingError(null);
      }
    } catch (err) {
      setRoutingError(err instanceof Error ? err.message : String(err));
    }
  }, [configEntries]);

  function handleRoutingChange(newRouting: Record<string, RouteConfig>) {
    setRouting(newRouting);
    setRoutingError(null);
    const flat: Record<string, string> = {};
    Object.entries(newRouting).forEach(([k, v]) => {
      flat[k] = v.model ? `${v.host}/${v.model}` : v.host;
    });
    saveConfigSetting("conductor.task_routing", JSON.stringify(flat), undefined, "project").catch((err) => {
      setRoutingError(err instanceof Error ? err.message : String(err));
    });
  }

  return (
    <section className="page page-conductor">
      <TaskRoutingPanel routing={routing} onChange={handleRoutingChange} open={routingOpen} onClose={() => setRoutingOpen(false)} />
      <div className="conductor-layout-3col">
        <div className="conductor-lanes-panel">
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <div className="section-label" style={{ margin: 0 }}>Lanes {hasLanes ? `(${conductorLanes.length})` : ""}</div>
            <button type="button" className="action-button action-button-compact" style={{ fontSize: "0.72rem" }} onClick={() => setRoutingOpen(true)}>Tasks</button>
          </div>
          {routingError ? <div className="conductor-empty-mini" style={{ color: "#f87171" }}>Task routing error: {routingError}</div> : null}
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
          }) : <div className="conductor-empty-mini">Ask the conductor to create a plan and lanes will show up here.</div>}
        </div>
        <ToolCallsPanel recentExecution={recentExecution} selectedSessionId={selectedSessionId} />
        <ConductorChat defaultBackend={conductorDefaults.backend} defaultModels={conductorDefaults.models} projectRoot={projectRoot} sessionId={sessionId} />
      </div>
      {hasLanes ? (
        <div className="conductor-summary">
          <div className="summary-row"><span>Progress</span><strong>{progressPercent}%</strong></div>
          <div className="summary-row"><span>Total lanes</span><strong>{conductorLanes.length}</strong></div>
          <div className="summary-row"><span>Runnable</span><strong>{runnableLaneIds.length}</strong></div>
          <div className="summary-row"><span>Blocked</span><strong>{Object.keys(blockedReasons).length}</strong></div>
        </div>
      ) : null}
    </section>
  );
}

