import { Suspense, lazy, useCallback, useEffect, useMemo, useState } from "react";
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
import { getMode, getScope, hasStoredLogin, isWebBuild, onScopeChange, type Scope } from "./webmcpScope";
import { isConnected, renewIfNeeded } from "./platform/webAuth";
// #509: folds an auth-probe outcome into the next authed state, so "could not
// ask" can never again be written as setDesktopAuthed(false) on a failed probe.
import { nextDesktopAuthed } from "./desktopAuthVerdict";
import { LoginPage } from "./LoginPage";
import { DesktopLoginView } from "./DesktopLoginView";
import { BindingsPanel } from "./BindingsPanel";
import {
  dashboardAuthStatus,
  bindingsList,
  type HostBindingRow,
} from "./dashboardApi";

// In WebAgent (cloud) scope the conductor stays local-only — it orchestrates
// agents on the local machine and has no cloud-served equivalent yet.
function ConductorLocalOnly() {
  return (
    <div className="page relative min-h-[70vh] overflow-hidden">
        {/* clean dark backdrop - the upgrade card is the focus, no faux placeholder boxes */}
      {/* blur overlay + lock card */}
      <div className="absolute inset-0 flex items-center justify-center bg-black/40 backdrop-blur-sm">
        <div className="mx-4 max-w-md rounded-2xl border border-castle-line bg-castle-card/90 p-7 text-center shadow-2xl">
            <div className="mb-3 text-4xl">☁️</div>
            <h2 className="text-lg font-semibold text-slate-100">Activate Cloud Conductor</h2>
            <p className="mt-2 text-sm leading-relaxed text-castle-mute">
              The Conductor orchestrates AIDOCS agents in the cloud — a <strong>CloudAgent</strong>
              capability. Your <strong>WebAgent</strong> access (catalog, code intelligence, sessions,
              skills, and config) stays fully available without it. Add CloudAgent seats to run
              cloud-orchestrated agents directly from this dashboard.
            </p>
            <button
              type="button"
              onClick={() => openUpgrade("cloudagent")}
              className="mt-4 inline-flex items-center justify-center rounded-lg border border-castle-allow/40 bg-castle-allow/15 px-4 py-2 text-sm font-semibold text-white transition hover:bg-castle-allow/25"
            >
              Activate Cloud Conductor — buy seats →
            </button>
        </div>
      </div>
    </div>
  );
}
import { CommandPalette, type PaletteCommand } from "./CommandPalette";
import { UpgradeModalHost } from "./UpgradeModal";
import { openUpgrade } from "./entitlements";
import { CastleDropdown } from "./CastleDropdown";
import { StickyGrantsIndicator } from "./StickyGrantsIndicator";
import { SettingDetailPanel } from "./SettingDetailPanel";

