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
  sessionBreakdown: DashboardSnapshot["token_usage"]["session_breakdown"];
  intentRows: Array<DashboardSeriesItem & { width: string }>;
  eventRows: Array<DashboardSeriesItem & { width: string }>;
  contextBudget: import("./dashboardApi").ContextBudgetResult | null;
  compactingContext: boolean;
  onCompactContext: () => void;
};

export type SessionsPageProps = {
  sessions: DashboardSnapshot["sessions"];
  sessionValue: string;
  onSelectSession: (sessionId: string) => void;
};

export type ConductorPageProps = {
  selectedSession: SelectedSession;
  progressPercent: number;
  conductorLanes: ConductorLane[];
  runnableLaneIds: string[];
  blockedReasons: Record<string, string[]>;
  recentExecution: DashboardSnapshot["execution"]["recent"];
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
  reason: string;
  tokenEstimates: { tokens_in: number; tokens_out: number; tokens_in_calls?: number; tokens_out_calls?: number; total: number };
  sessionBreakdown: Array<{ session_id: string; tokens_in: number; tokens_out: number; total: number; events: number }>;
  actionRows: Array<DashboardSeriesItem & { width: string }>;
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
  draftValues: Record<string, string>;
  savingSetting: string | null;
  requestConfigSave: (entry: DashboardConfigEntry, scope?: string) => void;
  setDraftValue: (path: string, value: string) => void;
  devModeEntry: DashboardConfigEntry | null;
  openImportExport: () => void;
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
  configTextDocuments: DashboardTomlDocument[];
  selectedConfigTextPath: string;
  setConfigTextPath: (path: string) => void;
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
  confirm: () => void;
};

export type ToastStackProps = {
  notice: string | null;
  error: string | null;
  clearNotice: () => void;
  clearError: () => void;
};
