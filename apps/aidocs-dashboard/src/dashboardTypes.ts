import type {
  DashboardConfigEntry,
  DashboardSeriesItem,
  DashboardSnapshot,
  DashboardTomlDocument,
} from "./dashboardApi";
import type { SettingsScope, TomlCategory } from "./dashboardUtils";

export type SelectedSession = DashboardSnapshot["selected_session"];
export type ConductorLane = { lane_id: string; name: string; depends_on?: string[] };

export type OverviewPageProps = {
  snapshot: DashboardSnapshot;
  selectedSession: SelectedSession;
  contextBudget: import("./dashboardApi").ContextBudgetResult | null;
  compactingContext: boolean;
  onCompactContext: () => void;
};

export type SessionsPageProps = {
  sessions: DashboardSnapshot["sessions"];
  connectedAgents?: DashboardSnapshot["connected_agents"];
  sessionValue: string;
  onSelectSession: (sessionId: string) => void;
  onDeleteSession: (sessionId: string) => void;
  deletingSessionId?: string | null;
  // Session create / connect actions. Each returns the raw control-plane
  // result (carrying owner_grant / ownership_degraded / blocked_by) so the
  // page renders the shared authority notice. Optional in read-only contexts.
  onCreateSession?: (title: string) => Promise<Record<string, unknown>>;
  onConnectSession?: (sessionId: string) => Promise<Record<string, unknown>>;
  // SEC-005 (2026-04-23): degraded_state for the CURRENTLY-SELECTED
  // session. null/undefined → no selected session or not degraded.
  // The panel marks the currently-selected row with a red chip when
  // set.
  degradedState?: DashboardSnapshot["degraded_state"] | null;
  // Gate liveness for the whole install (hooks firing / declining, NLP alive).
  // undefined/null → the card renders UNKNOWN (amber), never green: a health
  // badge that cannot check must never claim a pass.
  gateHealth?: DashboardSnapshot["gate_health"] | null;
};

export type ConductorPageProps = {
  progressPercent: number;
  conductorLanes: ConductorLane[];
  runnableLaneIds: string[];
  blockedReasons: Record<string, string[]>;
  recentExecution: DashboardSnapshot["execution"]["recent"];
  configEntries?: DashboardConfigEntry[];
  selectedSessionId: string | null;
  projectRoot: string | null;
  sessionId: string | null;
};
export type ConductorAgentsPageProps = {
  configEntries?: DashboardConfigEntry[];
  settingsScope: SettingsScope;
  setSettingsScope: (scope: SettingsScope) => void;
  hasProject: boolean;
  hasSession: boolean;
  requestConfigSave: (entry: DashboardConfigEntry, scope: string, value: string) => void;
  savingSetting: string | null;
};

export type ExecutionPageProps = {
  recentExecution: Array<{
    event_id: string;
    run_id?: string | null;
    observed_at: string;
    event_kind: string;
    source_kind?: string | null;
    session_id?: string | null;
    procedure_id?: string | null;
    action_kind: string | null;
    capability_name: string | null;
    target_entity?: string | null;
    status: string | null;
    payload?: Record<string, unknown>;
    }>;
    onClearToolCalls: () => void;
    clearing: boolean;
};

export type UsagePageProps = {
  tokenEstimates: { tokens_in: number; tokens_out: number; tokens_in_calls?: number; tokens_out_calls?: number; total: number };
  sessionBreakdown: Array<{ session_id: string; tokens_in: number; tokens_out: number; total: number; events: number }>;
  eventRows: Array<DashboardSeriesItem & { width: string }>;
  intentRows: Array<DashboardSeriesItem & { width: string }>;
  onClearTokens?: () => void;
  clearingTokens?: boolean;
};

export type SettingsPageProps = {
  settingsScope: SettingsScope;
  setSettingsScope: (scope: SettingsScope) => void;
  hasProject: boolean;
  hasSession: boolean;
  configSections: Array<{ section: string; entries: DashboardConfigEntry[] }>;
  bashPolicy?: import("./dashboardApi").BashPolicySnapshot | null;
  saveBashCommandState?: (
    cmd: string,
    layer: "global" | "project" | "session",
    nextState: "allow" | "deny" | "bubble",
  ) => void;
  draftValues: Record<string, string>;
  savingSetting: string | null;
  requestConfigSave: (entry: DashboardConfigEntry, scope: string, value: string) => void;
  requestConfigBatchSave: (entries: Array<{ entry: DashboardConfigEntry; value: string }>, scope: string) => void;
  setDraftValue: (path: string, value: string) => void;
  openImportExport: () => void;
  /** Phase 6g: when set, the parent renders SettingDetailPanel for
   * this path into the shell's contextRail. */
  selectedPath?: string | null;
  /** Phase 6g: callback when the operator picks an entry. */
  onEntrySelect?: (path: string | null) => void;
};

export type TomlConfigsPageProps = {
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

export type ImportExportModalProps = {
  selectedConfigTextDocument: DashboardTomlDocument | null;
  close: () => void;
  savingTomlPath: string | null;
  tomlDrafts: Record<string, string>;
  setTomlDraft: (path: string, value: string) => void;
  handleConfigTextSave: () => void;
  scopeLabel: string;
};

export type DangerConfirmModalProps = {
  settingPath: string;
  close: () => void;
  // Receives the operator's typed reason — required for a dashboard-only (T0)
  // guardrail change, carried into the audit (--reason).
  confirm: (reason: string) => void;
};

export type ToastStackProps = {
  notice: string | null;
  error: string | null;
  clearNotice: () => void;
  clearError: () => void;
};
