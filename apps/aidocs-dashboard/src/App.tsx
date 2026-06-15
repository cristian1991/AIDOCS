import { Suspense, lazy, useEffect, useMemo, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import logoUrl from "./cn-logo.svg";
import type { ContextBudgetResult, DashboardConfigEntry, RegistrySearchResult, SkillScanResult } from "./dashboardApi";
import { actionResultNotice, type AuthorityResult } from "./authorityPresentation";
import { contextBudgetCheck, contextCompact, executionClearTokens, executionClearToolCalls, executionPrune, mcpRegistrySearch, skillScanResults, toggleSkill, deleteSkill, setSkillProviderOverride, deleteSession, createSession, connectSession } from "./dashboardApi";
import { DangerConfirmModal, ImportExportModal, LoadingOverlay, ToastStack } from "./dashboardModals";
import {
  asText,
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
import { DegradedStrip } from "./DegradedStatePanel";
import { CastleShellWrapper } from "./CastleShellWrapper";
import { getScope, onScopeChange, type Scope } from "./webmcpScope";

// In WebMCP (cloud) scope the conductor stays local-only — it orchestrates
// agents on the local machine and has no cloud-served equivalent yet.
function ConductorLocalOnly() {
  return (
    <div className="mx-auto max-w-xl p-8">
      <div className="rounded-xl border border-castle-line bg-black/20 p-6 text-center">
        <div className="mb-2 text-2xl">🛡️</div>
        <h2 className="text-lg font-semibold text-white">Conductor is local-only</h2>
        <p className="mt-2 text-sm text-castle-mute">
          The conductor orchestrates agents on this machine, so it stays in Local
          scope for now. Switch the dashboard back to Local (top bar) to use it.
          Every other surface — sessions, execution, tool calls, monitoring — is
          live here in WebMCP scope.
        </p>
      </div>
    </div>
  );
}
import { CommandPalette, type PaletteCommand } from "./CommandPalette";
import { LaneDetailPanel } from "./LaneDetailPanel";
import { CastleDropdown } from "./CastleDropdown";
import { SettingDetailPanel } from "./SettingDetailPanel";

const ConductorPage = lazy(() => import("./ConductorPage").then((module) => ({ default: module.ConductorPage })));
const BacklogTodoPage = lazy(() => import("./BacklogTodoPage").then((module) => ({ default: module.BacklogTodoPage })));
const ConductorAgentsPage = lazy(() => import("./ConductorAgentsPage").then((module) => ({ default: module.ConductorAgentsPage })));
const SetupWizardPage = lazy(() => import("./SetupWizardPage").then((module) => ({ default: module.SetupWizardPage })));
const ExecutionPage = lazy(() => import("./ExecutionPage").then((module) => ({ default: module.ExecutionPage })));
const MonitoringPage = lazy(() => import("./MonitoringPage").then((module) => ({ default: module.MonitoringPage })));
const OverviewPage = lazy(() => import("./OverviewPage").then((module) => ({ default: module.OverviewPage })));
const LivePage = lazy(() => import("./LivePage").then((module) => ({ default: module.LivePage })));
const ShellPolicyPage = lazy(() => import("./ShellPolicyPage").then((module) => ({ default: module.ShellPolicyPage })));
const RegistryPage = lazy(() => import("./RegistryPage").then((module) => ({ default: module.RegistryPage })));
const SkillsPage = lazy(() => import("./SkillsPage").then((module) => ({ default: module.SkillsPage })));
const SessionsPage = lazy(() => import("./SessionsPage").then((module) => ({ default: module.SessionsPage })));
const SettingsPage = lazy(() => import("./SettingsPage").then((module) => ({ default: module.SettingsPage })));
const RBACPage = lazy(() => import("./RBACPage").then((module) => ({ default: module.RBACPage })));
const TomlConfigsPage = lazy(() => import("./TomlConfigsPage").then((module) => ({ default: module.TomlConfigsPage })));
const UsagePage = lazy(() => import("./dashboardCharts").then((module) => ({ default: module.UsagePage })));

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
  const [showWizard, setShowWizard] = useState(false);
  const [activeNav, setActiveNav] = useState<NavKey>("overview");
  // Dashboard scope (DoD #6): "local" = on-box AIDOCS, "web" = codenexus WebMCP.
  // Persisted + cross-component via webmcpScope; flipping it re-renders the shell.
  const [webmcpScope, setWebmcpScope] = useState<Scope>(() => getScope());
  // Flipping scope re-renders the shell (conductor guard, scope badge). The
  // data reload (project-list + snapshot from the new source) is owned by
  // useDashboardData, which subscribes to scope changes itself.
  useEffect(() => onScopeChange(() => setWebmcpScope(getScope())), []);
  const [backlogRouteOpen, setBacklogRouteOpen] = useState(
    typeof window !== "undefined" && window.location.hash === "#backlog-todo",
  );
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [selectedLaneId, setSelectedLaneId] = useState<string | null>(null);
  const [selectedSettingPath, setSelectedSettingPath] = useState<string | null>(null);

  const [settingsScope, setSettingsScope] = useState<SettingsScope>("project");
  const [tomlCategory, setTomlCategory] = useState<TomlCategory>("intent_tokens");
  const [registryQuery, setRegistryQuery] = useState("");
  const [registryResults, setRegistryResults] = useState<RegistrySearchResult[]>([]);
  const [registryCursor, setRegistryCursor] = useState<string | null>(null);
  const [registryLoading, setRegistryLoading] = useState(false);
  const [skillScanResultsState, setSkillScanResultsState] = useState<SkillScanResult[]>([]);
  const [providerOverridePending, setProviderOverridePending] = useState<string | null>(null);
  const [deletingSessionId, setDeletingSessionId] = useState<string | null>(null);
  const [contextBudgetState, setContextBudgetState] = useState<ContextBudgetResult | null>(null);
  const [compactingContext, setCompactingContext] = useState(false);
  const [executionClearing, setExecutionClearing] = useState(false);
  const {
    importExportOpen,
    pendingDangerSettingPath,
    openDropdown,
    setImportExportOpen,
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
    saveConfigBatch,
    saveBashCommandState,
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
    const groups = new Map<string, DashboardConfigEntry[]>();
    for (const entry of snapshot?.config.entries ?? []) {
        if (entry.path === "global.aidocs_core_version") {
          continue;
        }
      if (!entry.allowed_scopes.includes(settingsScope)) {
        continue;
      }
      const section = entry.section || "misc";
      groups.set(section, [...(groups.get(section) ?? []), entry]);
    }
      const securitySections = new Set(["dev", "gate", "security"]);
      return Array.from(groups.entries())
        .sort(([a], [b]) => {
          const aSecure = securitySections.has(a) ? 1 : 0;
          const bSecure = securitySections.has(b) ? 1 : 0;
          return aSecure - bSecure || a.localeCompare(b);
        })
        .map(([section, entries]) => ({ section, entries }));
  }, [snapshot, settingsScope]);
  const selectedTomlDocument = tomlDocuments.find((document) => document.path === selectedTomlPath) ?? null;
  const conductorLanes = selectedSession?.conductor?.graph?.lanes ?? [];
  const runnableLaneIds = selectedSession?.conductor?.runnable?.runnable_lane_ids ?? [];
  const blockedReasons = selectedSession?.conductor?.runnable?.blocked_reasons ?? {};
  const progressPercent = parseProgressPercent(selectedSession?.plan_overview.progress);
  const eventRows = useMemo(() => scaleRows(snapshot?.token_usage.proxy_series.event_breakdown ?? []), [snapshot]);
  const intentRows = useMemo(() => scaleRows(snapshot?.token_usage.proxy_series.intent_breakdown ?? []), [snapshot]);
  const projectValue = selectedProjectRoot ?? snapshot?.project.project_root ?? projects[0]?.project_root ?? "";
  const sessionValue = selectedSessionId ?? snapshot?.selected_session_id ?? "__all__";
  const skillSessionId = sessionValue && sessionValue !== "__all__" ? sessionValue : undefined;
  const aidocsVersion = useMemo(() => readAidocsVersion(snapshot), [snapshot]);

  // Phase 6e: Cmd-K command palette commands.
  const paletteCommands = useMemo<PaletteCommand[]>(() => {
    const cmds: PaletteCommand[] = [];
    // Navigate — every nav slot from primary + secondary nav.
    for (const item of [...navigation, ...globalNavigation]) {
      cmds.push({
        id: `nav:${item.value}`,
        kind: "navigate",
        label: `Open ${item.name}`,
        subtitle: `Switch to ${item.name} page`,
        run: () => setActiveNav(item.value),
      });
    }
    // Actions — common dashboard operations.
    cmds.push({
      id: "action:refresh",
      kind: "action",
      label: "Refresh snapshot",
      subtitle: "Re-fetch the dashboard payload",
      shortcut: "Ctrl R",
      run: () => refresh(),
    });
    if (snapshot?.project.project_root) {
      cmds.push({
        id: "action:compact",
        kind: "action",
        label: "Compact context",
        subtitle: "Run context compaction on the active session",
        run: () => void compactContextBudget(),
      });
    }
    cmds.push({
      id: "action:import_export",
      kind: "action",
      label: "Open Import / Export",
      subtitle: "Edit raw TOML config",
      run: () => setImportExportOpen(true),
    });
    // Settings — top entries from the catalog.
    for (const entry of snapshot?.config.entries ?? []) {
      cmds.push({
        id: `setting:${entry.path}`,
        kind: "setting",
        label: entry.path,
        subtitle: entry.description?.replace(/\s+/g, " ").slice(0, 80),
        run: () => setActiveNav("settings"),
      });
    }
    return cmds;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [snapshot]);
  const configTextDocuments = useMemo(
    () => tomlDocuments.filter((document) => document.path === "aidocs.toml" || document.path.endsWith("/aidocs.toml")),
    [tomlDocuments],
  );
  const scopeForConfigText = settingsScope === "global" ? "Global" : settingsScope === "session" ? "Session" : "Project";
  const selectedConfigTextDocument = configTextDocuments.find((document) => document.scope === scopeForConfigText) ?? configTextDocuments[0] ?? null;
  const projectOptions = useMemo<DropdownOption[]>(
    () =>
      projects.map((project) => ({
        value: project.project_root,
        label: project.title,
        // Gate projects carry no session counts; show their source instead.
        subtitle:
          webmcpScope === "web"
            ? (project.source ? `cloud · ${project.source}` : "cloud")
            : `${project.session_count} sessions`,
      })),
    [projects, webmcpScope],
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

  useEffect(() => {
    if (isAllSessions && settingsScope === "session") {
      setSettingsScope("project");
    }
  }, [isAllSessions, settingsScope]);

  function requestConfigSave(entry: DashboardConfigEntry, scope: string, value: string) {
    void saveConfigEntry(entry, scope, value);
  }

  function requestConfigBatchSave(items: Array<{ entry: DashboardConfigEntry; value: string }>, scope: string) {
    void saveConfigBatch(items, scope);
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
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setRegistryLoading(false);
    }
  }

  async function refreshSkills() {
    try {
      const response = await skillScanResults(selectedProjectRoot ?? snapshot?.project.project_root, isAllSessions ? undefined : sessionValue || undefined);
      setSkillScanResultsState(response.results);
    } catch (err) {
      setError(typeof err === "string" ? err : err instanceof Error ? err.message : JSON.stringify(err));
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
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setCompactingContext(false);
    }
  }



  // Load page-specific data when user navigates to that page (not on every snapshot change)
  const [loadedPages, setLoadedPages] = useState<Set<string>>(new Set());
  useEffect(() => {
    setLoadedPages(new Set());
    setContextBudgetState(null);
    setSkillScanResultsState([]);
  }, [projectValue, sessionValue]);

  useEffect(() => {
    if (!snapshot) return;
    if (loadedPages.has(activeNav)) return;
    setLoadedPages((prev) => new Set(prev).add(activeNav));
    if (activeNav === "skills") void refreshSkills();
    if (activeNav === "overview") void refreshContextBudget();
  }, [activeNav, loadedPages, snapshot]);

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
    executionPrune().catch((err) => {
      console.warn("executionPrune failed", err);
    });
  }, []);

  // Hash-based route for the backlog/todo page (Slice 1: avoids
  // extending NavKey, which lives outside this lane's scope).
  useEffect(() => {
    const onHashChange = () => {
      setBacklogRouteOpen(window.location.hash === "#backlog-todo");
    };
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

    // No auto-refresh — data loads once at startup, updates happen
    // inline when actions return fresh snapshots (save, toggle, delete).
    // Manual refresh via refresh() is available for explicit user actions.


  function renderActivePage() {
    if (!snapshot) {
      return null;
    }
    if (backlogRouteOpen) {
      return (
        <BacklogTodoPage
          projectRoot={selectedProjectRoot ?? snapshot?.project.project_root ?? null}
          sessionId={isAllSessions ? null : sessionValue}
        />
      );
    }
    if (activeNav === "overview") {
      return (
        <LivePage
          snapshot={snapshot}
          onLaneSelect={setSelectedLaneId}
          onApproveEscalation={async (requestId) => {
            // AUTHENTICATED LIVE WIRING (2026-05-26): approver email is
            // required by the CLI (no anonymous approvals). For the
            // first cut we prompt the operator; a follow-up can resolve
            // it from snapshot.config.rbac.users keyed by the bound
            // session's operator-of-record once that field lands.
            const email = window.prompt(
              "Approve escalation as (your email):",
              "",
            );
            if (!email || !email.trim()) return;
            const reason = window.prompt(
              "Reason (optional, audit trail):",
              "",
            );
            try {
              await invoke("approve_escalation", {
                projectRoot: selectedProjectRoot ?? null,
                requestId,
                approverEmail: email.trim(),
                reason: reason ?? null,
              });
              refresh();
            } catch (e) {
              setError(`Approve failed: ${String(e)}`);
            }
          }}
          onDenyEscalation={async (requestId) => {
            const email = window.prompt(
              "Deny escalation as (your email):",
              "",
            );
            if (!email || !email.trim()) return;
            const reason = window.prompt(
              "Reason for denial (recommended for audit):",
              "",
            );
            try {
              await invoke("deny_escalation", {
                projectRoot: selectedProjectRoot ?? null,
                requestId,
                approverEmail: email.trim(),
                reason: reason ?? null,
              });
              refresh();
            } catch (e) {
              setError(`Deny failed: ${String(e)}`);
            }
          }}
        />
      );
    }
    if (activeNav === "sessions") {
      return <SessionsPage sessions={snapshot.sessions ?? []} sessionValue={sessionValue} onSelectSession={setSelectedSessionId} deletingSessionId={deletingSessionId} degradedState={snapshot?.degraded_state ?? null} onDeleteSession={async (targetSessionId) => {
        if (!window.confirm(`Delete session '${targetSessionId}'? This removes its local session files.`)) return;
        setDeletingSessionId(targetSessionId);
        try {
          const response = await deleteSession(targetSessionId, selectedProjectRoot);
          // A refused delete returns ok:false (operator_auth / relation_not_
          // approved) WITHOUT throwing — surface the shared refusal banner text
          // instead of a false "Deleted" notice.
          const notice = actionResultNotice(
            response as AuthorityResult, `Deleted session: ${targetSessionId}`);
          if (!notice.ok) {
            setError(notice.text);
            return;
          }
          if (sessionValue === targetSessionId) {
            setSelectedSessionId("__all__");
          }
          setNotice(notice.text);
          refresh();
          if (response.snapshot?.selected_session_id && response.snapshot.selected_session_id !== targetSessionId) {
            setSelectedSessionId(response.snapshot.selected_session_id);
          }
        } catch (err) {
          setError(err instanceof Error ? err.message : String(err));
        } finally {
          setDeletingSessionId(null);
        }
      }}
      onCreateSession={async (title) => {
        // Return the raw control-plane result; SessionsPage renders the shared
        // authority notice (owner_grant / ownership_degraded / blocked_by).
        const res = await createSession(title, { projectRoot: selectedProjectRoot ?? undefined });
        if (res.ok !== false && !res.blocked_by) refresh();
        return res as unknown as Record<string, unknown>;
      }}
      onConnectSession={async (targetSessionId) => {
        const res = await connectSession(targetSessionId, selectedProjectRoot ?? undefined);
        if (res.ok !== false && !res.blocked_by) {
          setSelectedSessionId(targetSessionId);
          refresh();
        }
        return res as unknown as Record<string, unknown>;
      }} />;
    }
    if ((activeNav === "conductor" || activeNav === "conductor_agents") && webmcpScope === "web") {
      return <ConductorLocalOnly />;
    }
    if (activeNav === "conductor") {
      return (
            <ConductorPage
              progressPercent={progressPercent}
              conductorLanes={conductorLanes}
              runnableLaneIds={runnableLaneIds}
              blockedReasons={blockedReasons}
              recentExecution={snapshot.execution.recent}
              configEntries={snapshot.config?.entries}
              selectedSessionId={snapshot.selected_session_id}
              projectRoot={selectedProjectRoot ?? snapshot?.project.project_root ?? null}
              sessionId={isAllSessions ? null : sessionValue}
            />
      );
    }
    if (activeNav === "conductor_agents") {
      return <ConductorAgentsPage configEntries={snapshot.config?.entries} settingsScope={settingsScope} setSettingsScope={setSettingsScope} hasProject={!!snapshot?.project.project_root} hasSession={!isAllSessions} requestConfigSave={requestConfigSave} savingSetting={savingSetting} />;
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
            tokenEstimates={snapshot.token_usage.token_estimates ?? { tokens_in: 0, tokens_out: 0, total: 0 }}
            sessionBreakdown={snapshot.token_usage.session_breakdown ?? []}
            eventRows={eventRows}
            intentRows={intentRows}
            clearingTokens={executionClearing}
            onClearTokens={async () => {
              const root = selectedProjectRoot ?? snapshot.project.project_root;
              const scopedSessionId = isAllSessions ? undefined : sessionValue;
              setExecutionClearing(true);
              try { await executionClearTokens(root, scopedSessionId); setNotice(scopedSessionId ? `Token usage cleared for session ${scopedSessionId}.` : "Token usage cleared."); refresh(); } catch (e) { setError(String(e)); }
              setExecutionClearing(false);
            }}
          />
      );
    }
    if (activeNav === "rbac") {
      return <RBACPage rbac={snapshot.config?.rbac ?? null} />;
    }
    if (activeNav === "shell_policy") {
      return (
        <ShellPolicyPage
          policy={snapshot.config?.bash_policy ?? null}
          activeLayer={settingsScope}
          hasProject={!!selectedProjectRoot || !!snapshot?.project.project_root}
          hasSession={!isAllSessions && !!sessionValue}
          projectRoot={selectedProjectRoot ?? snapshot?.project.project_root ?? null}
          saving={savingSetting}
          onSetCommandState={saveBashCommandState}
          onLayerChange={setSettingsScope}
        />
      );
    }
    if (activeNav === "registry") {
      return <RegistryPage results={registryResults} query={registryQuery} setQuery={setRegistryQuery} nextCursor={registryCursor} loading={registryLoading} onSearch={() => void searchRegistry()} onMore={() => void searchRegistry(registryCursor ?? undefined)} projectRoot={selectedProjectRoot} onNotice={setNotice} onError={setError} />;
    }
    if (activeNav === "monitoring") {
      return <MonitoringPage snapshot={snapshot} />;
    }
    if (activeNav === "skills") {
        return <SkillsPage
          results={skillScanResultsState}
          providerOverridePending={providerOverridePending}
          onToggleSkill={async (skillId, enabled) => {
            if (!skillSessionId) {
              setError("Select a session to manage skills.");
              return;
            }
            try {
              const result = await toggleSkill(skillId, enabled, selectedProjectRoot, skillSessionId);
              setNotice(result?.message ?? `Skill ${skillId} ${enabled ? "enabled" : "disabled"}`);
              await refreshSkills();
            } catch (err) {
              setError(typeof err === "string" ? err : err instanceof Error ? err.message : JSON.stringify(err));
            }
          }}
          onSetProviderOverride={async (providerId, choice) => {
            setProviderOverridePending(providerId);
            try {
              const result = await setSkillProviderOverride(providerId, choice, selectedProjectRoot);
              const choiceLabel = choice ? `override set to ${choice}` : "override cleared";
              setNotice(`${providerId}: ${choiceLabel} (${result.provider_state})`);
              await refreshSkills();
            } catch (err) {
              setError(typeof err === "string" ? err : err instanceof Error ? err.message : JSON.stringify(err));
            } finally {
              setProviderOverridePending(null);
            }
          }}
          onDeleteSkill={async (skillId) => {
            try {
              await deleteSkill(skillId, selectedProjectRoot, skillSessionId);
              setNotice(`Skill ${skillId} deleted`);
              refresh();
            } catch (err) {
              setError(err instanceof Error ? err.message : String(err));
            }
          }}
          onUploadSkill={async () => {
            try {
              const filePath = await invoke<string | null>("select_skill_file");
              if (!filePath) return;
              // Rust reads the file and copies it to .MEMORY/skills/
              await invoke("import_skill_file", { projectRoot: selectedProjectRoot, filePath });
              setNotice(`Skill uploaded from: ${filePath}`);
              refresh();
            } catch (err) {
              setError(err instanceof Error ? err.message : String(err));
            }
          }}
        />;
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
        bashPolicy={snapshot?.config?.bash_policy ?? null}
        saveBashCommandState={saveBashCommandState}
        draftValues={draftValues}
        savingSetting={savingSetting}
        requestConfigSave={requestConfigSave}
        requestConfigBatchSave={requestConfigBatchSave}
        setDraftValue={setDraftValue}
        openImportExport={() => setImportExportOpen(true)}
        selectedPath={selectedSettingPath}
        onEntrySelect={setSelectedSettingPath}
      />
    );
  }

  // Show setup wizard when no projects are configured (first run)
  useEffect(() => {
    if (!loading && projects.length === 0 && !snapshot) {
      setShowWizard(true);
    }
  }, [loading, projects, snapshot]);

  if (showWizard) {
    return (
      <Suspense fallback={<LoadingOverlay />}>
        <SetupWizardPage onComplete={() => { setShowWizard(false); refresh(); }} />
      </Suspense>
    );
  }

  // WebMCP (cloud) scope renders the SAME full shell — the snapshot is just
  // sourced from the gate's `dashboard_snapshot` tool instead of the local
  // runtime (see loadDashboard). Only the conductor stays local-only (guarded
  // inside renderActivePage). The scope switch lives in the top-bar panel.
  return (
    <>
      <CastleShellWrapper
        active={activeNav}
        onSelect={setActiveNav}
        snapshot={snapshot}
        projectLabel={snapshot?.project.project_name ?? selectedProject?.title ?? undefined}
        projectPath={snapshot?.project.project_root ?? selectedProject?.project_root ?? undefined}
        sessionLabel={selectedSession?.overview?.title ?? (sessionValue === "__all__" ? "All sessions" : sessionValue || undefined)}
        onCommandPalette={() => setPaletteOpen(true)}
        onRefresh={refresh}
        contextTitle={
          selectedLaneId && activeNav === "overview"
            ? "Lane Detail"
            : selectedSettingPath && activeNav === "settings"
            ? "Setting Detail"
            : undefined
        }
        contextRail={
          selectedLaneId && activeNav === "overview" && snapshot ? (
            <LaneDetailPanel
              laneId={selectedLaneId}
              snapshot={snapshot}
              onClose={() => setSelectedLaneId(null)}
            />
          ) : undefined
        }
        projectSelector={
          <CastleDropdown
            label="Project"
            value={projectValue}
            options={projectOptions}
            open={openDropdown === "project"}
            onToggle={() => setOpenDropdown((current) => (current === "project" ? null : "project"))}
            onSelect={(value) => { handleProjectChange(value); setOpenDropdown(null); }}
          />
        }
        sessionSelector={
          <CastleDropdown
            label="Session"
            accent
            value={sessionValue}
            options={sessionOptions}
            open={openDropdown === "session"}
            onToggle={() => setOpenDropdown((current) => (current === "session" ? null : "session"))}
            onSelect={(value) => {
              setSelectedSessionId(value === "__all__" ? "__all__" : (value || undefined));
              setOpenDropdown(null);
            }}
          />
        }
        topStripExtras={
          <button
            type="button"
            className="grid h-10 w-10 place-items-center rounded-xl border border-castle-line bg-white/[0.035] text-castle-mute hover:bg-white/[0.07] hover:text-slate-200"
            title="Add project"
            onClick={async () => {
              try {
                const folder = await invoke<string | null>("select_folder");
                if (!folder) return;
                setNotice("Setting up project...");
                const result = await invoke<{ success: boolean; errors: string[] }>("setup_configure", { projectRoot: folder });
                if (result.success) {
                  setNotice(`Project added: ${folder}`);
                  handleProjectChange(folder);
                  refresh();
                } else {
                  setError(result.errors?.[0] || "Setup failed");
                }
              } catch (e) { setError(String(e)); }
            }}
          >
            +
          </button>
        }
        saveState={savingSetting ? "Saving..." : undefined}
      >
        <Suspense fallback={<LoadingOverlay />}>
          {renderActivePage()}
        </Suspense>
        <DegradedStrip
          snapshot={snapshot}
          projectRoot={selectedProjectRoot ?? null}
          onReload={refresh}
        />
      {selectedSettingPath && activeNav === "settings" && snapshot && (() => {
        const entry = snapshot.config.entries.find((e) => e.path === selectedSettingPath);
        if (!entry) return null;
        return (
          <div
            className="fixed inset-0 z-50 flex items-stretch justify-end bg-black/60 backdrop-blur-sm"
            onClick={() => setSelectedSettingPath(null)}
            role="dialog"
            aria-modal="true"
          >
            <div
              className="h-full w-[480px] max-w-[90vw] overflow-y-auto border-l border-castle-line bg-castle-card p-4 shadow-2xl"
              onClick={(e) => e.stopPropagation()}
            >
              <SettingDetailPanel
                entry={entry}
                activeScope={settingsScope}
                draftForScope={(scope) => draftValues[`${scope}:${entry.path}`]}
                saving={savingSetting === entry.path}
                onClose={() => setSelectedSettingPath(null)}
                onDraftChange={(path, scope, value) => setDraftValue(`${scope}:${path}`, value)}
                onSave={(e, scope, value) => saveConfigEntry(e, scope, value)}
                onReset={(path, scope) => {
                  const e2 = snapshot?.config.entries.find((x) => x.path === path);
                  if (e2) saveConfigEntry(e2, scope, "__inherit__");
                }}
              />
            </div>
          </div>
        );
      })()}
      </CastleShellWrapper>

      {importExportOpen ? (
        <ImportExportModal
          selectedConfigTextDocument={selectedConfigTextDocument}
          close={() => setImportExportOpen(false)}
          savingTomlPath={savingTomlPath}
          tomlDrafts={tomlDrafts}
          setTomlDraft={setTomlDraft}
          handleConfigTextSave={() => void handleConfigTextSave()}
          scopeLabel={settingsScope === "global" ? "Global config" : settingsScope === "session" ? `Session · ${sessionValue}` : `Project · ${snapshot?.project.project_name ?? "unknown"}`}
        />
      ) : null}

      {loading ? <LoadingOverlay /> : null}

      <CommandPalette
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        commands={paletteCommands}
      />

      <ToastStack
        notice={notice}
        error={error}
        clearNotice={() => setNotice(null)}
        clearError={() => setError(null)}
      />
    </>
  );
}

export default App;
