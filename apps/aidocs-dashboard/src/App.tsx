import { useEffect, useMemo, useState } from "react";
import logoUrl from "./cn-logo.svg";
import {
  loadDashboard,
  loadManagedProjects,
  loadTomlDocuments,
  saveConfigSetting,
  saveTomlDocument,
  type DashboardConfigEntry,
  type DashboardManagedProject,
  type DashboardSnapshot,
  type DashboardTomlDocument,
} from "./dashboardApi";
import {
  ConductorPage,
  DangerConfirmModal,
  ExecutionPage,
  ImportExportModal,
  LoadingOverlay,
  OverviewPage,
  SessionsPage,
  SettingsPage,
  ToastStack,
  UsagePage,
} from "./dashboardPages";
import {
  asText,
  navigation,
  parseEntryValue,
  parseProgressPercent,
  readAidocsVersion,
  scaleRows,
  type DropdownOption,
  type NavKey,
  type SettingsView,
} from "./dashboardUtils";

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
  const [snapshot, setSnapshot] = useState<DashboardSnapshot | null>(null);
  const [projects, setProjects] = useState<DashboardManagedProject[]>([]);
  const [tomlDocuments, setTomlDocuments] = useState<DashboardTomlDocument[]>([]);
  const [selectedProjectRoot, setSelectedProjectRoot] = useState<string | undefined>(undefined);
  const [selectedSessionId, setSelectedSessionId] = useState<string | undefined>(undefined);
  const [selectedTomlPath, setSelectedTomlPath] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [draftValues, setDraftValues] = useState<Record<string, string>>({});
  const [tomlDrafts, setTomlDrafts] = useState<Record<string, string>>({});
  const [savingSetting, setSavingSetting] = useState<string | null>(null);
  const [savingTomlPath, setSavingTomlPath] = useState<string | null>(null);
  const [refreshToken, setRefreshToken] = useState(0);
  const [settingsView, setSettingsView] = useState<SettingsView>("typed");
  const [tomlPage, setTomlPage] = useState(1);
  const [tomlPageSize, setTomlPageSize] = useState(10);
  const [importExportOpen, setImportExportOpen] = useState(false);
  const [configTextPath, setConfigTextPath] = useState<string | null>(null);
  const [pendingDangerSettingPath, setPendingDangerSettingPath] = useState<string | null>(null);
  const [openDropdown, setOpenDropdown] = useState<"project" | "session" | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    Promise.all([
      loadManagedProjects(selectedProjectRoot),
      loadDashboard(selectedProjectRoot, selectedSessionId),
      loadTomlDocuments(selectedProjectRoot, selectedSessionId),
    ])
      .then(([projectItems, data, documents]) => {
        if (cancelled) {
          return;
        }

        setProjects(projectItems);
        setSnapshot(data);
        setTomlDocuments(documents);
        setSelectedProjectRoot((current) => current ?? data.project.project_root);
        setSelectedSessionId((current) => {
          const next = current && data.sessions.some((session) => session.session_id === current)
            ? current
            : data.selected_session_id ?? undefined;
          return current === next ? current : next;
        });
        setDraftValues(Object.fromEntries(data.config.entries.map((entry) => [entry.path, asText(entry.current_value)])));
        setTomlDrafts(Object.fromEntries(documents.map((document) => [document.path, document.content])));
        setSelectedTomlPath((current) =>
          current && documents.some((document) => document.path === current)
            ? current
            : documents[0]?.path ?? null,
        );
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : String(err));
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [refreshToken, selectedProjectRoot, selectedSessionId]);

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
      if (entry.path === "global.aidocs_core_version" || entry.path === "dev.dev_mode") {
        continue;
      }
      const section = entry.section || "misc";
      groups.set(section, [...(groups.get(section) ?? []), entry]);
    }
    return Array.from(groups.entries()).map(([section, entries]) => ({ section, entries }));
  }, [snapshot]);
  const devModeEntry = useMemo(
    () => snapshot?.config.entries.find((entry) => entry.path === "dev.dev_mode") ?? null,
    [snapshot],
  );
  const selectedTomlDocument = tomlDocuments.find((document) => document.path === selectedTomlPath) ?? null;
  const conductorLanes = selectedSession?.conductor?.graph?.lanes ?? [];
  const runnableLaneIds = selectedSession?.conductor?.runnable?.runnable_lane_ids ?? [];
  const blockedReasons = selectedSession?.conductor?.runnable?.blocked_reasons ?? {};
  const progressPercent = parseProgressPercent(selectedSession?.plan_overview.progress);
  const capabilityRows = useMemo(() => scaleRows(snapshot?.token_usage.proxy_series.top_capabilities ?? []), [snapshot]);
  const actionRows = useMemo(() => scaleRows(snapshot?.token_usage.proxy_series.top_action_kinds ?? []), [snapshot]);
  const eventRows = useMemo(() => scaleRows(snapshot?.token_usage.proxy_series.event_breakdown ?? []), [snapshot]);
  const projectValue = selectedProjectRoot ?? snapshot?.project.project_root ?? projects[0]?.project_root ?? "";
  const sessionValue = selectedSessionId ?? snapshot?.selected_session_id ?? "";
  const aidocsVersion = useMemo(() => readAidocsVersion(snapshot), [snapshot]);
  const configTextDocuments = useMemo(
    () => tomlDocuments.filter((document) => document.path === "aidocs.toml" || document.path.endsWith("/aidocs.toml")),
    [tomlDocuments],
  );
  const totalTomlPages = Math.max(1, Math.ceil(tomlDocuments.length / tomlPageSize));
  const paginatedTomlDocuments = useMemo(
    () => tomlDocuments.slice((tomlPage - 1) * tomlPageSize, tomlPage * tomlPageSize),
    [tomlDocuments, tomlPage, tomlPageSize],
  );
  const selectedConfigTextDocument = configTextDocuments.find((document) => document.path === configTextPath) ?? configTextDocuments[0] ?? null;
  const overviewStats = useMemo(() => {
    if (!snapshot) {
      return [];
    }
    return [
      { label: "Projects", value: String(projects.length || 1), note: "known to this dashboard" },
      { label: "Sessions", value: String(snapshot.sessions.length), note: "inside current project" },
      { label: "Events", value: String(snapshot.execution.summary.total_events), note: "runtime evidence" },
      { label: "Modes", value: String(snapshot.config.available_edit_modes.length), note: "editable surfaces" },
    ];
  }, [projects, snapshot]);
  const projectOptions = useMemo<DropdownOption[]>(
    () => projects.map((project) => ({ value: project.project_root, label: project.title, subtitle: `${project.session_count} sessions` })),
    [projects],
  );
  const sessionOptions = useMemo<DropdownOption[]>(
    () => [
      { value: "", label: "Automatic / managed selection" },
      ...(snapshot?.sessions ?? []).map((session) => ({
        value: session.session_id,
        label: session.title ?? session.session_id,
        subtitle: session.status ?? undefined,
      })),
    ],
    [snapshot],
  );

  async function handleConfigSave(entry: DashboardConfigEntry) {
    const rawValue = draftValues[entry.path] ?? asText(entry.current_value);
    setSavingSetting(entry.path);
    setNotice(null);
    setError(null);
    try {
      const response = await saveConfigSetting(entry.path, parseEntryValue(entry, rawValue), selectedProjectRoot);
      setSnapshot(response.snapshot);
      setDraftValues(Object.fromEntries(response.snapshot.config.entries.map((item) => [item.path, asText(item.current_value)])));
      setNotice(response.message);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSavingSetting(null);
    }
  }

  function requestConfigSave(entry: DashboardConfigEntry) {
    if (entry.path === "dev.dev_mode") {
      setPendingDangerSettingPath(entry.path);
      return;
    }
    void handleConfigSave(entry);
  }

  async function handleTomlSave() {
    if (!selectedTomlDocument) {
      return;
    }
    setSavingTomlPath(selectedTomlDocument.path);
    setNotice(null);
    setError(null);
    try {
      const response = await saveTomlDocument(
        selectedTomlDocument.path,
        tomlDrafts[selectedTomlDocument.path] ?? selectedTomlDocument.content,
        selectedSessionId,
        selectedProjectRoot,
      );
      setTomlDocuments(response.documents);
      setTomlDrafts(Object.fromEntries(response.documents.map((document) => [document.path, document.content])));
      setNotice(response.message);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSavingTomlPath(null);
    }
  }

  async function handleConfigTextSave() {
    if (!selectedConfigTextDocument) {
      return;
    }
    setSavingTomlPath(selectedConfigTextDocument.path);
    setNotice(null);
    setError(null);
    try {
      const response = await saveTomlDocument(
        selectedConfigTextDocument.path,
        tomlDrafts[selectedConfigTextDocument.path] ?? selectedConfigTextDocument.content,
        selectedSessionId,
        selectedProjectRoot,
      );
      setTomlDocuments(response.documents);
      setTomlDrafts(Object.fromEntries(response.documents.map((document) => [document.path, document.content])));
      setNotice(response.message);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSavingTomlPath(null);
    }
  }

  function handleProjectChange(projectRoot: string) {
    if (!projectRoot || projectRoot === projectValue) {
      return;
    }
    setSelectedProjectRoot(projectRoot);
    setSelectedSessionId(undefined);
    setSelectedTomlPath(null);
    setNotice(null);
    setError(null);
  }

  function setDraftValue(path: string, value: string) {
    setDraftValues((current) => ({ ...current, [path]: value }));
  }

  function setTomlDraft(path: string, value: string) {
    setTomlDrafts((current) => ({ ...current, [path]: value }));
  }

  useEffect(() => {
    setTomlPage((current) => Math.min(current, Math.max(1, Math.ceil(tomlDocuments.length / tomlPageSize))));
  }, [tomlDocuments.length, tomlPageSize]);

  useEffect(() => {
    const updateTomlPageSize = () => {
      const viewportHeight = window.innerHeight;
      const estimatedRows = Math.floor((viewportHeight - 240) / 44);
      setTomlPageSize(Math.max(10, Math.min(28, estimatedRows)));
    };
    updateTomlPageSize();
    window.addEventListener("resize", updateTomlPageSize);
    return () => window.removeEventListener("resize", updateTomlPageSize);
  }, []);

  useEffect(() => {
    if (!importExportOpen) {
      return;
    }
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setImportExportOpen(false);
      }
    };
    window.addEventListener("keydown", handleEscape);
    return () => window.removeEventListener("keydown", handleEscape);
  }, [importExportOpen]);

  useEffect(() => {
    const handlePointerDown = (event: MouseEvent) => {
      const target = event.target;
      if (!(target instanceof Element)) {
        return;
      }
      if (!target.closest(".dropdown-field")) {
        setOpenDropdown(null);
      }
    };
    document.addEventListener("mousedown", handlePointerDown);
    return () => document.removeEventListener("mousedown", handlePointerDown);
  }, []);

  useEffect(() => {
    if (!pendingDangerSettingPath) {
      return;
    }
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setPendingDangerSettingPath(null);
      }
    };
    window.addEventListener("keydown", handleEscape);
    return () => window.removeEventListener("keydown", handleEscape);
  }, [pendingDangerSettingPath]);

  useEffect(() => {
    if (!paginatedTomlDocuments.length) {
      return;
    }
    setSelectedTomlPath((current) =>
      current && paginatedTomlDocuments.some((document) => document.path === current)
        ? current
        : paginatedTomlDocuments[0].path,
    );
  }, [paginatedTomlDocuments]);

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

  function renderActivePage() {
    if (!snapshot) {
      return null;
    }
    if (activeNav === "overview") {
      return <OverviewPage snapshot={snapshot} selectedSession={selectedSession} overviewStats={overviewStats} />;
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
        />
      );
    }
    if (activeNav === "execution") {
      return <ExecutionPage recentExecution={snapshot.execution.recent} />;
    }
    if (activeNav === "usage") {
      return (
        <UsagePage
          reason={snapshot.token_usage.reason}
          capabilityRows={capabilityRows}
          actionRows={actionRows}
          eventRows={eventRows}
        />
      );
    }
    return (
      <SettingsPage
        settingsView={settingsView}
        setSettingsView={setSettingsView}
        configSections={configSections}
        draftValues={draftValues}
        savingSetting={savingSetting}
        requestConfigSave={requestConfigSave}
        setDraftValue={setDraftValue}
        devModeEntry={devModeEntry}
        openImportExport={() => setImportExportOpen(true)}
        paginatedTomlDocuments={paginatedTomlDocuments}
        selectedTomlPath={selectedTomlPath}
        setSelectedTomlPath={(path) => setSelectedTomlPath(path)}
        tomlPage={tomlPage}
        totalTomlPages={totalTomlPages}
        setTomlPage={setTomlPage}
        selectedTomlDocument={selectedTomlDocument}
        tomlDrafts={tomlDrafts}
        setTomlDraft={setTomlDraft}
        savingTomlPath={savingTomlPath}
        handleTomlSave={() => void handleTomlSave()}
      />
    );
  }

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand-block">
          <img className="brand-logo" src={logoUrl} alt="CodeNexus" />
          <div className="brand-text">
            <h1>AIDOCS Dashboard</h1>
            <span className="brand-subtitle">CodeNexus operator app</span>
          </div>
        </div>

        <section className="sidebar-section">
          <div className="section-label">Known Projects</div>
          <div className="project-list">
            {projects.map((project) => (
              <button
                key={project.project_root}
                className={project.project_root === projectValue ? "project-row is-selected" : "project-row"}
                type="button"
                onClick={() => handleProjectChange(project.project_root)}
              >
                <span className="project-row-title">{project.title}</span>
                <span className="project-row-meta">{project.session_count} sessions</span>
              </button>
            ))}
          </div>
        </section>

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
                  setSelectedSessionId(value || undefined);
                  setOpenDropdown(null);
                }}
              />
              <button className="action-button" type="button" onClick={() => setRefreshToken((value) => value + 1)}>
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
          />
        ) : null}

        {pendingDangerSettingPath && devModeEntry ? (
          <DangerConfirmModal
            close={() => setPendingDangerSettingPath(null)}
            confirm={() => {
              setPendingDangerSettingPath(null);
              void handleConfigSave(devModeEntry);
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
