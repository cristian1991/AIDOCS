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
  type SettingsView,
} from "./dashboardUtils";

type SelectedSession = DashboardSnapshot["selected_session"];
type ConductorLane = { lane_id: string; name: string; depends_on?: string[] };

type OverviewPageProps = {
  snapshot: DashboardSnapshot;
  selectedSession: SelectedSession;
  overviewStats: Array<{ label: string; value: string; note: string }>;
};

export function OverviewPage({ snapshot, selectedSession, overviewStats }: OverviewPageProps) {
  return (
    <section className="page">
      <div className="dashboard-grid">
        {overviewStats.map((item) => (
          <article key={item.label} className="dashboard-card">
            <div className="section-label">{item.label}</div>
            <strong>{item.value}</strong>
            <span>{item.note}</span>
          </article>
        ))}
      </div>

      <div className="overview-grid">
        <section className="flat-panel">
          <div className="section-label">Project</div>
          <div className="summary-list">
            <div className="summary-row"><span>Project root</span><strong>{snapshot.project.project_root}</strong></div>
            <div className="summary-row"><span>Code files</span><strong>{snapshot.project.code_file_count}</strong></div>
            <div className="summary-row"><span>Schema entities</span><strong>{snapshot.project.schema_entity_count}</strong></div>
            <div className="summary-row"><span>Sessions</span><strong>{snapshot.project.session_count}</strong></div>
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

        <section className="flat-panel overview-execution-panel">
          <div className="section-label">Execution</div>
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
      </div>
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
  capabilityRows: Array<DashboardSeriesItem & { width: string }>;
  actionRows: Array<DashboardSeriesItem & { width: string }>;
  eventRows: Array<DashboardSeriesItem & { width: string }>;
};

function MetricList({ items }: { items: Array<DashboardSeriesItem & { width: string }> }) {
  return (
    <div className="metric-list">
      {items.map((item) => (
        <div key={item.label} className="metric-row">
          <span className="metric-label" title={item.label}>{item.label}</span>
          <div className="metric-track"><div className="metric-fill" style={{ width: item.width }} /></div>
          <strong>{item.count}</strong>
        </div>
      ))}
    </div>
  );
}

export function UsagePage({ reason, capabilityRows, actionRows, eventRows }: UsagePageProps) {
  return (
    <section className="page">
      <div className="usage-grid">
        <section className="flat-panel">
          <div className="section-label">Capabilities</div>
          <MetricList items={capabilityRows} />
        </section>
        <section className="flat-panel">
          <div className="section-label">Action kinds</div>
          <MetricList items={actionRows} />
        </section>
        <section className="flat-panel">
          <div className="section-label">Event kinds</div>
          <MetricList items={eventRows} />
          <p className="panel-copy usage-note">{reason}</p>
        </section>
      </div>
    </section>
  );
}

type SettingsPageProps = {
  settingsView: SettingsView;
  setSettingsView: (view: SettingsView) => void;
  configSections: Array<{ section: string; entries: DashboardConfigEntry[] }>;
  draftValues: Record<string, string>;
  savingSetting: string | null;
  requestConfigSave: (entry: DashboardConfigEntry) => void;
  setDraftValue: (path: string, value: string) => void;
  devModeEntry: DashboardConfigEntry | null;
  openImportExport: () => void;
  paginatedTomlDocuments: DashboardTomlDocument[];
  selectedTomlPath: string | null;
  setSelectedTomlPath: (path: string) => void;
  tomlPage: number;
  totalTomlPages: number;
  setTomlPage: (updater: number | ((current: number) => number)) => void;
  selectedTomlDocument: DashboardTomlDocument | null;
  tomlDrafts: Record<string, string>;
  setTomlDraft: (path: string, value: string) => void;
  savingTomlPath: string | null;
  handleTomlSave: () => void;
};

function SettingInput({
  entry,
  currentValue,
  settingTooltip,
  savingSetting,
  onChange,
}: {
  entry: DashboardConfigEntry;
  currentValue: string;
  settingTooltip: string;
  savingSetting: string | null;
  onChange: (value: string) => void;
}) {
  const disabled = !isDashboardEditable(entry) || savingSetting === entry.path;
  if (entry.allowed_values?.length) {
    return (
      <select value={currentValue} disabled={disabled} title={settingTooltip} onChange={(event) => onChange(event.target.value)}>
        {entry.allowed_values.map((value) => (
          <option key={value} value={value}>
            {value}
          </option>
        ))}
      </select>
    );
  }
  if (entry.type === "boolean") {
    return (
      <select value={currentValue} disabled={disabled} title={settingTooltip} onChange={(event) => onChange(event.target.value)}>
        <option value="true">true</option>
        <option value="false">false</option>
      </select>
    );
  }
  return <input value={currentValue} disabled={disabled} title={settingTooltip} onChange={(event) => onChange(event.target.value)} />;
}

export function SettingsPage({
  settingsView,
  setSettingsView,
  configSections,
  draftValues,
  savingSetting,
  requestConfigSave,
  setDraftValue,
  devModeEntry,
  openImportExport,
  paginatedTomlDocuments,
  selectedTomlPath,
  setSelectedTomlPath,
  tomlPage,
  totalTomlPages,
  setTomlPage,
  selectedTomlDocument,
  tomlDrafts,
  setTomlDraft,
  savingTomlPath,
  handleTomlSave,
}: SettingsPageProps) {
  return (
    <section className="page page-config">
      <div className="page-fixed-header">
        <div className="config-toolbar">
          <div className="config-tabs" role="tablist" aria-label="Config views">
            <button
              type="button"
              role="tab"
              aria-selected={settingsView === "typed"}
              className={settingsView === "typed" ? "config-tab is-active" : "config-tab"}
              onClick={() => setSettingsView("typed")}
            >
              Typed settings
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={settingsView === "documents"}
              className={settingsView === "documents" ? "config-tab is-active" : "config-tab"}
              onClick={() => setSettingsView("documents")}
            >
              TOML documents
            </button>
          </div>
        </div>
      </div>
      <div className="page-scroll-region">
        {settingsView === "typed" ? (
          <div className="config-section-list">
            {configSections.map(({ section, entries }) => (
              <section key={section} className="flat-panel">
                <div className="page-header compact-header">
                  <div>
                    <div className="section-label">Section</div>
                    <h3>{section}</h3>
                  </div>
                </div>
                <div className="setting-list">
                  {entries.map((entry) => {
                    const currentValue = draftValues[entry.path] ?? asText(entry.current_value);
                    const settingTooltip = buildSettingTooltip(entry, currentValue);
                    return (
                      <div key={entry.path} className="setting-row">
                        <div className="setting-copy">
                          <div className="setting-title-row">
                            <strong>{entry.key}</strong>
                            <span className="setting-info" title={settingTooltip} aria-label={settingTooltip}>
                              ?
                            </span>
                          </div>
                          <p>{entry.description}</p>
                          <small>
                            Default {asText(entry.default)} · {entry.requires_restart ? "restart required" : "hot reload"}
                          </small>
                        </div>
                        <div className="setting-control">
                          <SettingInput
                            entry={entry}
                            currentValue={currentValue}
                            settingTooltip={settingTooltip}
                            savingSetting={savingSetting}
                            onChange={(value) => setDraftValue(entry.path, value)}
                          />
                          <button
                            className="action-button action-button-small"
                            type="button"
                            disabled={!isDashboardEditable(entry) || savingSetting === entry.path}
                            onClick={() => requestConfigSave(entry)}
                          >
                            {savingSetting === entry.path ? "Saving..." : isDashboardEditable(entry) ? "Save" : "Read-only"}
                          </button>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </section>
            ))}
            {devModeEntry ? (
              <section className="flat-panel danger-panel">
                <div className="page-header compact-header">
                  <div>
                    <div className="section-label">Danger Zone</div>
                    <h3>dev_mode</h3>
                  </div>
                </div>
                <div className="setting-row danger-setting-row">
                  <div className="setting-copy">
                    <div className="setting-title-row">
                      <strong>{devModeEntry.key}</strong>
                      <span
                        className="setting-info"
                        title={buildSettingTooltip(devModeEntry, draftValues[devModeEntry.path] ?? asText(devModeEntry.current_value))}
                        aria-label={buildSettingTooltip(devModeEntry, draftValues[devModeEntry.path] ?? asText(devModeEntry.current_value))}
                      >
                        ?
                      </span>
                    </div>
                    <p>{devModeEntry.description}</p>
                    <small className="warning-text">
                      Warning: enabling dev mode allows edits to AIDOCS MCP source files. Use only when you intentionally want to modify infrastructure.
                    </small>
                  </div>
                  <div className="setting-control">
                    <select
                      value={draftValues[devModeEntry.path] ?? asText(devModeEntry.current_value)}
                      disabled={savingSetting === devModeEntry.path}
                      title={buildSettingTooltip(devModeEntry, draftValues[devModeEntry.path] ?? asText(devModeEntry.current_value))}
                      onChange={(event) => setDraftValue(devModeEntry.path, event.target.value)}
                    >
                      <option value="true">true</option>
                      <option value="false">false</option>
                    </select>
                    <button
                      className="action-button action-button-small action-button-danger"
                      type="button"
                      disabled={savingSetting === devModeEntry.path}
                      onClick={() => requestConfigSave(devModeEntry)}
                    >
                      {savingSetting === devModeEntry.path ? "Saving..." : "Save"}
                    </button>
                  </div>
                </div>
              </section>
            ) : null}
            <div className="settings-footer-actions">
              <button type="button" className="action-button config-import-export" onClick={openImportExport}>
                Import / Export config
              </button>
            </div>
          </div>
        ) : (
          <div className="toml-layout settings-documents-layout">
            <div className="flat-table">
              <div className="table-head toml-table-row" aria-hidden="true">
                <span>Target</span>
                <span>Scope</span>
                <span>Active</span>
                <span>Language / context</span>
              </div>
              {paginatedTomlDocuments.map((document) => (
                <button
                  key={document.path}
                  type="button"
                  className={document.path === selectedTomlPath ? "table-row toml-table-row is-selected" : "table-row toml-table-row"}
                  onClick={() => setSelectedTomlPath(document.path)}
                >
                  <span title={document.path}>{document.target}</span>
                  <span>{document.scope}</span>
                  <span className="toggle-cell" title={document.active}>
                    <span className={isDocumentActive(document) ? "toggle is-on" : "toggle"} aria-hidden="true">
                      <span className="toggle-knob" />
                    </span>
                  </span>
                  <span title={document.language_context}>{document.language_context}</span>
                </button>
              ))}
              <div className="table-pagination">
                <button
                  type="button"
                  className="action-button action-button-small"
                  disabled={tomlPage <= 1}
                  onClick={() => setTomlPage((current) => Math.max(1, current - 1))}
                >
                  Previous
                </button>
                <span>Page {tomlPage} / {totalTomlPages}</span>
                <button
                  type="button"
                  className="action-button action-button-small"
                  disabled={tomlPage >= totalTomlPages}
                  onClick={() => setTomlPage((current) => Math.min(totalTomlPages, current + 1))}
                >
                  Next
                </button>
              </div>
            </div>
            <section className="flat-panel editor-panel">
              {selectedTomlDocument ? (
                <>
                  <div className="document-meta-grid">
                    <div><span className="section-label">Target</span><strong>{selectedTomlDocument.target}</strong></div>
                    <div><span className="section-label">Category</span><strong>{selectedTomlDocument.category}</strong></div>
                    <div><span className="section-label">Scope</span><strong>{selectedTomlDocument.scope}</strong></div>
                    <div><span className="section-label">Editable</span><strong>{selectedTomlDocument.editable ? "Yes" : "No"}</strong></div>
                  </div>
                  <textarea
                    className="toml-editor"
                    value={tomlDrafts[selectedTomlDocument.path] ?? selectedTomlDocument.content}
                    onChange={(event) => setTomlDraft(selectedTomlDocument.path, event.target.value)}
                  />
                  <div className="editor-actions">
                    <button
                      className="action-button"
                      type="button"
                      disabled={!selectedTomlDocument.editable || savingTomlPath === selectedTomlDocument.path}
                      onClick={handleTomlSave}
                    >
                      {savingTomlPath === selectedTomlDocument.path ? "Saving..." : "Save TOML"}
                    </button>
                  </div>
                </>
              ) : (
                <div className="empty-panel">No TOML settings documents are available for the current project or session.</div>
              )}
            </section>
          </div>
        )}
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
