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
  type DashboardSeriesItem,
  type DashboardSnapshot,
  type DashboardTomlDocument,
} from "./dashboardApi";

type NavKey = "overview" | "sessions" | "conductor" | "execution" | "settings" | "usage";
type SettingsView = "typed" | "documents";

const navigation: Array<{ name: string; value: NavKey }> = [
  { name: "Overview", value: "overview" },
  { name: "Sessions", value: "sessions" },
  { name: "Conductor", value: "conductor" },
  { name: "Execution", value: "execution" },
  { name: "Settings", value: "settings" },
  { name: "Usage", value: "usage" },
];

function asText(value: unknown): string {
  if (Array.isArray(value)) {
    return value.join(", ");
  }
  if (typeof value === "boolean") {
    return value ? "true" : "false";
  }
  if (value && typeof value === "object") {
    return JSON.stringify(value);
  }
  return String(value ?? "");
}

function parseEntryValue(entry: DashboardConfigEntry, rawValue: string): unknown {
  if (entry.type === "integer") {
    return Number.parseInt(rawValue, 10);
  }
  if (entry.type === "boolean") {
    return rawValue === "true";
  }
  if (entry.type === "string_list") {
    return rawValue
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);
  }
  return rawValue;
}

function scaleRows(items: DashboardSeriesItem[]) {
  const max = Math.max(...items.map((item) => item.count), 1);
  return items.map((item) => ({
    ...item,
    width: `${Math.max((item.count / max) * 100, 8)}%`,
  }));
}

function formatTimestamp(value: string | null | undefined): string {
  if (!value) {
    return "n/a";
  }
  return value.replace("T", " ");
}

function parseProgressPercent(progress: string | null | undefined): number {
  const match = progress?.match(/(\d+)/);
  if (!match) {
    return 0;
  }
  const value = Number.parseInt(match[1], 10);
  return Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : 0;
}

function describeSettingValue(entry: DashboardConfigEntry, value: string): string | null {
  if (entry.value_descriptions[value]) {
    return entry.value_descriptions[value];
  }
  if (entry.type === "boolean") {
    return value === "true" ? "Enabled." : "Disabled.";
  }
  return null;
}

function buildSettingTooltip(entry: DashboardConfigEntry, value: string): string {
  const lines = [entry.description];
  const currentMeaning = describeSettingValue(entry, value);
  if (currentMeaning) {
    lines.push(`Current: ${value} - ${currentMeaning}`);
  } else {
    lines.push(`Current: ${value}`);
  }
  lines.push(`Default: ${asText(entry.default)}`);
  if (entry.allowed_scopes.length) {
    lines.push(`Scopes: ${entry.allowed_scopes.join(", ")}`);
  }
  if (entry.security_sensitive) {
    lines.push("Security-sensitive setting.");
  }
  if (entry.allowed_values?.length) {
    lines.push("Options:");
    for (const option of entry.allowed_values) {
      lines.push(`- ${option}: ${entry.value_descriptions[option] ?? option}`);
    }
  }
  return lines.join("\n");
}

function isDashboardEditable(entry: DashboardConfigEntry): boolean {
  return entry.editable || entry.path === "dev.dev_mode";
}

function readAidocsVersion(snapshot: DashboardSnapshot | null): string {
  const entry = snapshot?.config.entries.find((item) => item.path === "global.aidocs_core_version");
  if (entry) {
    return String(entry.current_value ?? entry.default ?? "unknown");
  }
  return "unknown";
}

function isDocumentActive(document: DashboardTomlDocument): boolean {
  return !/not selected|inactive|disabled/i.test(document.active);
}

