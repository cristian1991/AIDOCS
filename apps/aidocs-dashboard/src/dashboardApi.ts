import { invoke } from "@tauri-apps/api/core";

export type DashboardSeriesItem = {
  label: string;
  count: number;
  tokens?: number;
};

export type DashboardConfigEntry = {
  path: string;
  section: string;
  key: string;
  type: "integer" | "boolean" | "string" | "string_list";
  description: string;
  default: number | boolean | string | string[];
  allowed_values: string[] | null;
  value_descriptions: Record<string, string>;
  allowed_scopes: string[];
  agent_editable_scopes: string[];
  security_sensitive: boolean;
  requires_restart: boolean;
  editable: boolean;
  current_value: unknown;
  scope_values: Record<string, unknown>;
};

export type DashboardTomlDocument = {
  path: string;
  label: string;
  category: string;
  scope: string;
  target: string;
  active: string;
  language_context: string;
  editable: boolean;
  content: string;
};

export type DashboardManagedProject = {
  title: string;
  project_root: string;
  session_count: number;
  managed_session_id: string | null;
  current: boolean;
};

export type RegistrySearchResult = {
  name: string;
  description?: string | null;
  version?: string | null;
  runtime?: string | null;
  transport?: string | null;
  install_commands?: Array<{ type: string; command: string; transport: string }>;
};

export type MonitoringSnapshot = {
  uptime_seconds: number;
  active_sessions: number;
  tokens_in: Record<string, number>;
  tokens_out: Record<string, number>;
  tool_calls: Record<string, number>;
  guard_scans: Record<string, number>;
  guard_findings: Record<string, number>;
};

export type SkillScanResult = {
  skill: Record<string, unknown>;
  selected: boolean;
  scan: {
    skill_id: string;
    risk_level: string;
    finding_count: number;
    findings: Array<{ category: string; severity: string; description: string; evidence?: string }>;
  };
};

export type ContextBudgetResult = {
  available: boolean;
  reason?: string | null;
  session_id?: string | null;
  journal_entries?: number;
  journal_tokens?: number;
  section_tokens?: number;
  path_tokens?: number;
  execution_tokens?: number;
  estimated_tokens?: number;
  warning?: boolean;
  critical?: boolean;
  status?: "ok" | "warning" | "critical";
};

export type DashboardSnapshot = {
  project: {
    project_name: string;
    project_root: string;
    code_file_count: number;
    module_count: number;
    schema_entity_count: number;
    session_count: number;
    selected_session_id: string | null;
  };
  managed_mode: {
    active?: boolean;
    session_id?: string | null;
  };
  sessions: Array<{
    session_id: string;
    title: string | null;
    status: string | null;
    owner: string | null;
    goal: string | null;
    last_updated: string | null;
    selected: boolean;
    managed: boolean;
  }>;
  selected_session_id: string | null;
  selected_session: {
    overview: {
      title: string | null;
      status: string | null;
      goal: string | null;
      owner: string | null;
      relevant_file_count: number;
      actionable_handoff_step_count: number;
      logging_debt: boolean;
      latest_meaningful_event_at: string | null;
    };
    plan_overview: {
      progress: string;
      next_step: string | null;
      has_lanes: boolean;
      purpose: string | null;
      end_goal: string | null;
    };
    compliance: {
      warnings: string[];
      execution_events: number;
      latest_journal_at: string | null;
    };
    handoff_steps: Array<{ id: string; text: string; status: string }>;
    conductor: {
      graph?: {
        lanes: Array<{ lane_id: string; name: string; depends_on?: string[] }>;
      } | null;
      runnable?: {
        runnable_lane_ids: string[];
        blocked_reasons: Record<string, string[]>;
      } | null;
    } | null;
    conductor_error: string | null;
    session: {
      sections: Record<string, string[]>;
    };
  } | null;
  execution: {
    summary: {
      total_events: number;
      by_action_kind: Record<string, number>;
      by_event_kind: Record<string, number>;
      by_source: Record<string, number>;
    };
    recent: Array<{
        event_id: string;
        observed_at: string;
        event_kind: string;
        action_kind: string | null;
        capability_name: string | null;
        status: string | null;
        payload?: Record<string, unknown>;
      }>;
  };
  token_usage: {
    available: boolean;
    reason: string;
      token_estimates: {
        tokens_in: number;
        tokens_out: number;
        tokens_in_calls?: number;
        tokens_out_calls?: number;
        total: number;
      };
    proxy_series: {
      top_capabilities: DashboardSeriesItem[];
      top_action_kinds: DashboardSeriesItem[];
      event_breakdown: DashboardSeriesItem[];
      intent_breakdown: DashboardSeriesItem[];
    };
    session_breakdown: Array<{
      session_id: string;
      tokens_in: number;
      tokens_out: number;
      total: number;
      events: number;
    }>;
  };
  config: {
    project_config_path: string;
    session_config_path: string;
    effective: Record<string, unknown>;
    entries: DashboardConfigEntry[];
    available_edit_modes: string[];
  };
};

