import { useState } from "react";
import { PieChart as RechartsPie, Pie, Cell, Tooltip, ResponsiveContainer, BarChart, Bar, XAxis, YAxis, CartesianGrid, Legend } from "recharts";
import type {
  DashboardConfigEntry,
  DashboardSeriesItem,
  DashboardSnapshot,
  DashboardTomlDocument,
} from "./dashboardApi";
import {
  asText,
  buildSettingTooltip,
  formatTimestamp,
  isDashboardEditable,
  isDocumentActive,
  type SettingsScope,
  type TomlCategory,
} from "./dashboardUtils";


type SelectedSession = DashboardSnapshot["selected_session"];
type ConductorLane = { lane_id: string; name: string; depends_on?: string[] };

const CHART_COLORS = ["#338441", "#8ce0af", "#4a90d9", "#e8a838", "#d94a6b", "#7c5cbf", "#5cb8a8", "#d97a4a"];

type OverviewPageProps = {
  snapshot: DashboardSnapshot;
  selectedSession: SelectedSession;
  sessionBreakdown: DashboardSnapshot["token_usage"]["session_breakdown"];
};

export function OverviewPage({ snapshot, selectedSession, sessionBreakdown }: OverviewPageProps) {
  const [tokenView, setTokenView] = useState<"session" | "inout">("session");
  const sessionData = sessionBreakdown
    .filter((s) => s.total > 0)
    .map((s) => ({ name: s.session_id.replace(/^\d{4}-\d{2}-\d{2}-/, ""), value: s.total }));
  const tokenEstimates = snapshot.token_usage.token_estimates ?? { tokens_in: 0, tokens_out: 0, total: 0 };
  const inOutData = [
    { name: "Tokens in", value: tokenEstimates.tokens_in },
    { name: "Tokens out", value: tokenEstimates.tokens_out },
  ];
  const chartData = tokenView === "session" ? sessionData : inOutData;
  const hasData = chartData.some((d) => d.value > 0);

  return (
    <section className="page page-overview">
      <div className="overview-top">
        <section className="flat-panel overview-token-panel">
          <div className="chart-panel-header">
            <div className="section-label">Token Usage</div>
            <div className="chart-mode-toggle">
              <button type="button" className={tokenView === "session" ? "toggle-btn is-active" : "toggle-btn"} onClick={() => setTokenView("session")}>Sessions</button>
              <button type="button" className={tokenView === "inout" ? "toggle-btn is-active" : "toggle-btn"} onClick={() => setTokenView("inout")}>In/Out</button>
            </div>
          </div>
          <div className="chart-container-fill">
            {hasData ? (
              <ResponsiveContainer width="100%" height="100%">
                <RechartsPie>
                  <Pie data={chartData} dataKey="value" nameKey="name" cx="50%" cy="50%" innerRadius="35%" outerRadius="70%">
                    {chartData.map((_, i) => <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />)}
                  </Pie>
                  <Tooltip content={<ChartTooltip />} />
                  <Legend iconType="circle" iconSize={8} wrapperStyle={{ fontSize: "0.78rem", color: "#b9d0c2" }} />
                </RechartsPie>
              </ResponsiveContainer>
            ) : (
              <div className="empty-panel">No token data yet. Estimates appear after MCP tool calls.</div>
            )}
          </div>
        </section>

        <div className="overview-info">
          <section className="flat-panel">
            <div className="section-label">Project</div>
            <div className="summary-list">
              <div className="summary-row"><span>Code files</span><strong>{snapshot.project.code_file_count}</strong></div>
              <div className="summary-row"><span>Schema entities</span><strong>{snapshot.project.schema_entity_count}</strong></div>
              <div className="summary-row"><span>Sessions</span><strong>{snapshot.project.session_count}</strong></div>
              <div className="summary-row"><span>Events</span><strong>{snapshot.execution.summary.total_events}</strong></div>
            </div>
          </section>

          <section className="flat-panel">
            <div className="section-label">Session</div>
            <div className="summary-list">
              <div className="summary-row"><span>Status</span><strong>{selectedSession?.overview.status ?? "unknown"}</strong></div>
              <div className="summary-row"><span>Goal</span><strong>{selectedSession?.overview.goal ?? "none"}</strong></div>
              <div className="summary-row"><span>Next step</span><strong>{selectedSession?.plan_overview.next_step ?? "none"}</strong></div>
              <div className="summary-row"><span>Warnings</span><strong>{selectedSession?.compliance.warnings.join(" | ") || "none"}</strong></div>
            </div>
          </section>
        </div>
      </div>

      <section className="flat-panel overview-execution-panel">
        <div className="section-label">Recent Execution</div>
        <div className="flat-table">
          <div className="table-head execution-table-row" aria-hidden="true">
            <span>Capability</span>
            <span>Action</span>
            <span>Observed</span>
          </div>
          {snapshot.execution.recent.slice(0, 6).map((event) => (
            <div key={event.event_id} className="feed-row execution-table-row">
              <strong>{event.capability_name ?? event.event_kind}</strong>
              <span>{event.action_kind ?? event.event_kind}</span>
              <time>{formatTimestamp(event.observed_at)}</time>
            </div>
          ))}
        </div>
      </section>
    </section>
  );
}