const ConductorPage = lazy(() => import("./ConductorPage").then((module) => ({ default: module.ConductorPage })));
const BacklogTodoPage = lazy(() => import("./BacklogTodoPage").then((module) => ({ default: module.BacklogTodoPage })));
const RefIntegrityPanel = lazy(() => import("./RefIntegrityPanel").then((module) => ({ default: module.RefIntegrityPanel })));
const ConductorAgentsPage = lazy(() => import("./ConductorAgentsPage").then((module) => ({ default: module.ConductorAgentsPage })));
const SetupWizardPage = lazy(() => import("./SetupWizardPage").then((module) => ({ default: module.SetupWizardPage })));
const ExecutionPage = lazy(() => import("./ExecutionPage").then((module) => ({ default: module.ExecutionPage })));
const MonitoringPage = lazy(() => import("./MonitoringPage").then((module) => ({ default: module.MonitoringPage })));
const OverviewPage = lazy(() => import("./OverviewPage").then((module) => ({ default: module.OverviewPage })));
const LivePage = lazy(() => import("./LivePage").then((module) => ({ default: module.LivePage })));
const FreezePanel = lazy(() => import("./FreezePanel").then((module) => ({ default: module.FreezePanel })));
const ShellPolicyPage = lazy(() => import("./ShellPolicyPage").then((module) => ({ default: module.ShellPolicyPage })));
const RegistryPage = lazy(() => import("./RegistryPage").then((module) => ({ default: module.RegistryPage })));
const SkillsPage = lazy(() => import("./SkillsPage").then((module) => ({ default: module.SkillsPage })));
const SessionsPage = lazy(() => import("./SessionsPage").then((module) => ({ default: module.SessionsPage })));
const SettingsPage = lazy(() => import("./SettingsPage").then((module) => ({ default: module.SettingsPage })));
const RBACPage = lazy(() => import("./RBACPage").then((module) => ({ default: module.RBACPage })));
const TomlConfigsPage = lazy(() => import("./TomlConfigsPage").then((module) => ({ default: module.TomlConfigsPage })));
const UsagePage = lazy(() => import("./dashboardCharts").then((module) => ({ default: module.UsagePage })));
const UpdatesPage = lazy(() => import("./UpdatesPage").then((module) => ({ default: module.UpdatesPage })));
const MemoryKgPage = lazy(() => import("./MemoryPage").then((module) => ({ default: module.MemoryPage })));

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
  // Dashboard scope (DoD #6): "local" = on-box AIDOCS, "web" = codenexus WebAgent.
  // Persisted + cross-component via webmcpScope; flipping it re-renders the shell.
  const [webmcpScope, setWebmcpScope] = useState<Scope>(() => getScope());
  // Flipping scope re-renders the shell (conductor guard, scope badge). The
  // data reload (project-list + snapshot from the new source) is owned by
  // useDashboardData, which subscribes to scope changes itself.
  useEffect(() => onScopeChange(() => setWebmcpScope(getScope())), []);
  // Desktop operator sign-in state (Empire directive 2026-07-17: 1 dashboard =
  // 1 user = bind). Reflects ACTUAL token validity via dashboard_auth_status
  // — no fake-connected shell. null = still checking. Web builds keep the
  // gate-connection flow below and skip this entirely.
  const [desktopAuthed, setDesktopAuthed] = useState<boolean | null>(
    isWebBuild() ? true : null,
  );
  // #92 "renew, never re-wall": a session whose access token aged out but which
  // still holds a refresh token must be RENEWED at boot, before the login wall
  // is ever drawn. Without this the hour simply elapsed and the user was thrown
  // back to the sign-in card — the observed 2026-07-22 symptom where the shell
  // painted and then swapped itself out. null = renewal still in flight; the
  // wall waits for it rather than flashing.
  const [renewChecked, setRenewChecked] = useState(false);
  useEffect(() => {
    let alive = true;
    // Resolves true/false; on success saveGateConnection() fires the scope event
    // and the shell re-renders on its own.
    void renewIfNeeded().finally(() => {
      if (alive) setRenewChecked(true);
    });
    return () => {
      alive = false;
    };
  }, []);
  const [backlogRouteOpen, setBacklogRouteOpen] = useState(
    typeof window !== "undefined" && window.location.hash === "#backlog-todo",
  );
  const [refIntegrityOpen, setRefIntegrityOpen] = useState(
    typeof window !== "undefined" && window.location.hash === "#ref-integrity",
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
    saveJudgeOverrideFamily,
    saveTomlPath,
    refresh,
    reloadProjects,
  } = useDashboardData();

  const checkDesktopAuth = useCallback(async () => {
    if (isWebBuild()) return;
    try {
      const st = await dashboardAuthStatus(selectedProjectRoot ?? undefined);
      setDesktopAuthed((prev) =>
        nextDesktopAuthed(prev, { ok: true, authenticated: Boolean(st.authenticated) }),
      );
    } catch {
      // Could not ask -> keep the last verdict. See desktopAuthVerdict.ts.
      setDesktopAuthed((prev) => nextDesktopAuthed(prev, { ok: false }));
      // #509 (operator, 2026-07-25): "the dashboard app requested for login
      // after switching projects, before 12hr passed". THIS is that bug.
      //
      // This callback depends on selectedProjectRoot, so it re-runs on EVERY
      // project switch -- and it used to setDesktopAuthed(false) right here.
      // But a throw from dashboardAuthStatus means we COULD NOT ASK:
      // dashboard_auth_status goes through run_json_cli (unchecked), so a failed
      // CLI spawn, a transport hiccup, or any per-project CLI error arrives as a
      // rejected promise -- indistinguishable AT THIS LINE from the authority
      // answering "no". Flipping to false laundered a transport failure into a
      // logout and threw the operator back to the login form holding a
      // perfectly valid 30-day token.
      //
      // THE LAW (operator ruling): the token is valid for autologin for 30 days
      // unless INVALIDATED, and the only invalidation events are removal from
      // the project, a platform ban, or a permissions change. A question we
      // failed to ask is none of those. Invalidation is a POSITIVE act by the
      // authority and is NEVER inferred by the client from unreachable
      // evidence.
      //
      // So: keep the last known verdict. false is only ever set from an
      // AFFIRMATIVE `authenticated: false` in the try branch above. This is NOT
      // fail-open -- the initial state is false, so a failure at boot still
      // shows the login form; what it refuses to do is destroy an ESTABLISHED
      // session because one check could not complete. Do not "simplify" this
      // back to setDesktopAuthed(false) -- that is the bug, and it is the same
      // fail-direction defect as _token_is_valid's `Err(_) => false` in main.rs
      // (fixed for #508) and the hardcoded broker banner (#504).
    }
  }, [selectedProjectRoot]);
  useEffect(() => {
    void checkDesktopAuth();
  }, [checkDesktopAuth, webmcpScope]);
  // SIGN-OUT MUST LAND IMMEDIATELY. The effect above keys on `webmcpScope`,
  // which only changes when local<->web flips — so signing out while already in
  // Local mode revoked the token and left the shell rendered, still looking
  // signed in until something else happened to re-probe. saveGateConnection()
  // fires the scope event on every connection change (including removal), so
  // subscribing here re-asks the authority the moment the credential changes.
  // The verdict rule is untouched: only an AFFIRMATIVE authenticated:false
  // flips the shell out (desktopAuthVerdict.ts) — a failed probe still keeps
  // the last verdict, so this cannot become an offline logout (#508/#509).
  useEffect(() => onScopeChange(() => void checkDesktopAuth()), [checkDesktopAuth]);

  // Host-operator bindings rail (Empire 2026-07-17: 1 dashboard = 1 user =
  // bind). Keyed on `snapshot` so it rides every data load/refresh (no second
  // timer). Fail-closed: web build or unauthenticated => nothing.
  //
  // AUTO-APPROVE REMOVED 2026-08-27 (#559), operator ruling verbatim: "559. no,
  // remove it, no auto-bind." This loop used to approve every pending
  // claude_code binding by itself whenever a settings flag was on. Each binding
  // is now approved by a human clicking "Bind to me" in BindingsPanel — the
  // same audited path, minus the standing grant.
  const [bindings, setBindings] = useState<HostBindingRow[]>([]);
  useEffect(() => {
    if (isWebBuild() || !desktopAuthed) return;
    let cancelled = false;
    (async () => {
      try {
        const res = await bindingsList(selectedProjectRoot ?? undefined);
        if (cancelled) return;
        setBindings(res.bindings ?? []);
      } catch {
        /* poll errors are non-fatal */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [snapshot, selectedProjectRoot, desktopAuthed]);

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
    if (isWebBuild()) return; // execution_prune_events is desktop-only (local DB maintenance); not wired over the gate
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
      // No snapshot yet. dashboard_snapshot is per-BOUND-project; in web scope a
      // project auto-binds (useDashboardData) and this resolves. Show guidance instead
      // of a blank screen for the transient/no-project/no-access/error cases.
      return (
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
            minHeight: "60vh",
            gap: "10px",
            textAlign: "center",
            color: "#94a3b8",
            padding: "24px",
          }}
        >
          <div style={{ fontSize: "28px" }}>🗂️</div>
          <div style={{ fontWeight: 600, color: "#e2e8f0" }}>
            {error
              ? "Couldn't load the dashboard"
              : projects.length === 0
                ? "No projects available"
                : loading
                  ? "Loading project…"
                  : "Select a project to begin"}
          </div>
          <div style={{ maxWidth: "460px", fontSize: "13px" }}>
            {error
              ? error
              : projects.length === 0
                ? (isWebBuild()
                    ? "Load a public repository, or a private repo your org connected via GitHub — use the button below. Team member without access? Ask an org OWNER/ADMIN to grant you a project."
                    : "Your account has no projects yet — ask an org OWNER/ADMIN to grant you access (project_grant_member), or register one.")
                : "Pick a project from the PROJECT selector in the top bar."}          </div>
          {isWebBuild() && !error && projects.length === 0 ? (
            <div style={{ display: "flex", gap: "10px", marginTop: "8px", flexWrap: "wrap", justifyContent: "center" }}>
              <button
                onClick={async () => {
                  const url = window.prompt("Load a project — paste a GitHub URL (a public repo, or a private one your org connected via GitHub):", "https://github.com/owner/repo");
                  if (!url || !url.trim()) return;
                  setNotice("Importing project from GitHub…");
                  try {
                    const r = await invoke<Record<string, unknown>>("project_register_from_github_url", { url: url.trim() });
                    if (r && r["_error"]) { setError(String(r["_detail"] || r["_error"])); return; }
                    setNotice("Project imported: " + String(r?.["name"] ?? url.trim()));
                      reloadProjects();
                  } catch (e) { setError(String(e)); }
                }}
                style={{ borderRadius: "8px", border: "1px solid rgba(94,234,212,0.4)", background: "rgba(94,234,212,0.15)", color: "#5eead4", padding: "8px 14px", fontSize: "13px", fontWeight: 600, cursor: "pointer" }}
              >
                + Load a project
              </button>
              <a
                href="https://codenexus.cloud/dashboard/team"
                target="_blank"
                rel="noopener noreferrer"
                style={{ borderRadius: "8px", border: "1px solid #334155", padding: "8px 14px", fontSize: "13px", color: "#94a3b8", textDecoration: "none", alignSelf: "center" }}
              >
                Connect / manage GitHub →
              </a>
            </div>
          ) : null}
        </div>
      );
    }
        if (refIntegrityOpen) {
      return (
        <RefIntegrityPanel
          projectRoot={selectedProjectRoot ?? snapshot?.project.project_root ?? null}
        />
      );
    }
        if (backlogRouteOpen) {
      return (
        <BacklogTodoPage
          projectRoot={selectedProjectRoot ?? snapshot?.project.project_root ?? null}
          sessionId={isAllSessions ? null : sessionValue}
        />
      );
    }
    // Nav-routed Backlog page (2026-07-30). The `#backlog-todo` hash above
    // stays as a deep link, but the NAV is the door: a hash-only page is
    // unreachable in the desktop build (no address bar), which is how a
    // 145-item backlog read as "there is nothing here".
    if (activeNav === "backlog") {
      return (
        <BacklogTodoPage
          projectRoot={selectedProjectRoot ?? snapshot?.project.project_root ?? null}
          sessionId={isAllSessions ? null : sessionValue}
        />
      );
    }
    if (activeNav === "ref_integrity") {
      return (
        <RefIntegrityPanel
          projectRoot={selectedProjectRoot ?? snapshot?.project.project_root ?? null}
        />
      );
    }
    if (activeNav === "overview") {
      // AUTHENTICATED LIVE WIRING (2026-05-26): approver email is
      // required by the CLI (no anonymous approvals); the WEB gate derives
      // the approver from the authenticated principal instead. Shared by
      // LivePage's Pending Approvals AND the FreezePanel alert strip.
      const approveEscalationHandler = async (requestId: string) => {
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
      };
      const denyEscalationHandler = async (requestId: string) => {
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
      };
      return (
        <div className="flex min-h-0 flex-1 flex-col">
          {/* War 0: active freezes + pending escalations surface HERE — an
              alert strip with Clear / Approve / Deny, on both shells. */}
          <FreezePanel
            projectRoot={selectedProjectRoot ?? snapshot?.project.project_root ?? null}
            onApproveEscalation={approveEscalationHandler}
            onDenyEscalation={denyEscalationHandler}
            onCleared={refresh}
          />
          <LivePage
            snapshot={snapshot}
            onLaneSelect={setSelectedLaneId}
            selectedLaneId={selectedLaneId}
            onClearLane={() => setSelectedLaneId(null)}
            onApproveEscalation={approveEscalationHandler}
            onDenyEscalation={denyEscalationHandler}
          />
        </div>
      );
    }
    if (activeNav === "sessions") {
      return <SessionsPage sessions={snapshot.sessions ?? []} connectedAgents={snapshot.connected_agents} sessionValue={sessionValue} onSelectSession={setSelectedSessionId} projectRoot={selectedProjectRoot ?? null} deletingSessionId={deletingSessionId} degradedState={snapshot?.degraded_state ?? null} gateHealth={snapshot?.gate_health ?? null} onDeleteSession={async (targetSessionId) => {
        // COLLECT A REASON, don't just confirm. session_deletion_law refuses any
        // delete whose reason is under 6 chars (blocked_by='reason_required'), and
        // this handler used to send none at all — so every dashboard delete was
        // refused. It went unnoticed because the operator-token wall fires first
        // and reports 'operator_auth'; signing in only swapped one refusal for
        // another. The reason is also what lands in the audit trail, so a prompt
        // is the honest surface for it rather than a fabricated constant.
        const deleteReason = (
          window.prompt(
            `Delete session '${targetSessionId}'?\n\n` +
              "Its files are checkpointed (restorable), not erased.\n" +
              "Enter a reason for the audit trail (at least 6 characters):",
            "",
          ) ?? ""
        ).trim();
        if (!deleteReason) return; // cancelled
        if (deleteReason.length < 6) {
          setError("Session delete needs a reason of at least 6 characters — nothing was deleted.");
          return;
        }
        setDeletingSessionId(targetSessionId);
        try {
          const response = await deleteSession(targetSessionId, selectedProjectRoot, deleteReason);
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
    // Only the in-dashboard CONDUCTOR (the chat that drives cloud agents) is CloudAgent-
    // gated in web. Agents config, plans, lanes, execution, sessions, skills, config are
    // all WebAgent data/views and stay available (the connected client IS the agent).
    if (activeNav === "conductor" && webmcpScope === "web") {
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
              // #885: the verdict comes from audit_deletion_law and is a
              // REFUSAL whenever the dashboard has no authenticated admin
              // operator. Announcing "cleared" regardless would tell the
              // operator their audit trail was erased when it was not.
              try {
                const verdict = await executionClearToolCalls(undefined, scopedSessionId);
                if (verdict?.ok) {
                  setNotice(scopedSessionId ? `Tool calls cleared for session ${scopedSessionId}.` : "All tool calls cleared.");
                } else {
                  setError(verdict?.error || `Refused (${verdict?.blocked_by || "unknown"}). Deleting audit evidence needs an authenticated admin operator — use the governed execution_clear_tool_calls tool.`);
                }
                refresh();
              } catch (e) { setError(String(e)); }
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
              // #885: the counter is reset by a watermark; nothing is deleted, so the
              // notice must not say "cleared" — the events are still in the ledger.
              try { await executionClearTokens(root, scopedSessionId); setNotice(scopedSessionId ? `Token counter reset for session ${scopedSessionId} (audit events kept).` : "Token counter reset (audit events kept)."); refresh(); } catch (e) { setError(String(e)); }
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
          judgeOverrides={snapshot.config?.judge_overrides ?? null}
          onSaveJudgeOverrideFamily={(family, ruleIds) =>
            saveJudgeOverrideFamily(family, ruleIds, settingsScope)
          }
        />
      );
    }
    if (activeNav === "registry") {
      return <RegistryPage results={registryResults} query={registryQuery} setQuery={setRegistryQuery} nextCursor={registryCursor} loading={registryLoading} onSearch={() => void searchRegistry()} onMore={() => void searchRegistry(registryCursor ?? undefined)} projectRoot={selectedProjectRoot} onNotice={setNotice} onError={setError} />;
    }
    if (activeNav === "monitoring") {
      return <MonitoringPage snapshot={snapshot} />;
    }
    if (activeNav === "updates") {
      return <UpdatesPage />;
    }
    if (activeNav === "memory") {
      return <MemoryKgPage projectRoot={selectedProjectRoot ?? snapshot?.project.project_root ?? null} />;
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
    if (!isWebBuild() && !loading && projects.length === 0 && !snapshot && isConnected()) {  // desktop-only, POST-login
      setShowWizard(true);
    }
  }, [loading, projects, snapshot]);

  if (showWizard && !isWebBuild() && isConnected()) {
    return (
      <Suspense fallback={<LoadingOverlay />}>
        <SetupWizardPage onComplete={() => { setShowWizard(false); refresh(); }} />
      </Suspense>
    );
  }

  // Web build: a signed-OUT user lands on the dedicated /login page — NOT the full
  // shell (which would show a blank "No projects available" + an overflowing header
  // connection-panel). Re-evaluated on connection change via the onScopeChange effect
  // above (saveGateConnection/logout dispatch the scope event). The gate serves the SPA
  // shell at /login (classify + SPA fallback), so a reload/deep-link of /login works.
  // Login gate (ALL flavours — universal-login Phase 1): a signed-out operator
  // lands on the sign-in card before the shell OR the setup wizard (the wizard
  // blocks above are now gated on isConnected, so login effectively precedes
  // them). Web rewrites the path so a reload/deep-link of /login works; desktop
  // just renders the card and beginLogin() runs the loopback flow.
  // Desktop (Tauri): gate the shell on ACTUAL operator token validity — no more
  // fake-connected (Empire directive 2026-07-17). While the first status check is
  // in flight, hold on the loading overlay; a signed-out operator sees the real
  // email/password login. Signing in re-checks + refreshes the shell.
  if (!isWebBuild()) {
    if (desktopAuthed === null) {
      return <LoadingOverlay />;
    }
    if (!desktopAuthed) {
      return (
        <DesktopLoginView
          projectRoot={selectedProjectRoot ?? undefined}
          onAuthenticated={() => {
            setDesktopAuthed(true);
            void checkDesktopAuth();
            refresh();
          }}
        />
      );
    }
  }
  // #471 + Empire ruling 2026-07-19: LOGIN IS A WALL — no one uses AIDOCS
  // without logging in at least once (#404). What local mode relaxes is only
  // the EXPIRY churn (#92: renew, never re-wall): a STORED login (even
  // expired) satisfies the desktop-local gate; a fresh install with no login
  // record ever still hits the wall. Web build and cloud modes require a
  // LIVE connection as before.
  const liveGateRequired = isWebBuild() || getMode() !== "local";
  const loginSatisfied = liveGateRequired ? isConnected() : hasStoredLogin();
  // Never wall a session that is still being RENEWED (#92): drawing the sign-in
  // card while a refresh is in flight is precisely the paint-then-withdraw the
  // renewal seam exists to stop.
  if (!loginSatisfied && !renewChecked && hasStoredLogin()) {
    return <LoadingOverlay />;
  }
  if (!loginSatisfied) {
    if (isWebBuild() && typeof window !== "undefined" && window.location.pathname !== "/login") {
      window.history.replaceState(null, "", "/login");
    }
    return <LoginPage />;
  }

  // WebAgent (cloud) scope renders the SAME full shell — the snapshot is just
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
        sessionLabel={selectedSession?.overview?.title ?? (sessionValue === "__all__" ? ((snapshot?.sessions?.length ?? 0) === 0 ? "No agents connected to a project" : "All sessions") : sessionValue || undefined)}
        onCommandPalette={() => setPaletteOpen(true)}
        onRefresh={refresh}
        contextTitle={
          selectedSettingPath && activeNav === "settings"
            ? "Setting Detail"
            : undefined
        }
        contextRail={
          !isWebBuild() && desktopAuthed ? (
            // The "Auto-bind new local sessions while logged in" checkbox lived
            // here until 2026-08-27, when the operator ruled it out of
            // existence (#559: "no, remove it, no auto-bind"). It wrote
            // `dashboard.auto_bind_local_sessions`, which no backend code ever
            // read — the control promised a standing grant that nothing
            // implemented. Bindings are approved one at a time, by a human,
            // through the panel below.
            <BindingsPanel
              bindings={bindings}
              projectRoot={selectedProjectRoot ?? undefined}
              onChanged={refresh}
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
          <>
          {/* Backlog #21 — always-visible sticky-perms indicator: a
              warning badge whenever the selected session holds active
              sticky grants; hidden when none are active. */}
          <StickyGrantsIndicator sticky={snapshot?.sticky_grants ?? null} />
          <button
            type="button"
            className="grid h-10 w-10 place-items-center rounded-xl border border-castle-line bg-white/[0.035] text-castle-mute hover:bg-white/[0.07] hover:text-slate-200"
            title="Add project"
            onClick={async () => {
              try {
                if (isWebBuild()) {
                    const url = window.prompt("Add a project from a GitHub URL — the gate imports it into your org:", "https://github.com/owner/repo");
                    if (!url || !url.trim()) return;
                    setNotice("Importing project from GitHub…");
                    const r = await invoke<Record<string, unknown>>("project_register_from_github_url", { url: url.trim() });
                    if (r && r["_error"]) { setError(String(r["_detail"] || r["_error"])); return; }
                    setNotice("Project imported: " + String(r?.["name"] ?? url.trim()));
                      reloadProjects();
                    return;
                  }
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
          </>
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
        {/* #237 fix: the memory-anchor health stats used to render HERE —
            globally, under every page, as raw text that broke layouts.
            They now live where they belong: the Memory page. */}
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
        <UpgradeModalHost />
      </>
  );
}

export default App;