type DashboardResponse = {
  ok: boolean;
  snapshot: DashboardSnapshot;
};

type ConfigSaveResponse = {
  ok: boolean;
  snapshot: DashboardSnapshot;
  message: string;
};

type TomlDocumentsResponse = {
  ok: boolean;
  documents: DashboardTomlDocument[];
};

type ManagedProjectsResponse = {
  ok: boolean;
  projects: DashboardManagedProject[];
};

type TomlSaveResponse = {
  ok: boolean;
  message: string;
  documents: DashboardTomlDocument[];
};

export async function loadDashboard(
  projectRoot?: string,
  sessionId?: string,
): Promise<DashboardSnapshot> {
  const response = await invoke<DashboardResponse>("load_dashboard", {
    projectRoot,
    sessionId,
  });
  return response.snapshot;
}

export async function loadManagedProjects(
  projectRoot?: string,
): Promise<DashboardManagedProject[]> {
  const response = await invoke<ManagedProjectsResponse>("list_managed_projects", {
    projectRoot,
  });
  return response.projects;
}

export async function saveConfigSetting(
  settingPath: string,
  value: string | number | boolean | string[],
  projectRoot?: string,
  scope?: string,
  sessionId?: string,
): Promise<ConfigSaveResponse> {
  return invoke<ConfigSaveResponse>("save_config_setting", {
    projectRoot,
    settingPath,
    value,
    scope,
    sessionId,
  });
}

export async function loadTomlDocuments(
  projectRoot?: string,
  sessionId?: string,
): Promise<DashboardTomlDocument[]> {
  const response = await invoke<TomlDocumentsResponse>("load_toml_documents", {
    projectRoot,
    sessionId,
  });
  return response.documents;
}

export async function saveTomlDocument(
  relativePath: string,
  content: string,
  sessionId?: string,
  projectRoot?: string,
): Promise<TomlSaveResponse> {
  return invoke<TomlSaveResponse>("save_toml_document", {
    projectRoot,
    sessionId,
    relativePath,
    content,
  });
}

export async function toggleManagedMode(
  enable: boolean,
  projectRoot?: string,
  sessionId?: string,
): Promise<{ ok: boolean; managed_mode: Record<string, unknown> }> {
  return invoke("toggle_managed_mode", {
    projectRoot,
    sessionId,
    enable,
  });
}

// ── Conductor chat ──

export async function conductorStart(
  projectRoot?: string,
  backend?: string,
): Promise<{ started: boolean; backend: string; project_root: string }> {
  return invoke("conductor_start", { projectRoot, backend });
}

export async function conductorSend(message: string): Promise<{ sent: boolean }> {
  return invoke("conductor_send", { message });
}

export async function loadMetricsSnapshot(): Promise<{ ok: boolean; snapshot: MonitoringSnapshot }> {
  return invoke("metrics_snapshot", {});
}

export async function mcpRegistrySearch(query: string, limit = 20, cursor?: string): Promise<{ ok: boolean; servers: RegistrySearchResult[]; next_cursor: string | null }> {
  return invoke("mcp_registry_search", { query, limit, cursor });
}

export async function skillScanResults(projectRoot?: string, sessionId?: string): Promise<{ ok: boolean; results: SkillScanResult[] }> {
  return invoke("skill_scan_results", { projectRoot, sessionId });
}

export async function contextBudgetCheck(projectRoot?: string, sessionId?: string): Promise<{ ok: boolean; result: ContextBudgetResult }> {
  return invoke("context_budget_check", { projectRoot, sessionId });
}

export async function contextCompact(projectRoot?: string, sessionId?: string): Promise<{ ok: boolean; result: Record<string, unknown> }> {
  return invoke("context_compact", { projectRoot, sessionId });
}
export async function conductorStatus(): Promise<{ running: boolean }> {
  return invoke("conductor_status", {});
}

export async function conductorStop(): Promise<{ stopped: boolean }> {
  return invoke("conductor_stop", {});
}

// ── Execution management ──

export async function executionClearTokens(projectRoot?: string, sessionId?: string): Promise<{ ok: boolean; runs_deleted: number }> {
  return invoke("execution_clear_tokens", { projectRoot, sessionId });
}

export async function executionClearToolCalls(projectRoot?: string, sessionId?: string): Promise<{ ok: boolean; events_deleted: number; runs_deleted: number }> {
  return invoke("execution_clear_tool_calls", { projectRoot, sessionId });
}

export async function executionPrune(projectRoot?: string, keepDays?: number, maxEvents?: number): Promise<{ ok: boolean; counts: { events: number; runs: number } }> {
  return invoke("execution_prune_events", { projectRoot, keepDays, maxEvents });
}

export async function executionUsageByIdentity(projectRoot?: string): Promise<{ ok: boolean; by_host: Array<{ host_id: string; event_count: number; tokens_in: number }>; by_agent: Array<{ agent_id: string; event_count: number; tokens_in: number }> }> {
  return invoke("execution_usage_by_identity", { projectRoot });
}