type DropdownOption = {
  value: string;
  label: string;
  subtitle?: string;
};

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
        setDraftValues(
          Object.fromEntries(data.config.entries.map((entry) => [entry.path, asText(entry.current_value)])),
        );
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
    [tomlDocuments, tomlPage],
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
      setDraftValues(
        Object.fromEntries(response.snapshot.config.entries.map((item) => [item.path, asText(item.current_value)])),
      );
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

        <div className="content-body">
        {snapshot ? (
          <>
            {activeNav === "overview" ? (
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
            ) : null}

            {activeNav === "sessions" ? (
              <section className="page">
                <div className="flat-table">
                  <div className="table-head session-table-row" aria-hidden="true">
                    <span>Session</span>
                    <span>Status</span>
                    <span>Owner</span>
                    <span>Updated</span>
                  </div>
                  {(snapshot.sessions ?? []).map((session) => (
                    <button
                      key={session.session_id}
                      className={session.session_id === sessionValue ? "table-row session-table-row is-selected" : "table-row session-table-row"}
                      type="button"
                      onClick={() => setSelectedSessionId(session.session_id)}
                    >
                      <span>{session.title ?? session.session_id}</span>
                      <span>{session.status ?? "unknown"}</span>
                      <span>{session.owner ?? "unowned"}</span>
                      <span>{session.last_updated ?? "n/a"}</span>
                    </button>
                  ))}
                </div>
              </section>
            ) : null}

            {activeNav === "conductor" ? (
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
            ) : null}

            {activeNav === "execution" ? (
              <section className="page">
                <div className="flat-table">
                  <div className="table-head execution-table-wide-row" aria-hidden="true">
                    <span>Capability</span>
                    <span>Action</span>
                    <span>Status</span>
                    <span>Observed</span>
                  </div>
                  {snapshot.execution.recent.map((event) => (
                    <div key={event.event_id} className="feed-row feed-row-wide execution-table-wide-row">
                      <strong>{event.capability_name ?? event.event_kind}</strong>
                      <span>{event.action_kind ?? event.event_kind}</span>
                      <span>{event.status ?? "unknown"}</span>
                      <time>{formatTimestamp(event.observed_at)}</time>
                    </div>
                  ))}
                </div>
              </section>
            ) : null}

            {activeNav === "usage" ? (
              <section className="page">
                <div className="usage-grid">
                  <section className="flat-panel">
                    <div className="section-label">Capabilities</div>
                    <div className="metric-list">
                      {capabilityRows.map((item) => (
                        <div key={item.label} className="metric-row">
                          <span className="metric-label" title={item.label}>{item.label}</span>
                          <div className="metric-track"><div className="metric-fill" style={{ width: item.width }} /></div>
                          <strong>{item.count}</strong>
                        </div>
                      ))}
                    </div>
                  </section>
                  <section className="flat-panel">
                    <div className="section-label">Action kinds</div>
                    <div className="metric-list">
                      {actionRows.map((item) => (
                        <div key={item.label} className="metric-row">
                          <span className="metric-label" title={item.label}>{item.label}</span>
                          <div className="metric-track"><div className="metric-fill" style={{ width: item.width }} /></div>
                          <strong>{item.count}</strong>
                        </div>
                      ))}
                    </div>
                  </section>
                  <section className="flat-panel">
                    <div className="section-label">Event kinds</div>
                    <div className="metric-list">
                      {eventRows.map((item) => (
                        <div key={item.label} className="metric-row">
                          <span className="metric-label" title={item.label}>{item.label}</span>
                          <div className="metric-track"><div className="metric-fill" style={{ width: item.width }} /></div>
                          <strong>{item.count}</strong>
                        </div>
                      ))}
                    </div>
                    <p className="panel-copy usage-note">{snapshot.token_usage.reason}</p>
                  </section>
                </div>
              </section>
            ) : null}

            {activeNav === "settings" ? (
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
                                    {entry.allowed_values?.length ? (
                                      <select
                                        value={currentValue}
                                      disabled={!isDashboardEditable(entry) || savingSetting === entry.path}
                                      title={settingTooltip}
                                        onChange={(event) =>
                                          setDraftValues((current) => ({
                                            ...current,
                                            [entry.path]: event.target.value,
                                          }))
                                        }
                                      >
                                        {entry.allowed_values.map((value) => (
                                          <option key={value} value={value}>
                                            {value}
                                          </option>
                                        ))}
                                      </select>
                                    ) : entry.type === "boolean" ? (
                                      <select
                                        value={currentValue}
                                      disabled={!isDashboardEditable(entry) || savingSetting === entry.path}
                                      title={settingTooltip}
                                        onChange={(event) =>
                                          setDraftValues((current) => ({
                                            ...current,
                                            [entry.path]: event.target.value,
                                          }))
                                        }
                                      >
                                        <option value="true">true</option>
                                        <option value="false">false</option>
                                      </select>
                                    ) : (
                                      <input
                                        value={currentValue}
                                      disabled={!isDashboardEditable(entry) || savingSetting === entry.path}
                                      title={settingTooltip}
                                        onChange={(event) =>
                                          setDraftValues((current) => ({
                                            ...current,
                                            [entry.path]: event.target.value,
                                          }))
                                        }
                                      />
                                    )}
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
                                <span className="setting-info" title={buildSettingTooltip(devModeEntry, draftValues[devModeEntry.path] ?? asText(devModeEntry.current_value))} aria-label={buildSettingTooltip(devModeEntry, draftValues[devModeEntry.path] ?? asText(devModeEntry.current_value))}>
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
                                onChange={(event) =>
                                  setDraftValues((current) => ({
                                    ...current,
                                    [devModeEntry.path]: event.target.value,
                                  }))
                                }
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
                        <button
                          type="button"
                          className="action-button config-import-export"
                          onClick={() => setImportExportOpen(true)}
                        >
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
                              onChange={(event) =>
                                setTomlDrafts((current) => ({
                                  ...current,
                                  [selectedTomlDocument.path]: event.target.value,
                                }))
                              }
                            />
                            <div className="editor-actions">
                              <button
                                className="action-button"
                                type="button"
                                disabled={!selectedTomlDocument.editable || savingTomlPath === selectedTomlDocument.path}
                                onClick={() => handleTomlSave()}
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
            ) : null}
          </>
        ) : null}
        </div>

        {importExportOpen && selectedConfigTextDocument ? (
          <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Import or export AIDOCS config" onClick={() => setImportExportOpen(false)}>
            <div className="modal-panel">
              <div className="page-header modal-header" onClick={(event) => event.stopPropagation()}>
                <div>
                  <div className="section-label">Import / Export config</div>
                  <h3>{selectedConfigTextDocument.target}</h3>
                </div>
                <button className="action-button action-button-small modal-close" type="button" onClick={() => setImportExportOpen(false)}>
                  Close
                </button>
              </div>
              <div className="config-text-toolbar" onClick={(event) => event.stopPropagation()}>
                <label className="field-control">
                  <span>Source</span>
                  <select value={selectedConfigTextDocument.path} onChange={(event) => setConfigTextPath(event.target.value)}>
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
                  onClick={() => handleConfigTextSave()}
                >
                  {savingTomlPath === selectedConfigTextDocument.path ? "Saving..." : "Save"}
                </button>
              </div>
              <textarea
                className="toml-editor modal-editor"
                onClick={(event) => event.stopPropagation()}
                value={tomlDrafts[selectedConfigTextDocument.path] ?? selectedConfigTextDocument.content}
                onChange={(event) =>
                  setTomlDrafts((current) => ({
                    ...current,
                    [selectedConfigTextDocument.path]: event.target.value,
                  }))
                }
              />
            </div>
          </div>
        ) : null}

        {pendingDangerSettingPath && devModeEntry ? (
          <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Confirm dev mode change" onClick={() => setPendingDangerSettingPath(null)}>
            <div className="modal-panel danger-modal" onClick={(event) => event.stopPropagation()}>
              <div className="page-header modal-header">
                <div>
                  <div className="section-label">Confirm change</div>
                  <h3>Update dev_mode</h3>
                </div>
                <button className="action-button action-button-small modal-close" type="button" onClick={() => setPendingDangerSettingPath(null)}>
                  Cancel
                </button>
              </div>
              <p className="warning-text modal-warning-copy">
                This setting allows editing AIDOCS MCP infrastructure source files. Only enable it when you intentionally need to change protected internal code.
              </p>
              <div className="danger-modal-actions">
                <button className="action-button action-button-small" type="button" onClick={() => setPendingDangerSettingPath(null)}>
                  Keep current setting
                </button>
                <button
                  className="action-button action-button-small action-button-danger"
                  type="button"
                  onClick={() => {
                    setPendingDangerSettingPath(null);
                    void handleConfigSave(devModeEntry);
                  }}
                >
                  Confirm change
                </button>
              </div>
            </div>
          </div>
        ) : null}

        {loading ? (
          <div className="app-overlay" role="status" aria-live="polite">
            <div className="app-overlay-panel">
              <div className="overlay-spinner" aria-hidden="true" />
              <strong>Loading runtime snapshot</strong>
              <span>Refreshing project, session, and settings state.</span>
            </div>
          </div>
        ) : null}

        {notice || error ? (
          <div className="toast-stack" aria-live="polite">
            {notice ? (
              <div className="toast toast-success">
                <span>{notice}</span>
                <button type="button" className="toast-close" onClick={() => setNotice(null)}>
                  Close
                </button>
              </div>
            ) : null}
            {error ? (
              <div className="toast toast-error" role="alert">
                <span>{error}</span>
                <button type="button" className="toast-close" onClick={() => setError(null)}>
                  Close
                </button>
              </div>
            ) : null}
          </div>
        ) : null}
      </main>
    </div>
  );
}

export default App;