type SessionsPageProps = {
  sessions: DashboardSnapshot["sessions"];
  sessionValue: string;
  onSelectSession: (sessionId: string) => void;
};

export function SessionsPage({ sessions, sessionValue, onSelectSession }: SessionsPageProps) {
  return (
    <section className="page">
      <div className="flat-table">
        <div className="table-head session-table-row" aria-hidden="true">
          <span>Session</span>
          <span>Status</span>
          <span>Owner</span>
          <span>Updated</span>
        </div>
        {sessions.map((session) => (
          <button
            key={session.session_id}
            className={session.session_id === sessionValue ? "table-row session-table-row is-selected" : "table-row session-table-row"}
            type="button"
            onClick={() => onSelectSession(session.session_id)}
          >
            <span>{session.title ?? session.session_id}</span>
            <span>{session.status ?? "unknown"}</span>
            <span>{session.owner ?? "unowned"}</span>
            <span>{session.last_updated ?? "n/a"}</span>
          </button>
        ))}
      </div>
    </section>
  );
}

type ConductorPageProps = {
  selectedSession: SelectedSession;
  progressPercent: number;
  conductorLanes: ConductorLane[];
  runnableLaneIds: string[];
  blockedReasons: Record<string, string[]>;
};

export function ConductorPage({
  selectedSession,
  progressPercent,
  conductorLanes,
  runnableLaneIds,
  blockedReasons,
}: ConductorPageProps) {
  return (
    <section className="page">
      <div className="section-label">Conductor</div>
      <div className="progress-shell" aria-hidden="true">
        <div className="progress-bar" style={{ width: `${progressPercent}%` }} />
      </div>
      {selectedSession?.conductor && selectedSession.conductor.graph && selectedSession.conductor.runnable ? (
        <div className="flat-table">
          <div className="table-head conductor-table-row" aria-hidden="true">
            <span>Lane</span>
            <span>State</span>
            <span>Dependencies</span>
            <span>Notes</span>
          </div>
          {conductorLanes.map((lane) => {
            const runnable = runnableLaneIds.includes(lane.lane_id);
            const blocked = blockedReasons[lane.lane_id];
            return (
              <div key={lane.lane_id} className={runnable ? "table-row conductor-table-row is-active" : "table-row conductor-table-row"}>
                <span>{lane.name}</span>
                <span>{runnable ? "Runnable" : "Waiting"}</span>
                <span>{lane.depends_on?.length ?? 0} deps</span>
                <span>{blocked ? blocked.join(", ") : "clear"}</span>
              </div>
            );
          })}
        </div>
      ) : (
        <div className="empty-panel">
          {selectedSession?.conductor_error
            ? selectedSession.conductor_error
            : selectedSession?.plan_overview.has_lanes
              ? "Conductor data is currently unavailable for this session."
              : "This session does not expose conductor lanes."}
        </div>
      )}
    </section>
  );
}

type ExecutionPageProps = {
  recentExecution: DashboardSnapshot["execution"]["recent"];
};

