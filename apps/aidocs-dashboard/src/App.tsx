import { useEffect, useMemo, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import logoUrl from "./cn-logo.svg";
import type { ContextBudgetResult, DashboardConfigEntry, MonitoringSnapshot, RegistrySearchResult, SkillScanResult } from "./dashboardApi";
import { contextBudgetCheck, contextCompact, executionClearTokens, executionClearToolCalls, executionPrune, loadMetricsSnapshot, mcpRegistrySearch, skillScanResults } from "./dashboardApi";
import { ConductorPage } from "./ConductorPage";
import { ExecutionPage } from "./ExecutionPage";
import { MonitoringPage } from "./MonitoringPage";
import { OverviewPage } from "./OverviewPage";
import { RegistryPage } from "./RegistryPage";
import { SkillsPage } from "./SkillsPage";
import { SessionsPage } from "./SessionsPage";
import { SettingsPage } from "./SettingsPage";
import { TomlConfigsPage } from "./TomlConfigsPage";
import { UsagePage } from "./dashboardCharts";
import { DangerConfirmModal, ImportExportModal, LoadingOverlay, ToastStack } from "./dashboardModals";
import {
  globalNavigation,
  navigation,
  parseProgressPercent,
  readAidocsVersion,
  scaleRows,
  type DropdownOption,
  type NavKey,
  type SettingsScope,
  type TomlCategory,
} from "./dashboardUtils";
import { useDashboardData } from "./useDashboardData";
import { useDashboardUi } from "./useDashboardUi";

function HeaderDropdown({
  label,
  value,
  options,
  open,
  onToggle,
  onSelect,
}: {
  label: string;
  value: string;
  options: DropdownOption[];
  open: boolean;
  onToggle: () => void;
  onSelect: (value: string) => void;
}) {
  const selected = options.find((option) => option.value === value) ?? options[0] ?? null;

  return (
    <div className={open ? "field-control dropdown-field is-open" : "field-control dropdown-field"}>
      <span>{label}</span>
      <button type="button" className="dropdown-trigger" onClick={onToggle} aria-expanded={open}>
        <span className="dropdown-trigger-label">{selected?.label ?? "Select"}</span>
        <span className="dropdown-trigger-icon" aria-hidden="true">▾</span>
      </button>
      {open ? (
        <div className="dropdown-menu" role="listbox" aria-label={label}>
          {options.map((option) => (
            <button
              key={option.value}
              type="button"
              className={option.value === value ? "dropdown-option is-selected" : "dropdown-option"}
              onClick={() => onSelect(option.value)}
            >
              <span>{option.label}</span>
              {option.subtitle ? <small>{option.subtitle}</small> : null}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function App() {
  const [activeNav, setActiveNav] = useState<NavKey>("overview");

  const [settingsScope, setSettingsScope] = useState<SettingsScope>("project");
  const [tomlCategory, setTomlCategory] = useState<TomlCategory>("action_tokens");
  const [registryQuery, setRegistryQuery] = useState("");
  const [registryResults, setRegistryResults] = useState<RegistrySearchResult[]>([]);
  const [registryCursor, setRegistryCursor] = useState<string | null>(null);
  const [registryLoading, setRegistryLoading] = useState(false);
  const [monitoringSnapshot, setMonitoringSnapshot] = useState<MonitoringSnapshot | null>(null);
  const [monitoringLoading, setMonitoringLoading] = useState(false);
  const [skillScanResultsState, setSkillScanResultsState] = useState<SkillScanResult[]>([]);
  const [skillsLoading, setSkillsLoading] = useState(false);
  const [contextBudgetState, setContextBudgetState] = useState<ContextBudgetResult | null>(null);
  const [compactingContext, setCompactingContext] = useState(false);
  const [executionClearing, setExecutionClearing] = useState(false);
  const {
    importExportOpen,
    configTextPath,
    pendingDangerSettingPath,
    openDropdown,
    setTomlPage,
    setImportExportOpen,
    setConfigTextPath,
    setPendingDangerSettingPath,
    setOpenDropdown,
  } = useDashboardUi();
  const {
    snapshot,
    projects,
    tomlDocuments,
    selectedProjectRoot,
    selectedSessionId,
    selectedTomlPath,
    loading,
    error,
    notice,
    draftValues,
    tomlDrafts,
    savingSetting,
    savingTomlPath,
    setSelectedSessionId,
    setSelectedTomlPath,
    setError,
    setNotice,
    setDraftValue,
    setTomlDraft,
    handleProjectChange,
    saveConfigEntry,
    saveTomlPath,
    refresh,
  } = useDashboardData();

  const selectedSession = snapshot?.selected_session ?? null;
  const selectedProject = useMemo(() => {
    const currentRoot = selectedProjectRoot ?? snapshot?.project.project_root;
    if (!currentRoot) {
      return null;
    }
    return projects.find((project) => project.project_root === currentRoot) ?? null;
  }, [projects, selectedProjectRoot, snapshot]);
  const configSections = useMemo(() => {
    const scopeKey = settingsScope === "global" ? "user" : settingsScope;
    const groups = new Map<string, DashboardConfigEntry[]>();
    for (const entry of snapshot?.config.entries ?? []) {
        if (entry.path === "global.aidocs_core_version") {
          continue;
        }
      if (!entry.allowed_scopes.includes(scopeKey) && !entry.allowed_scopes.includes(settingsScope)) {
        continue;
      }
      const section = entry.section || "misc";
      groups.set(section, [...(groups.get(section) ?? []), entry]);
    }
      const securitySections = new Set(["dev", "gate"]);
      return Array.from(groups.entries())
        .sort(([a], [b]) => {
          const aSecure = securitySections.has(a) ? 1 : 0;
          const bSecure = securitySections.has(b) ? 1 : 0;
          return aSecure - bSecure || a.localeCompare(b);
        })
        .map(([section, entries]) => ({ section, entries }));
  }, [snapshot, settingsScope]);
  const devModeEntry = useMemo(
    () => snapshot?.config.entries.find((entry) => entry.path === "dev.dev_mode") ?? null,
    [snapshot],
  );
  const selectedTomlDocument = tomlDocuments.find((document) => document.path === selectedTomlPath) ?? null;
  const conductorLanes = selectedSession?.conductor?.graph?.lanes ?? [];
  const runnableLaneIds = selectedSession?.conductor?.runnable?.runnable_lane_ids ?? [];
  const blockedReasons = selectedSession?.conductor?.runnable?.blocked_reasons ?? {};
  const progressPercent = parseProgressPercent(selectedSession?.plan_overview.progress);
  const actionRows = useMemo(() => scaleRows(snapshot?.token_usage.proxy_series.top_action_kinds ?? []), [snapshot]);
  const eventRows = useMemo(() => scaleRows(snapshot?.token_usage.proxy_series.event_breakdown ?? []), [snapshot]);
  const intentRows = useMemo(() => scaleRows(snapshot?.token_usage.proxy_series.intent_breakdown ?? []), [snapshot]);
  const projectValue = selectedProjectRoot ?? snapshot?.project.project_root ?? projects[0]?.project_root ?? "";
  const sessionValue = selectedSessionId ?? snapshot?.selected_session_id ?? "__all__";
  const aidocsVersion = useMemo(() => readAidocsVersion(snapshot), [snapshot]);
  const configTextDocuments = useMemo(
    () => tomlDocuments.filter((document) => document.path === "aidocs.toml" || document.path.endsWith("/aidocs.toml")),
    [tomlDocuments],
  );
  const scopeForConfigText = settingsScope === "global" ? "Global" : settingsScope === "session" ? "Session" : "Project";
  const selectedConfigTextDocument = configTextDocuments.find((document) => document.scope === scopeForConfigText) ?? configTextDocuments[0] ?? null;
  const projectOptions = useMemo<DropdownOption[]>(
    () => projects.map((project) => ({ value: project.project_root, label: project.title, subtitle: `${project.session_count} sessions` })),
    [projects],
  );
  const sessionOptions = useMemo<DropdownOption[]>(
    () => [
      { value: "__all__", label: "All Sessions" },
      ...(snapshot?.sessions ?? []).map((session) => ({
        value: session.session_id,
        label: session.title ?? session.session_id,
        subtitle: session.status ?? undefined,
      })),
    ],
    [snapshot],
  );
  const isAllSessions = sessionValue === "__all__" || sessionValue === "";

  // Auto-switch settings scope when session selection changes
  if (isAllSessions && settingsScope === "session") {
    setSettingsScope("project");
  }

  function requestConfigSave(entry: DashboardConfigEntry, scope?: string) {
    if (entry.path === "dev.dev_mode") {
      setPendingDangerSettingPath(entry.path);
      return;
    }
    void saveConfigEntry(entry, scope);
  }

  async function handleTomlSave() {
    if (!selectedTomlDocument) {
      return;
    }
    await saveTomlPath(
      selectedTomlDocument.path,
      tomlDrafts[selectedTomlDocument.path] ?? selectedTomlDocument.content,
    );
  }

  async function handleConfigTextSave() {
    if (!selectedConfigTextDocument) {
      return;
    }
    await saveTomlPath(
      selectedConfigTextDocument.path,
      tomlDrafts[selectedConfigTextDocument.path] ?? selectedConfigTextDocument.content,
    );
  }

  async function searchRegistry(cursor?: string) {
    setRegistryLoading(true);
    try {
      const response = await mcpRegistrySearch(registryQuery.trim(), 20, cursor);
      setRegistryResults((current) => (cursor ? [...current, ...response.servers] : response.servers));
      setRegistryCursor(response.next_cursor ?? null);
    } finally {
      setRegistryLoading(false);
    }
  }

  async function refreshMonitoring() {
    setMonitoringLoading(true);
    try {
      const response = await loadMetricsSnapshot();
      setMonitoringSnapshot(response.snapshot);
    } finally {
      setMonitoringLoading(false);
    }
  }

  async function refreshSkills() {
    setSkillsLoading(true);
    try {
      const response = await skillScanResults(selectedProjectRoot ?? snapshot?.project.project_root, isAllSessions ? undefined : sessionValue || undefined);
      setSkillScanResultsState(response.results);
    } finally {
      setSkillsLoading(false);
    }
  }

  async function refreshContextBudget() {
    const root = selectedProjectRoot ?? snapshot?.project.project_root;
    if (!root) return;
    const budgetSession = isAllSessions
      ? (snapshot?.selected_session_id ?? undefined)
      : (sessionValue || snapshot?.selected_session_id || undefined);
    try {
      const response = await contextBudgetCheck(root, budgetSession);
      if (response && response.result) {
        setContextBudgetState(response.result);
      } else if (response && (response as Record<string, unknown>).available !== undefined) {
        setContextBudgetState(response as unknown as ContextBudgetResult);
      }
    } catch (err) {
      setNotice?.(`Context budget error: ${String(err)}`);
    }
  }

  async function compactContextBudget() {
    const root = selectedProjectRoot ?? snapshot?.project.project_root;
    if (!root) return;
    setCompactingContext(true);
    try {
      await contextCompact(root, isAllSessions ? undefined : sessionValue || undefined);
      await refreshContextBudget();
    } finally {
      setCompactingContext(false);
    }
  }



  useEffect(() => {
    if (activeNav === "monitoring") {
      void refreshMonitoring();
    }
    if (activeNav === "skills") {
      void refreshSkills();
    }
    if (activeNav === "overview") {
      void refreshContextBudget();
    }
  }, [activeNav, selectedProjectRoot, sessionValue, snapshot]);

  useEffect(() => {
    if (!tomlDocuments.length) {
      return;
    }
    setSelectedTomlPath((current) =>
      current && tomlDocuments.some((document) => document.path === current)
        ? current
        : tomlDocuments[0].path,
    );
  }, [tomlDocuments]);

  useEffect(() => {
    if (!configTextDocuments.length) {
      setConfigTextPath(null);
      return;
    }
    setConfigTextPath((current) =>
      current && configTextDocuments.some((document) => document.path === current)
        ? current
        : configTextDocuments[0].path,
    );
  }, [configTextDocuments]);

  useEffect(() => {
    if (!notice) {
      return;
    }
    const timeout = window.setTimeout(() => setNotice(null), 3200);
    return () => window.clearTimeout(timeout);
  }, [notice]);

  useEffect(() => {
    if (!error) {
      return;
    }
    const timeout = window.setTimeout(() => setError(null), 5200);
    return () => window.clearTimeout(timeout);
  }, [error]);

  // Auto-prune old execution events on mount (silent, no UI feedback)
  useEffect(() => {
    executionPrune().catch(() => {});
  }, []);

  // Auto-refresh every 30 seconds
  useEffect(() => {
    const interval = window.setInterval(() => {
      if (activeNav !== "config_toml" && activeNav !== "settings") refresh();
    }, 30000);
    return () => window.clearInterval(interval);
  }, [activeNav]);

  function renderActivePage() {
    if (!snapshot) {
      return null;
    }
    if (activeNav === "overview") {
      return <OverviewPage snapshot={snapshot} selectedSession={selectedSession} sessionBreakdown={snapshot.token_usage.session_breakdown ?? []} intentRows={intentRows} eventRows={eventRows} contextBudget={contextBudgetState} compactingContext={compactingContext} onCompactContext={() => void compactContextBudget()} />;
    }
    if (activeNav === "sessions") {
      return <SessionsPage sessions={snapshot.sessions ?? []} sessionValue={sessionValue} onSelectSession={setSelectedSessionId} />;
    }
    if (activeNav === "conductor") {
      return (
        <ConductorPage
          selectedSession={selectedSession}
          progressPercent={progressPercent}
          conductorLanes={conductorLanes}
          runnableLaneIds={runnableLaneIds}
          blockedReasons={blockedReasons}
          recentExecution={snapshot.execution.recent}
        />
      );
    }
    if (activeNav === "execution") {
        const scopedSessionId = isAllSessions ? undefined : sessionValue;
        return (
          <ExecutionPage
            recentExecution={snapshot.execution.recent}
            clearing={executionClearing}
            onClearToolCalls={async () => {
              setExecutionClearing(true);
              try { await executionClearToolCalls(undefined, scopedSessionId); setNotice(scopedSessionId ? `Tool calls cleared for session ${scopedSessionId}.` : "All tool calls cleared."); refresh(); } catch (e) { setError(String(e)); }
              setExecutionClearing(false);
            }}
          />
        );
      }
    if (activeNav === "usage") {
      return (
          <UsagePage
            reason={snapshot.token_usage.reason}
            tokenEstimates={snapshot.token_usage.token_estimates ?? { tokens_in: 0, tokens_out: 0, total: 0 }}
            sessionBreakdown={snapshot.token_usage.session_breakdown ?? []}
            actionRows={actionRows}
            eventRows={eventRows}
            intentRows={intentRows}
            clearingTokens={executionClearing}
            onClearTokens={async () => {
              setExecutionClearing(true);
              try { await executionClearTokens(); setNotice("Token usage cleared."); refresh(); } catch (e) { setError(String(e)); }
              setExecutionClearing(false);
            }}
          />
      );
    }
    if (activeNav === "registry") {
      return <RegistryPage results={registryResults} query={registryQuery} setQuery={setRegistryQuery} nextCursor={registryCursor} loading={registryLoading} onSearch={() => void searchRegistry()} onMore={() => void searchRegistry(registryCursor ?? undefined)} />;
    }
    if (activeNav === "monitoring") {
      return <MonitoringPage snapshot={snapshot} />;
    }
    if (activeNav === "skills") {
      return <SkillsPage results={skillScanResultsState} />;
    }
    if (activeNav === "config_toml") {
      return (
        <TomlConfigsPage
          tomlCategory={tomlCategory}
          setTomlCategory={setTomlCategory}
          tomlDocuments={tomlDocuments}
          selectedTomlPath={selectedTomlPath}
          setSelectedTomlPath={(path) => setSelectedTomlPath(path)}
          selectedTomlDocument={selectedTomlDocument}
          tomlDrafts={tomlDrafts}
          setTomlDraft={setTomlDraft}
          savingTomlPath={savingTomlPath}
          handleTomlSave={() => void handleTomlSave()}
        />
      );
    }
    return (
      <SettingsPage
        settingsScope={settingsScope}
        setSettingsScope={setSettingsScope}
        hasProject={!!selectedProjectRoot || !!snapshot?.project.project_root}
        hasSession={!isAllSessions && !!sessionValue}
        configSections={configSections}
        draftValues={draftValues}
        savingSetting={savingSetting}
        requestConfigSave={requestConfigSave}
        setDraftValue={setDraftValue}
        devModeEntry={devModeEntry}
        openImportExport={() => setImportExportOpen(true)}
      />
    );
  }

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand-block" style={{ cursor: "pointer" }} onClick={() => invoke("open_url", { url: "https://codenexus.cloud" }).catch(() => {})}>
            <img className="brand-logo" src={logoUrl} alt="CodeNexus" />
            <div className="brand-text">
              <h1>AIDOCS Dashboard</h1>
              <span className="brand-subtitle">CodeNexus operator app</span>
            </div>
          </div>

        {snapshot?.managed_mode?.active ? (
          <div className="managed-indicator is-active">
            <span className="managed-dot" />
            <span>Managed mode active</span>
          </div>
        ) : (
          <div className="managed-indicator">
            <span className="managed-dot" />
            <span>Unmanaged</span>
          </div>
        )}

        <nav className="nav-list" aria-label="Dashboard navigation">
          {navigation.map((item) => (
            <button
              key={item.value}
              className={item.value === activeNav ? "nav-item is-active" : "nav-item"}
              type="button"
              onClick={() => setActiveNav(item.value)}
            >
              {item.name}
            </button>
          ))}
        </nav>

        <nav className="nav-list nav-list-global" aria-label="Global settings">
          {globalNavigation.map((item) => (
            <button
              key={item.value}
              className={item.value === activeNav ? "nav-item is-active" : "nav-item"}
              type="button"
              onClick={() => setActiveNav(item.value)}
            >
              {item.name}
            </button>
          ))}
        </nav>

        <footer className="sidebar-footer">
          <strong>AIDOCS Version: {aidocsVersion}</strong>
        </footer>
      </aside>

      <main className="content">
        <div className="content-header">
          <header className="topbar">
            <div className="topbar-copy">
              <h2>{snapshot?.project.project_name ?? selectedProject?.title ?? "AIDOCS"}</h2>
              <p title={snapshot?.project.project_root ?? selectedProject?.project_root ?? "Select a known project."}>
                {snapshot?.project.project_root ?? selectedProject?.project_root ?? "Select a known project."}
              </p>
            </div>

            <div className="control-strip">
              <HeaderDropdown
                label="Project"
                value={projectValue}
                options={projectOptions}
                open={openDropdown === "project"}
                onToggle={() => setOpenDropdown((current) => (current === "project" ? null : "project"))}
                onSelect={(value) => {
                  handleProjectChange(value);
                  setOpenDropdown(null);
                }}
              />
              <HeaderDropdown
                label="Session"
                value={sessionValue}
                options={sessionOptions}
                open={openDropdown === "session"}
                onToggle={() => setOpenDropdown((current) => (current === "session" ? null : "session"))}
                onSelect={(value) => {
                  setSelectedSessionId(value === "__all__" ? "__all__" : (value || undefined));
                  setOpenDropdown(null);
                }}
              />
              <button className="action-button" type="button" onClick={refresh}>
                Refresh
              </button>
            </div>
          </header>
        </div>

        <div className="content-body">{renderActivePage()}</div>

        {importExportOpen ? (
          <ImportExportModal
            selectedConfigTextDocument={selectedConfigTextDocument}
            configTextDocuments={configTextDocuments}
            selectedConfigTextPath={selectedConfigTextDocument?.path ?? ""}
            setConfigTextPath={(path) => setConfigTextPath(path)}
            close={() => setImportExportOpen(false)}
            savingTomlPath={savingTomlPath}
            tomlDrafts={tomlDrafts}
            setTomlDraft={setTomlDraft}
            handleConfigTextSave={() => void handleConfigTextSave()}
              scopeLabel={settingsScope === "global" ? "Global config" : settingsScope === "session" ? `Session · ${sessionValue}` : `Project · ${snapshot?.project.project_name ?? "unknown"}`}
            />
        ) : null}

        {pendingDangerSettingPath && devModeEntry ? (
          <DangerConfirmModal
            settingPath={pendingDangerSettingPath}
            close={() => setPendingDangerSettingPath(null)}
            confirm={() => {
              setPendingDangerSettingPath(null);
              void saveConfigEntry(devModeEntry, settingsScope === "global" ? "user" : settingsScope);
            }}
          />
        ) : null}

        {loading ? <LoadingOverlay /> : null}

        <ToastStack
          notice={notice}
          error={error}
          clearNotice={() => setNotice(null)}
          clearError={() => setError(null)}
        />
      </main>
    </div>
  );
}

export default App;
