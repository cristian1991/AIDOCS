import React, { useEffect, useMemo, useState } from "react";
import { opencodeModels } from "./dashboardApi";
import type { ConductorAgentsPageProps } from "./dashboardTypes";
import {
  CLAUDE_MODEL_OPTIONS,
  CODEX_MODEL_OPTIONS,
  HOST_OPTIONS,
  TASK_TYPES,
  THINK_MODE_OPTIONS,
  type DropdownOption,
} from "./dashboardCatalog";

type RouteConfig = { host: string; model: string; think_mode: "off" | "low" | "medium" | "high" };
type ConductorConfigEntry = NonNullable<ConductorAgentsPageProps["configEntries"]>[number];

function getModelOptionsForBackend(
  backend: string,
  opencodeModelOptions: Array<DropdownOption>,
): Array<DropdownOption> {
  if (backend === "claude") return CLAUDE_MODEL_OPTIONS;
  if (backend === "codex") return CODEX_MODEL_OPTIONS;
  if (backend === "opencode") return opencodeModelOptions;
  return [{ value: "", label: "default" }];
}

function scopeValue(entry: ConductorConfigEntry | undefined, settingsScope: ConductorAgentsPageProps["settingsScope"]): string {
  if (!entry) return "";
  const own = entry.scope_values?.[settingsScope];
  if (own !== undefined && own !== null) return String(own);
  if (settingsScope === "session") {
    const projectValue = entry.scope_values?.project;
    if (projectValue !== undefined && projectValue !== null) return String(projectValue);
  }
  if (settingsScope !== "global") {
    const globalValue = entry.scope_values?.global;
    if (globalValue !== undefined && globalValue !== null) return String(globalValue);
  }
  if (entry.current_value !== undefined && entry.current_value !== null) return String(entry.current_value);
  return String(entry.default ?? "");
}

function hasOwnValue(entry: ConductorConfigEntry | undefined, settingsScope: ConductorAgentsPageProps["settingsScope"]): boolean {
  if (!entry) return false;
  const own = entry.scope_values?.[settingsScope];
  return own !== undefined && own !== null;
}

function inheritedFrom(entry: ConductorConfigEntry | undefined, settingsScope: ConductorAgentsPageProps["settingsScope"]): string | null {
  if (!entry || hasOwnValue(entry, settingsScope)) return null;
  if (settingsScope === "session") {
    if (entry.scope_values?.project !== undefined && entry.scope_values.project !== null) return "project";
    if (entry.scope_values?.global !== undefined && entry.scope_values.global !== null) return "global";
    return "default";
  }
  if (settingsScope === "project") {
    if (entry.scope_values?.global !== undefined && entry.scope_values.global !== null) return "global";
    return "default";
  }
  return null;
}