export function ExecutionPage({ recentExecution }: ExecutionPageProps) {
  return (
    <section className="page">
      <div className="flat-table">
        <div className="table-head execution-table-wide-row" aria-hidden="true">
          <span>Capability</span>
          <span>Action</span>
          <span>Status</span>
          <span>Observed</span>
        </div>
        {recentExecution.map((event) => (
          <div key={event.event_id} className="feed-row feed-row-wide execution-table-wide-row">
            <strong>{event.capability_name ?? event.event_kind}</strong>
            <span>{event.action_kind ?? event.event_kind}</span>
            <span>{event.status ?? "unknown"}</span>
            <time>{formatTimestamp(event.observed_at)}</time>
          </div>
        ))}
      </div>
    </section>
  );
}

type UsagePageProps = {
  reason: string;
  tokenEstimates: { tokens_in: number; tokens_out: number; total: number };
  sessionBreakdown: Array<{ session_id: string; tokens_in: number; tokens_out: number; total: number; events: number }>;
  actionRows: Array<DashboardSeriesItem & { width: string }>;
  eventRows: Array<DashboardSeriesItem & { width: string }>;
};


function ChartTooltip({ active, payload }: { active?: boolean; payload?: Array<{ name: string; value: number }> }) {
  if (!active || !payload?.length) return null;
  return (
    <div className="chart-tooltip">
      <strong>{payload[0].name}</strong>
      <span>{payload[0].value.toLocaleString()}</span>
    </div>
  );
}

export function UsagePage({ reason, tokenEstimates, sessionBreakdown, actionRows, eventRows }: UsagePageProps) {
  const [actionMode, setActionMode] = useState<"bar" | "pie">("bar");
  const [eventMode, setEventMode] = useState<"bar" | "pie">("bar");

  const tokenData = [
    { name: "Tokens in", value: tokenEstimates.tokens_in },
    { name: "Tokens out", value: tokenEstimates.tokens_out },
  ];
  const sessionData = sessionBreakdown
    .filter((s) => s.total > 0)
    .map((s) => ({ name: s.session_id.replace(/^\d{4}-\d{2}-\d{2}-/, ""), value: s.total }));
  const actionData = actionRows.map((r) => ({ name: r.label, value: r.count }));
  const eventData = eventRows.map((r) => ({ name: r.label, value: r.count }));

  return (
    <section className="page page-usage">
      <div className="usage-header">
        <div className="usage-token-summary">
          <div className="token-stat">
            <span className="section-label">Tokens In</span>
            <strong>{tokenEstimates.tokens_in.toLocaleString()}</strong>
          </div>
          <div className="token-stat">
            <span className="section-label">Tokens Out</span>
            <strong>{tokenEstimates.tokens_out.toLocaleString()}</strong>
          </div>
          <div className="token-stat">
            <span className="section-label">Total</span>
            <strong>{tokenEstimates.total.toLocaleString()}</strong>
          </div>
        </div>
      </div>
      <div className="usage-grid usage-grid-2col">
        <section className="flat-panel">
          <div className="section-label">In / Out Distribution</div>
          <div className="chart-container">
            <ResponsiveContainer width="100%" height={220}>
              <RechartsPie>
                <Pie data={tokenData} dataKey="value" nameKey="name" cx="50%" cy="50%" innerRadius={40} outerRadius={75}>
                  {tokenData.map((_, i) => <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />)}
                </Pie>
                <Tooltip content={<ChartTooltip />} />
                <Legend iconType="circle" iconSize={8} wrapperStyle={{ fontSize: "0.78rem", color: "#b9d0c2" }} />
              </RechartsPie>
            </ResponsiveContainer>
          </div>
        </section>
        <section className="flat-panel">
          <div className="section-label">By Session</div>
          <div className="chart-container">
            {sessionData.length > 0 ? (
              <ResponsiveContainer width="100%" height={220}>
                <RechartsPie>
                  <Pie data={sessionData} dataKey="value" nameKey="name" cx="50%" cy="50%" innerRadius={40} outerRadius={75}>
                    {sessionData.map((_, i) => <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />)}
                  </Pie>
                  <Tooltip content={<ChartTooltip />} />
                  <Legend iconType="circle" iconSize={8} wrapperStyle={{ fontSize: "0.78rem", color: "#b9d0c2" }} />
                </RechartsPie>
              </ResponsiveContainer>
            ) : (
              <div className="empty-panel">No per-session token data yet</div>
            )}
          </div>
        </section>
      </div>
      <div className="usage-grid usage-grid-2col">
        <section className="flat-panel">
          <div className="chart-panel-header">
            <div className="section-label">Action Kinds</div>
            <div className="chart-mode-toggle">
              <button type="button" className={actionMode === "bar" ? "toggle-btn is-active" : "toggle-btn"} onClick={() => setActionMode("bar")}>Bars</button>
              <button type="button" className={actionMode === "pie" ? "toggle-btn is-active" : "toggle-btn"} onClick={() => setActionMode("pie")}>Pie</button>
            </div>
          </div>
          <div className="chart-container">
            {actionMode === "pie" ? (
              <ResponsiveContainer width="100%" height={260}>
                <RechartsPie>
                  <Pie data={actionData} dataKey="value" nameKey="name" cx="50%" cy="50%" innerRadius={45} outerRadius={85}>
                    {actionData.map((_, i) => <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />)}
                  </Pie>
                  <Tooltip content={<ChartTooltip />} />
                  <Legend iconType="circle" iconSize={8} wrapperStyle={{ fontSize: "0.78rem", color: "#b9d0c2" }} />
                </RechartsPie>
              </ResponsiveContainer>
            ) : (
              <ResponsiveContainer width="100%" height={260}>
                <BarChart data={actionData} layout="vertical" margin={{ left: 0, right: 20 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="rgba(153,211,180,0.08)" />
                  <XAxis type="number" tick={{ fill: "#7c9688", fontSize: 11 }} />
                  <YAxis type="category" dataKey="name" tick={{ fill: "#b9d0c2", fontSize: 12 }} width={110} />
                  <Tooltip content={<ChartTooltip />} />
                  <Bar dataKey="value" radius={[0, 4, 4, 0]}>
                    {actionData.map((_, i) => <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />)}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            )}
          </div>
        </section>
        <section className="flat-panel">
          <div className="chart-panel-header">
            <div className="section-label">Event Kinds</div>
            <div className="chart-mode-toggle">
              <button type="button" className={eventMode === "bar" ? "toggle-btn is-active" : "toggle-btn"} onClick={() => setEventMode("bar")}>Bars</button>
              <button type="button" className={eventMode === "pie" ? "toggle-btn is-active" : "toggle-btn"} onClick={() => setEventMode("pie")}>Pie</button>
            </div>
          </div>
          <div className="chart-container">
            {eventMode === "pie" ? (
              <ResponsiveContainer width="100%" height={260}>
                <RechartsPie>
                  <Pie data={eventData} dataKey="value" nameKey="name" cx="50%" cy="50%" innerRadius={45} outerRadius={85}>
                    {eventData.map((_, i) => <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />)}
                  </Pie>
                  <Tooltip content={<ChartTooltip />} />
                  <Legend iconType="circle" iconSize={8} wrapperStyle={{ fontSize: "0.78rem", color: "#b9d0c2" }} />
                </RechartsPie>
              </ResponsiveContainer>
            ) : (
              <ResponsiveContainer width="100%" height={260}>
                <BarChart data={eventData} layout="vertical" margin={{ left: 0, right: 20 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="rgba(153,211,180,0.08)" />
                  <XAxis type="number" tick={{ fill: "#7c9688", fontSize: 11 }} />
                  <YAxis type="category" dataKey="name" tick={{ fill: "#b9d0c2", fontSize: 12 }} width={130} />
                  <Tooltip content={<ChartTooltip />} />
                  <Bar dataKey="value" radius={[0, 4, 4, 0]}>
                    {eventData.map((_, i) => <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />)}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            )}
          </div>
        </section>
      </div>
      </section>
    );
}

type SettingsPageProps = {
  settingsScope: SettingsScope;
  setSettingsScope: (scope: SettingsScope) => void;
  hasProject: boolean;
  hasSession: boolean;
  configSections: Array<{ section: string; entries: DashboardConfigEntry[] }>;
  draftValues: Record<string, string>;
  savingSetting: string | null;
  requestConfigSave: (entry: DashboardConfigEntry, scope?: string) => void;
  setDraftValue: (path: string, value: string) => void;
  devModeEntry: DashboardConfigEntry | null;
  openImportExport: () => void;
};

function SettingDropdown({
  value,
  options,
  disabled,
  onChange,
}: {
  value: string;
  options: Array<{ value: string; label?: string }>;
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const selected = options.find((o) => o.value === value);
  return (
    <div className={open ? "setting-dropdown is-open" : "setting-dropdown"}>
      <button type="button" className="dropdown-trigger" disabled={disabled} onClick={() => setOpen(!open)}>
        <span className="dropdown-trigger-label">{selected?.label ?? value}</span>
        <span className="dropdown-trigger-icon" aria-hidden="true">{"\u25be"}</span>
      </button>
      {open ? (
        <div className="dropdown-menu" role="listbox">
          {options.map((option) => (
            <button
              key={option.value}
              type="button"
              className={option.value === value ? "dropdown-option is-selected" : "dropdown-option"}
              onClick={() => { onChange(option.value); setOpen(false); }}
            >
              <span>{option.label ?? option.value}</span>
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

const KNOWN_LANGUAGES = [
  "csharp", "css", "dart", "elixir", "go", "html", "java", "javascript", "json",
  "jsx", "kotlin", "less", "lua", "php", "powershell", "prisma", "python", "razor",
  "ruby", "rust", "sass", "scss", "shell", "sql", "svelte", "swift", "toml", "tsx",
  "typescript", "vue", "yaml",
];

function SettingMultiSelect({
  value,
  disabled,
  onChange,
}: {
  value: string;
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const isAll = value.trim().toLowerCase() === "all" || value.trim() === "";
  const selected = isAll ? new Set(KNOWN_LANGUAGES) : new Set(value.split(",").map((s) => s.trim()).filter(Boolean));
  const label = isAll ? "all" : `${selected.size} selected`;

  function toggle(lang: string) {
    if (disabled) return;
    const next = new Set(selected);
    if (isAll) {
      next.delete(lang);
    } else if (next.has(lang)) {
      next.delete(lang);
    } else {
      next.add(lang);
    }
    onChange(next.size === 0 || next.size >= KNOWN_LANGUAGES.length ? "all" : Array.from(next).join(", "));
  }

  function toggleAll() {
    if (disabled) return;
    onChange(isAll ? "" : "all");
  }

  return (
    <div className="setting-dropdown">
      <button type="button" className="dropdown-trigger" disabled={disabled} onClick={() => setOpen(!open)}>
        <span className="dropdown-trigger-label">{label}</span>
        <span className="dropdown-trigger-icon" aria-hidden="true">{"\u25be"}</span>
      </button>
      {open ? (
        <div className="dropdown-menu multiselect-menu" role="listbox">
          <label className="multiselect-option">
            <input type="checkbox" checked={isAll} onChange={toggleAll} />
            <strong>All languages</strong>
          </label>
          {KNOWN_LANGUAGES.map((lang) => (
            <label key={lang} className="multiselect-option">
              <input type="checkbox" checked={selected.has(lang)} onChange={() => toggle(lang)} />
              <span>{lang}</span>
            </label>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function SettingInput({
  entry,
  currentValue,
  savingSetting,
  onChange,
}: {
  entry: DashboardConfigEntry;
  currentValue: string;
  savingSetting: string | null;
  onChange: (value: string) => void;
}) {
  const disabled = !isDashboardEditable(entry) || savingSetting === entry.path;
  if (entry.allowed_values?.length) {
    return (
      <SettingDropdown
        value={currentValue}
        options={entry.allowed_values.map((v) => ({ value: v }))}
        disabled={disabled}
        onChange={onChange}
      />
    );
  }
  if (entry.type === "boolean") {
    return (
      <SettingDropdown
        value={currentValue}
        options={[{ value: "true", label: "true" }, { value: "false", label: "false" }]}
        disabled={disabled}
        onChange={onChange}
      />
    );
    }
    if (entry.path === "languages.enabled" || entry.path === "index.enabled_languages") {
      return <SettingMultiSelect value={currentValue} disabled={disabled} onChange={onChange} />;
    }
  if (entry.type === "string_list") {
    const lines = currentValue.split(",").map((s) => s.trim()).filter(Boolean).join("\n");
    return (
      <textarea
        className="setting-list-textarea"
        value={lines}
        disabled={disabled}
        placeholder="One item per line"
        onChange={(event) => onChange(event.target.value.split("\n").map((s) => s.trim()).filter(Boolean).join(", "))}
      />
    );
  }
  return <input value={currentValue} disabled={disabled} onChange={(event) => onChange(event.target.value)} />;
}

export function SettingsPage({
  settingsScope,
  setSettingsScope,
  hasProject,
  hasSession,
  configSections,
  draftValues,
  savingSetting,
  requestConfigSave,
  setDraftValue,
  devModeEntry,
  openImportExport,
}: SettingsPageProps) {
    const saveScope = settingsScope === "global" ? "user" : settingsScope;

    function scopeValue(entry: DashboardConfigEntry): string {
      const draft = draftValues[entry.path];
      if (draft !== undefined) return draft;
      const raw = entry.scope_values?.[settingsScope === "global" ? "user" : settingsScope];
      if (raw !== undefined && raw !== null) return asText(raw);
      return asText(entry.default);
    }

    const allEntries = configSections.flatMap(({ entries }) => entries);
    const dirtyEntries = allEntries.filter((e) => {
      const draft = draftValues[e.path];
      if (draft === undefined) return false;
      const raw = e.scope_values?.[settingsScope === "global" ? "user" : settingsScope];
      const baseline = raw !== undefined && raw !== null ? asText(raw) : asText(e.default);
      return draft !== baseline && isDashboardEditable(e);
    });
    const devDirty = devModeEntry && draftValues[devModeEntry.path] !== undefined && draftValues[devModeEntry.path] !== asText(devModeEntry.scope_values?.[saveScope] ?? devModeEntry.default);
    const hasDirty = dirtyEntries.length > 0 || devDirty;

  function saveAll() {
    for (const entry of dirtyEntries) {
      requestConfigSave(entry, saveScope);
    }
    if (devDirty && devModeEntry) {
      requestConfigSave(devModeEntry, saveScope);
    }
  }

  return (
    <section className="page page-config">
      <div className="page-fixed-header config-header-row">
        <div className="config-tabs">
          <button type="button" className={settingsScope === "global" ? "config-tab is-active" : "config-tab"} onClick={() => setSettingsScope("global")}>Global</button>
          <button type="button" className={settingsScope === "project" ? "config-tab is-active" : "config-tab"} disabled={!hasProject} onClick={() => setSettingsScope("project")}>Project</button>
          <button type="button" className={settingsScope === "session" ? "config-tab is-active" : "config-tab"} disabled={!hasSession} onClick={() => setSettingsScope("session")}>Session</button>
        </div>
        <button
          type="button"
          className="action-button config-save-button"
          disabled={!hasDirty || !!savingSetting}
          onClick={saveAll}
        >
          {savingSetting ? "Saving..." : "Save"}
        </button>
      </div>
      <div className="page-scroll-region">
        <div className="settings-flat-list">
          {configSections.map(({ section, entries }) => (
            <div key={section}>
              <div className="settings-section-header">{section.replace(/_/g, " ")}</div>
              {entries.map((entry) => {
                const currentValue = scopeValue(entry);
                const valueHelp = buildSettingTooltip(entry, currentValue);
                const hasOptions = !!(entry.allowed_values?.length || entry.type === "boolean");
                return (
                  <div key={entry.path} className="setting-row">
                    <div className="setting-copy">
                      <div className="setting-title-row">
                        <strong>{entry.key}</strong>
                        {hasOptions ? (
                          <span className="setting-info" title={valueHelp} aria-label={valueHelp}>?</span>
                        ) : null}
                      </div>
                      <p>{entry.description}</p>
                    </div>
                    <SettingInput
                      entry={entry}
                      currentValue={currentValue}
                      savingSetting={savingSetting}
                      onChange={(value) => setDraftValue(entry.path, value)}
                    />
                  </div>
                );
              })}
            </div>
          ))}
          {devModeEntry ? (
            <div>
              <div className="settings-section-header">Security</div>
              <div className="setting-row">
                <div className="setting-copy">
                  <strong className="warning-text">DEV_MODE</strong>
                  <p className="warning-text-soft">Allows agents to edit AIDOCS infrastructure source files. Only enable when intentionally modifying protected code.</p>
                </div>
                <SettingDropdown
                  value={scopeValue(devModeEntry)}
                  options={[{ value: "true", label: "true" }, { value: "false", label: "false" }]}
                  disabled={savingSetting === devModeEntry.path}
                  onChange={(value) => setDraftValue(devModeEntry.path, value)}
                />
              </div>
            </div>
          ) : null}
        </div>
        <div className="settings-footer-actions">
          <button type="button" className="action-button config-import-export" onClick={openImportExport}>
            Import / Export config
          </button>
        </div>
      </div>
    </section>
  );
}


type TomlConfigsPageProps = {
  tomlCategory: TomlCategory;
  setTomlCategory: (cat: TomlCategory) => void;
  tomlDocuments: DashboardTomlDocument[];
  selectedTomlPath: string | null;
  setSelectedTomlPath: (path: string) => void;
  selectedTomlDocument: DashboardTomlDocument | null;
  tomlDrafts: Record<string, string>;
  setTomlDraft: (path: string, value: string) => void;
  savingTomlPath: string | null;
  handleTomlSave: () => void;
};

export function TomlConfigsPage({
  tomlCategory,
  setTomlCategory,
  tomlDocuments,
  selectedTomlPath,
  setSelectedTomlPath,
  selectedTomlDocument,
  tomlDrafts,
  setTomlDraft,
  savingTomlPath,
  handleTomlSave,
}: TomlConfigsPageProps) {
  const categoryPrefix = tomlCategory === "action_tokens" ? "action_tokens/" : tomlCategory === "action_hooks" ? "action_hooks/" : "mcp/server/aidocs_mcp/index_languages/";
  const filteredDocuments = tomlDocuments.filter((d) => d.path.startsWith(categoryPrefix));

    return (
      <section className="page page-config">
        <div className="page-fixed-header">
          <div className="config-tabs">
            <button type="button" className={tomlCategory === "action_tokens" ? "config-tab is-active" : "config-tab"} onClick={() => setTomlCategory("action_tokens")}>Action Tokens</button>
            <button type="button" className={tomlCategory === "action_hooks" ? "config-tab is-active" : "config-tab"} onClick={() => setTomlCategory("action_hooks")}>Action Hooks</button>
            <button type="button" className={tomlCategory === "language_descriptors" ? "config-tab is-active" : "config-tab"} onClick={() => setTomlCategory("language_descriptors")}>Language Descriptors</button>
          </div>
        </div>
        <div className="page-fill-region">
          <div className="toml-layout settings-documents-layout">
            <div className="flat-table">
              <div className="table-head toml-table-row" aria-hidden="true">
                <span>Target</span>
                <span>Active</span>
                <span>Context</span>
              </div>
              {filteredDocuments.map((document) => (
                <button
                  key={document.path}
                  type="button"
                  className={document.path === selectedTomlPath ? "table-row toml-table-row is-selected" : "table-row toml-table-row"}
                  onClick={() => setSelectedTomlPath(document.path)}
                >
                  <span title={document.path}>{document.target}</span>
                  <span className="toggle-cell" title={document.active}>
                    <span className={isDocumentActive(document) ? "toggle is-on" : "toggle"} aria-hidden="true">
                      <span className="toggle-knob" />
                    </span>
                  </span>
                  <span title={document.language_context}>{document.language_context}</span>
                </button>
              ))}
            </div>
            <div className="editor-panel">
              {selectedTomlDocument ? (
                <>
                  <textarea
                    className="toml-editor"
                    value={tomlDrafts[selectedTomlDocument.path] ?? selectedTomlDocument.content}
                    onChange={(event) => setTomlDraft(selectedTomlDocument.path, event.target.value)}
                  />
                  <button
                    className="action-button"
                    type="button"
                    disabled={!selectedTomlDocument.editable || savingTomlPath === selectedTomlDocument.path}
                    onClick={handleTomlSave}
                  >
                    {savingTomlPath === selectedTomlDocument.path ? "Saving..." : "Save TOML"}
                  </button>
                </>
              ) : (
                <div className="empty-panel">No TOML documents found for this category.</div>
              )}
            </div>
          </div>
        </div>
      </section>
    );
  }

type ImportExportModalProps = {
  selectedConfigTextDocument: DashboardTomlDocument | null;
  configTextDocuments: DashboardTomlDocument[];
  selectedConfigTextPath: string;
  setConfigTextPath: (path: string) => void;
  close: () => void;
  savingTomlPath: string | null;
  tomlDrafts: Record<string, string>;
  setTomlDraft: (path: string, value: string) => void;
  handleConfigTextSave: () => void;
};

export function ImportExportModal({
  selectedConfigTextDocument,
  configTextDocuments,
  selectedConfigTextPath,
  setConfigTextPath,
  close,
  savingTomlPath,
  tomlDrafts,
  setTomlDraft,
  handleConfigTextSave,
}: ImportExportModalProps) {
  if (!selectedConfigTextDocument) {
    return null;
  }

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Import or export AIDOCS config" onClick={close}>
      <div className="modal-panel">
        <div className="page-header modal-header" onClick={(event) => event.stopPropagation()}>
          <div>
            <div className="section-label">Import / Export config</div>
            <h3>{selectedConfigTextDocument.target}</h3>
          </div>
          <button className="action-button action-button-small modal-close" type="button" onClick={close}>
            Close
          </button>
        </div>
        <div className="config-text-toolbar" onClick={(event) => event.stopPropagation()}>
          <label className="field-control">
            <span>Source</span>
            <select value={selectedConfigTextPath} onChange={(event) => setConfigTextPath(event.target.value)}>
              {configTextDocuments.map((document) => (
                <option key={document.path} value={document.path}>
                  {document.target} · {document.scope}
                </option>
              ))}
            </select>
          </label>
          <button
            className="action-button action-button-small"
            type="button"
            onClick={() => navigator.clipboard.writeText(tomlDrafts[selectedConfigTextDocument.path] ?? selectedConfigTextDocument.content)}
          >
            Copy
          </button>
          <button
            className="action-button action-button-small"
            type="button"
            disabled={!selectedConfigTextDocument.editable || savingTomlPath === selectedConfigTextDocument.path}
            onClick={handleConfigTextSave}
          >
            {savingTomlPath === selectedConfigTextDocument.path ? "Saving..." : "Save"}
          </button>
        </div>
        <textarea
          className="toml-editor modal-editor"
          onClick={(event) => event.stopPropagation()}
          value={tomlDrafts[selectedConfigTextDocument.path] ?? selectedConfigTextDocument.content}
          onChange={(event) => setTomlDraft(selectedConfigTextDocument.path, event.target.value)}
        />
      </div>
    </div>
  );
}

type DangerConfirmModalProps = {
  close: () => void;
  confirm: () => void;
};

export function DangerConfirmModal({ close, confirm }: DangerConfirmModalProps) {
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Confirm dev mode change" onClick={close}>
      <div className="modal-panel danger-modal" onClick={(event) => event.stopPropagation()}>
        <div className="page-header modal-header">
          <div>
            <div className="section-label">Confirm change</div>
            <h3>Update dev_mode</h3>
          </div>
          <button className="action-button action-button-small modal-close" type="button" onClick={close}>
            Cancel
          </button>
        </div>
        <p className="warning-text modal-warning-copy">
          This setting allows editing AIDOCS MCP infrastructure source files. Only enable it when you intentionally need to change protected internal code.
        </p>
        <div className="danger-modal-actions">
          <button className="action-button action-button-small" type="button" onClick={close}>
            Keep current setting
          </button>
          <button className="action-button action-button-small action-button-danger" type="button" onClick={confirm}>
            Confirm change
          </button>
        </div>
      </div>
    </div>
  );
}

export function LoadingOverlay() {
  return (
    <div className="app-overlay" role="status" aria-live="polite">
      <div className="app-overlay-panel">
        <div className="overlay-spinner" aria-hidden="true" />
        <strong>Loading runtime snapshot</strong>
        <span>Refreshing project, session, and settings state.</span>
      </div>
    </div>
  );
}

type ToastStackProps = {
  notice: string | null;
  error: string | null;
  clearNotice: () => void;
  clearError: () => void;
};

export function ToastStack({ notice, error, clearNotice, clearError }: ToastStackProps) {
  if (!notice && !error) {
    return null;
  }

  return (
    <div className="toast-stack" aria-live="polite">
      {notice ? (
        <div className="toast toast-success">
          <span>{notice}</span>
          <button type="button" className="toast-close" onClick={clearNotice}>
            Close
          </button>
        </div>
      ) : null}
      {error ? (
        <div className="toast toast-error" role="alert">
          <span>{error}</span>
          <button type="button" className="toast-close" onClick={clearError}>
            Close
          </button>
        </div>
      ) : null}
    </div>
  );
}