function MiniDropdown({ value, options, onChange, disabled = false }: { value: string; options: Array<DropdownOption>; onChange: (v: string) => void; disabled?: boolean }) {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);
  return (
    <div style={{ position: "relative" }}>
      <button type="button" disabled={disabled} onClick={() => setOpen((current) => !current)} className="routing-select" style={{ width: "100%", textAlign: "left", position: "relative", paddingRight: "20px" }}>
        {(options.find((option) => option.value === value)?.label ?? "default")} <span style={{ position: "absolute", right: "6px", fontSize: "0.65rem", color: "var(--text-faint)" }}>▾</span>
      </button>
      {open && !disabled ? (
        <div style={{ position: "absolute", top: "100%", left: 0, zIndex: 50, background: "var(--bg-3)", border: "1px solid var(--line)", borderRadius: "var(--radius-sm)", marginTop: "2px", boxShadow: "0 8px 24px rgba(0,0,0,0.4)", minWidth: "100%" }}>
          {options.map((opt) => (
            <button key={opt.value} type="button" onClick={() => { onChange(opt.value); setOpen(false); }} style={{ display: "block", width: "100%", padding: "5px 8px", border: 0, background: opt.value === value ? "rgba(140, 224, 175, 0.12)" : "transparent", color: opt.value === value ? "var(--accent-bright)" : "var(--text)", cursor: "pointer", textAlign: "left", fontSize: "0.8rem", fontFamily: "inherit" }}>
              {opt.label}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

export function ConductorAgentsPage({ configEntries, settingsScope, setSettingsScope, hasProject, hasSession, requestConfigSave, savingSetting }: ConductorAgentsPageProps) {
  const [opencodeModelOptions, setOpencodeModelOptions] = useState<Array<DropdownOption>>([{ value: "", label: "default" }]);

  useEffect(() => {
    let cancelled = false;
    opencodeModels()
      .then((result) => {
        if (cancelled) return;
        setOpencodeModelOptions([{ value: "", label: "default" }, ...result.models.map((entry) => ({ value: entry, label: entry }))]);
      })
      .catch(() => {
        if (cancelled) return;
        setOpencodeModelOptions([{ value: "", label: "default" }]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const routingEntry = configEntries?.find((entry) => entry.path === "conductor.task_routing");
  const routingRaw = scopeValue(routingEntry, settingsScope) || "{}";
  const routing = useMemo(() => {
    try {
      const raw = JSON.parse(routingRaw);
      if (!raw || typeof raw !== "object") return {} as Record<string, RouteConfig>;
      const loaded: Record<string, RouteConfig> = {};
      Object.entries(raw as Record<string, unknown>).forEach(([task, spec]) => {
        if (!spec || typeof spec !== "object") return;
        const route = spec as Record<string, unknown>;
        loaded[task] = {
          host: String(route.host ?? ""),
          model: String(route.model ?? ""),
          think_mode: String(route.think_mode ?? "medium") as RouteConfig["think_mode"],
        };
      });
      return loaded;
    } catch {
      return {} as Record<string, RouteConfig>;
    }
  }, [routingRaw]);

  function persist(next: Record<string, RouteConfig>) {
    if (!routingEntry) return;
    const shaped: Record<string, RouteConfig> = {};
    Object.entries(next).forEach(([task, route]) => {
      if (!route.host) return;
      shaped[task] = { host: route.host, model: route.model, think_mode: route.think_mode };
    });
    requestConfigSave(routingEntry, settingsScope, JSON.stringify(shaped));
  }

  function setHost(taskType: string, host: string) {
    const next = { ...routing };
    if (!host) {
      delete next[taskType];
    } else {
      next[taskType] = { ...(next[taskType] || { host: "", model: "", think_mode: "medium" }), host, model: "" };
    }
    persist(next);
  }

  function setThinkMode(taskType: string, think_mode: string) {
    const next = { ...routing };
    if (!next[taskType]) return;
    next[taskType] = { ...next[taskType], think_mode: think_mode as RouteConfig["think_mode"] };
    persist(next);
  }

  function setModel(taskType: string, model: string) {
    const next = { ...routing };
    if (!next[taskType]) return;
    next[taskType] = { ...next[taskType], model };
    persist(next);
  }

  return (
    <section className="page page-config conductor-agents-page">
      <div className="page-fixed-header config-header-row">
        <div className="config-tabs">
          <button type="button" className={settingsScope === "global" ? "config-tab is-active" : "config-tab"} onClick={() => setSettingsScope("global")}>Global</button>
          <button type="button" className={settingsScope === "project" ? "config-tab is-active" : "config-tab"} disabled={!hasProject} onClick={() => setSettingsScope("project")}>Project</button>
          <button type="button" className={settingsScope === "session" ? "config-tab is-active" : "config-tab"} disabled={!hasSession} onClick={() => setSettingsScope("session")}>Session</button>
        </div>
          <div className="config-header-actions">
            {settingsScope !== "global" ? (
              <button type="button" className="action-button action-button-small" disabled={!hasOwnValue(routingEntry, settingsScope) || savingSetting === "conductor.task_routing"} onClick={() => routingEntry && requestConfigSave(routingEntry, settingsScope, "__inherit__")}>
                Inherit
              </button>
          ) : null}
        </div>
      </div>
      <div className="page-scroll-region">
        <div className="settings-flat-list">
          <div className="setting-row">
            <div className="setting-copy">
              <div className="setting-title-row">
                <strong>Task routing</strong>
                {inheritedFrom(routingEntry, settingsScope) ? <span className="setting-inherited">from {inheritedFrom(routingEntry, settingsScope)}</span> : null}
                {hasOwnValue(routingEntry, settingsScope) && settingsScope !== "global" ? <span className="setting-own">override</span> : null}
              </div>
              <p>Choose which backend/model should handle each task kind for the selected scope.</p>
            </div>
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
                  <MiniDropdown value={route.host} options={HOST_OPTIONS} onChange={(value) => setHost(taskType, value)} disabled={savingSetting === "conductor.task_routing"} />
                  <MiniDropdown value={modelOptions.some((option) => option.value === route.model) ? route.model : ""} options={modelOptions} onChange={(value) => setModel(taskType, value)} disabled={!route.host || savingSetting === "conductor.task_routing"} />
                  <MiniDropdown value={route.think_mode} options={THINK_MODE_OPTIONS} onChange={(value) => setThinkMode(taskType, value)} disabled={!route.host || savingSetting === "conductor.task_routing"} />
                </React.Fragment>
              );
            })}
          </div>
        </div>
      </div>
    </section>
  );
}

