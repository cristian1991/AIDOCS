import { invoke } from "@tauri-apps/api/core";
import { fetch as tauriFetch } from "@tauri-apps/plugin-http";
import { getScope, loadGateConnection } from "./webmcpScope";
import { routeInvoke } from "./platform/transport";

// Same-origin (relative) so the dashboard talks to whichever gate serves it — consistent
// with the transport shim's gateCall, portable, and CSP-safe (connect-src 'self'). In prod
// this resolves to https://mcp.codenexus.cloud/v1/mcp (the SPA's own origin); unchanged there.
const WEBMCP_GATE_MCP = "/v1/mcp";

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
  dashboard_only: boolean;
  requires_restart: boolean;
  is_t0: boolean;
  editable: boolean;
  current_value: unknown;
  scope_values: Record<string, unknown>;
  effective_layer: "factory" | "global" | "project" | "session" | null;
  origin: Record<string, "factory" | "global" | "project" | "session">;
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
  // Non-empty when the document is read-only because its authority moved
  // off the TOML file (legacy config / build-source). Render as a
  // deprecation note + disabled editor.
  deprecated?: string;
  content: string;
};

export type DashboardManagedProject = {
  title: string;
  project_root: string;
  session_count: number;
  managed_session_id: string | null;
  current: boolean;
  source?: string; // WebAgent scope only: gate project source (no session counts)
};

export type RegistrySearchResult = {
  name: string;
  description?: string | null;
  version?: string | null;
  runtime?: string | null;
  transport?: string | null;
  install_commands?: Array<{ type: string; command: string; transport: string }>;
};

export type SkillScanResult = {
  skill: Record<string, unknown>;
  selected: boolean;
  active: boolean;
  activation_tags?: string[];
  provider_status?: SkillProviderStatus | null;
  scan: {
    skill_id: string;
    risk_level: string;
    finding_count: number;
    findings: Array<{ category: string; severity: string; description: string; evidence?: string }>;
  };
};

export type SkillProviderStatus = {
  provider_id: string;
  provider_state: string;
  aidocs_version?: string | null;
  provider_version?: string | null;
  compatible_versions?: string[];
  compatible_version_range?: string | null;
  choices: string[];
  user_choice?: string | null;
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

/**
 * Gate liveness signal (aidocs_mcp/gate_health.py). Probes: hook_traffic
 * (are hooks firing?), hook_declines (did the hook decline itself into
 * silence?), nlp (is the NLP security surface alive?). Statuses are plain
 * strings on purpose — the renderer whitelists "ok" and treats every other
 * value as UNKNOWN, so an unrecognized status can never render green.
 */
export type GateHealth = {
  status: string;
  computed_at?: string;
  reason?: string;
  probes?: Record<string, { status: string; reason?: string }>;
};

export type DashboardSnapshot = {
  // #210 slice-loads: present (true) when this payload is the compact LIVE
  // slice (plans/execution/token_usage/degraded/freezes only) — the caller
  // merges it into the previous full snapshot instead of replacing it.
  live_only?: boolean;
  // Pending freeze/escalation approval cards (rendered by the freeze surface).
  freezes?: Array<Record<string, unknown>>;
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
    // True when a session-scoped owner grant exists in RBAC (an "owned"
    // badge; honest grant-presence, not a fabricated degraded state).
    owner_granted?: boolean;
  }>;
  connected_agents?: {
    agents: Array<{
      host_session_id: string;
      agent_context_id?: string;
      agent_memory_epoch?: string;
      role: string;
      session_id: string;
      host_kind?: string;
      live: boolean;
      pid?: number | null;
      activated_at?: string;
      last_updated?: string;
      source?: string;
    }>;
    live_count: number;
  };
  selected_session_id: string | null;
  /**
   * SEC-005 (2026-04-23): degraded-state visibility for the selected
   * session. `degraded=true` → dashboard renders top-bar red badge +
   * right-panel strip with the reason/time/failure event pointer.
   */
  degraded_state: {
    degraded: boolean;
    reason: string;
    degraded_at: string;
    last_failure_event_id: string;
  };
  /**
   * Gate liveness (server-side, aidocs_mcp/gate_health.py): is the AIDOCS
   * security gate actually RUNNING? `status` is "ok" | "degraded" | "unknown".
   * Deliberately typed as `string` — the card treats anything that is not the
   * exact literal "ok" as UNKNOWN, so a new/garbled status can never widen
   * into a fake green.
   */
  gate_health?: GateHealth | null;
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
  plans?: Array<{
    session_id: string;
    title?: string | null;
    status?: string | null;
    conductor: {
      graph?: { lanes: Array<{ lane_id: string; name: string; depends_on?: string[] }> } | null;
      runnable?: { runnable_lane_ids: string[]; blocked_reasons: Record<string, string[]> } | null;
    } | null;
  }>;
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
    bash_policy?: BashPolicySnapshot;
    rbac?: RBACSnapshot;
    judge_overrides?: JudgeOverridesSnapshot;
    available_edit_modes: string[];
  };
  // Backlog #21 — sticky-perms indicator data for the selected session.
  sticky_grants?: StickyGrantsSnapshot;
};

export type RBACUserRow = {
  user_id: string;
  email: string;
  role: string;
  created_at: string;
  disabled: boolean;
};

export type RBACRoleRow = {
  role_id: string;
  name: string;
  description: string;
  is_system: boolean;
  rank: number;
  inherits_from_role_key: string | null;
  permission_count: number;
};

export type RBACPermissionRow = {
  name: string;
  description: string;
};

export type RBACPendingEscalation = {
  request_id: string;
  requester_label: string;
  requester_user_id: string;
  session_id: string | null;
  task_id: string | null;
  gate_permission: string;
  gate_phrase: string;
  sticky: boolean;
  created_at: string;
  expires_at: string | null;
};

export type RBACSnapshot = {
  users: RBACUserRow[];
  roles: RBACRoleRow[];
  permissions: RBACPermissionRow[];
  pending_escalations: RBACPendingEscalation[];
  summary: {
    user_count: number;
    active_user_count: number;
    role_count: number;
    permission_count: number;
    pending_escalation_count: number;
  };
};

export type BashCommandTriState = "allow" | "deny" | "bubble";

export type BashCommandRow = {
  factory: BashCommandTriState;
  global: BashCommandTriState;
  project: BashCommandTriState;
  session: BashCommandTriState;
  effective: BashCommandTriState;
  patterns: string[] | null;
};

export type BashPolicySnapshot = {
  commands: Record<string, BashCommandRow>;
  default: "allow" | "block";
  layers: Array<"factory" | "global" | "project" | "session">;
};

// Backlog #21 — active sticky user-intent grants for the selected session.
export type StickyGrantRow = {
  grant_id: string;
  tier: number;
  tool: string;
  subcommand: string | null;
  phrase?: string | null;
  registered_at: string;
  registered_by?: string | null;
  judge_verdict?: string | null;
  confirmation_answer?: string | null;
};

export type StickyGrantsSnapshot = {
  session_id: string | null;
  grants: StickyGrantRow[];
  count: number;
};

// Backlog #19/#22 — judge-rule registry + family-split override state.
export type JudgeRuleRow = {
  rule_id: string;
  family: string;
  description: string;
  risk: string;
  locked: boolean;
  verdict_class: string;
  overridden: boolean;
};

export type JudgeOverridesSnapshot = {
  rules: JudgeRuleRow[];
  overrides: Record<string, string[]>;
  families: string[];
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
  liveOnly = false,
): Promise<DashboardSnapshot> {
  if (getScope() === "web") {
    // The gate's dashboard_snapshot has no slice mode — full is a superset.
    return loadDashboardFromGate(projectRoot);
  }
  const response = await invoke<DashboardResponse>("load_dashboard", {
    projectRoot,
    sessionId,
    liveOnly,
  });
  return response.snapshot;
}

// WebAgent (cloud) scope: the dashboard snapshot is served by the gate's
// `dashboard_snapshot` MCP tool (catalog scope) over the operator's OAuth
// bearer token — no direct DB access. Same shape as the local snapshot, so the
// full CastleShell renders identically; only the conductor stays local-only.
async function loadDashboardFromGate(projectId?: string): Promise<DashboardSnapshot> {
  const conn = loadGateConnection();
  if (!conn?.accessToken) {
    throw new Error("Not connected to WebAgent — sign in again from the top bar.");
  }
  // Bind the selected project for this token FIRST — dashboard_snapshot reflects the
  // BOUND project (it takes no project arg). The shared gate transport owns
  // the two-phase protocol: first call without a token, human confirmation,
  // then exact echo of the token issued by the server. Never synthesize one
  // from a project id or schema text.
  if (projectId) {
    await routeInvoke("project_select", { project_id: projectId });
  }
  const res = await tauriFetch(WEBMCP_GATE_MCP, {
    method: "POST",
    headers: {
      Authorization: "Bearer " + conn.accessToken,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      jsonrpc: "2.0",
      id: 1,
      method: "tools/call",
      params: { name: "dashboard_snapshot", arguments: {} },
    }),
  });
  if (!res.ok) {
    throw new Error("WebAgent dashboard request failed (HTTP " + res.status + ")");
  }
  const rpc = (await res.json()) as {
    error?: { message?: string };
    result?: { content?: Array<{ text?: string }> };
  };
  if (rpc.error) {
    throw new Error("WebAgent: " + (rpc.error.message || "tool error"));
  }
  const text = rpc.result?.content?.[0]?.text;
  if (!text) {
    throw new Error("WebAgent returned an empty dashboard snapshot.");
  }
  const parsed = JSON.parse(text) as {
    snapshot?: DashboardSnapshot;
    _error?: string;
  };
  if (parsed._error) {
    throw new Error("WebAgent: " + parsed._error);
  }
  if (!parsed.snapshot) {
    throw new Error("WebAgent response had no snapshot.");
  }
  return parsed.snapshot;
}

// ── Governed Bash (verified-posture native shell) ─────────────────

// READ-ONLY live execution posture (the single authority the PreToolUse
// adapter + CLI consume). The UI renders these checks; it never re-derives.
export type LiveExecutionPosture = {
  route: string | null;
  ok: boolean | null;
  reason?: string | null;
  repair?: string | null;
  checks: Record<string, unknown>;
};

// A SIGNED, exact-path approval offer. Bound to path + current hash + nonce
// + expiry — the operator echoes one back verbatim to approve. The UI never
// types a path or a hash.
export type GovernedBashCandidateCard = {
  provider_path: string;
  sha256: string;
  nonce: string;
  issued_at: number;
  expiry: number;
  token: string;
};

export type GovernedBashApprovalCard = {
  title: string;
  detail: string;
  user_writable_candidates: string[];
  path_candidate: string;
  scanned_system_roots: string[];
  candidate_cards: GovernedBashCandidateCard[];
};

export type GovernedBashPosture = {
  verified: boolean;
  flags: Record<string, boolean>;
  checks: Record<string, boolean | null | string>;
  provider_path: string;
  trusted_roots: string[];
  hash_pinned: boolean;
  os_signature_required: boolean;
  // Read-only live diagnostics surfaced by the status command.
  live_execution_posture?: LiveExecutionPosture;
};

export type GovernedBashResult = {
  ok: boolean;
  posture?: GovernedBashPosture;
  reason?: string;
  message?: string;
  checks?: Record<string, boolean | null | string>;
  blocked_by?: string;
  // Present when no system provider auto-enrolled: the operator must approve
  // one signed candidate card.
  requires_operator_approval?: boolean;
  approval_card?: GovernedBashApprovalCard;
};

export async function governedBashStatus(
  projectRoot?: string,
): Promise<GovernedBashPosture> {
  return invoke<GovernedBashPosture>("governed_bash_status", { projectRoot });
}

// THE one action: "Allow shell tools validated and supported by AIDOCS".
// No path / hash / signature inputs — the backend auto-discovers + attests,
// or returns a signed approval card. An explicit card is echoed back ONLY to
// approve a server-issued candidate.
export async function governedBashEnable(opts: {
  projectRoot?: string;
  approvalCardJson?: string;
  scope?: string;
}): Promise<GovernedBashResult> {
  return invoke<GovernedBashResult>("governed_bash_enable", {
    projectRoot: opts.projectRoot,
    approvalCardJson: opts.approvalCardJson,
    scope: opts.scope ?? "global",
  });
}

export async function governedBashDisable(
  projectRoot?: string,
  scope = "global",
): Promise<GovernedBashResult> {
  return invoke<GovernedBashResult>("governed_bash_disable", {
    projectRoot,
    scope,
  });
}

// ── Memory anchor health ──────────────────────────────────────────
// Operator-visible signal of whether the memory→code-unit anchor wire is
// feeding (live) or dormant (starved). Cheap COUNT-only; surfaced here in the
// dashboard ONLY, never grafted onto the ai_palace_status hot path.

export type MemoryAnchorWire = "live" | "starved" | "unknown" | "error";

export interface MemoryAnchorHealth {
  active_memories: number;
  anchored_memories: number;
  total_anchors: number;
  palace_drawers: number | null;
  coverage_pct: number;
  wire: MemoryAnchorWire;
}

export async function memoryAnchorHealth(
  projectRoot: string,
): Promise<{ ok: boolean; health: MemoryAnchorHealth }> {
  return invoke<{ ok: boolean; health: MemoryAnchorHealth }>(
    "memory_anchor_health",
    { projectRoot },
  );
}

// ── Live cursor (cheap change-detector) ───────────────────────────
// A tiny sqlite-derived signature of live state (execution events + lane
// agents), read directly in Rust with NO python spawn. The dashboard polls
// this cheaply; only when the cursor changes does it pull the full snapshot —
// so an idle dashboard stops spawning cli-dashboard every 2s.

export async function dashboardLiveCursor(
  projectRoot: string,
): Promise<{ ok: boolean; cursor: string }> {
  return invoke<{ ok: boolean; cursor: string }>("dashboard_live_cursor", {
    projectRoot,
  });
}

// ── Operator Surface Catalog ──────────────────────────────────────
// Doctrine-level control profiles over the raw config ledger. The UI
// renders profiles (status/inspect/rows) and drives changes through
// apply / expertSet — never a raw per-key save of a service-managed or
// deprecated key (those are refused by the backend on every surface).

export interface OperatorProfileSummary {
  id: string;
  title: string;
  doctrine_area: string;
  danger: string;
  managed_by: string;
  advanced_only: boolean;
  keys: string[];
  hidden_owned_keys: string[];
}

export interface OperatorSurfaceResult {
  ok: boolean;
  error?: string;
  message?: string;
  expected_confirm?: string;
  [k: string]: unknown;
}

export async function operatorSurfaceList(
  projectRoot?: string,
): Promise<{ ok: boolean; profiles: OperatorProfileSummary[] }> {
  return invoke("operator_surface_list", { projectRoot });
}

export async function operatorSurfaceStatus(
  profileId: string,
  projectRoot?: string,
  sessionId?: string,
): Promise<OperatorSurfaceResult> {
  return invoke("operator_surface_status", {
    projectRoot,
    profileId,
    sessionId: sessionId ?? null,
  });
}

export async function operatorSurfaceInspect(
  key: string,
  projectRoot?: string,
  sessionId?: string,
): Promise<OperatorSurfaceResult> {
  return invoke("operator_surface_inspect", {
    projectRoot,
    key,
    sessionId: sessionId ?? null,
  });
}

export async function operatorSurfaceRows(
  projectRoot?: string,
  sessionId?: string,
): Promise<{
  ok: boolean;
  normal: OperatorSurfaceResult[];
  advanced_raw: OperatorSurfaceResult[];
}> {
  return invoke("operator_surface_rows", {
    projectRoot,
    sessionId: sessionId ?? null,
  });
}

export async function operatorSurfaceApply(opts: {
  profileId: string;
  projectRoot?: string;
  valuesJson?: string;
  confirm?: string;
  reason?: string;
  scope?: string;
  action?: string;
  providerPath?: string;
  hashPin?: string;
  requireOsSignature?: boolean;
}): Promise<OperatorSurfaceResult> {
  return invoke("operator_surface_apply", {
    projectRoot: opts.projectRoot,
    profileId: opts.profileId,
    valuesJson: opts.valuesJson ?? null,
    confirm: opts.confirm ?? null,
    reason: opts.reason ?? null,
    scope: opts.scope ?? "global",
    action: opts.action ?? null,
    providerPath: opts.providerPath ?? null,
    hashPin: opts.hashPin ?? null,
    requireOsSignature: opts.requireOsSignature ?? false,
  });
}

export async function operatorSurfaceExpertSet(opts: {
  key: string;
  valueJson: string;
  projectRoot?: string;
  confirm?: string;
  scope?: string;
}): Promise<OperatorSurfaceResult> {
  return invoke("operator_surface_expert_set", {
    projectRoot: opts.projectRoot,
    key: opts.key,
    valueJson: opts.valueJson,
    confirm: opts.confirm ?? null,
    scope: opts.scope ?? "global",
  });
}

export async function loadManagedProjects(
  projectRoot?: string,
): Promise<DashboardManagedProject[]> {
  if (getScope() === "web") {
    // WebAgent scope: the project list is the set the GATE serves, captured by
    // the OAuth connect (project_list tool) and persisted on the connection.
    // No session counts come from the gate, so subtitle falls back to source.
    const conn = loadGateConnection();
    // Fetch the project list LIVE (not the connect-time snapshot) so the selector
    // always reflects current projects and an existing session populates on reload
    // without re-signing-in. Falls back to the connection-captured list on failure.
    let list = conn?.projects ?? [];
    if (conn?.accessToken) {
      try {
        const _r = await tauriFetch(WEBMCP_GATE_MCP, {
          method: "POST",
          headers: { Authorization: "Bearer " + conn.accessToken, "Content-Type": "application/json" },
          body: JSON.stringify({
            jsonrpc: "2.0",
            id: 1,
            method: "tools/call",
            params: { name: "project_list", arguments: {} },
          }),
        });
        if (_r.ok) {
          const _j = await _r.json();
          let _d = _j?.result?.structuredContent;
          if (!_d && _j?.result?.content?.[0]?.text) {
            try { _d = JSON.parse(_j.result.content[0].text); } catch { /* keep fallback */ }
          }
          if (Array.isArray(_d?.projects)) list = _d.projects;
        }
      } catch {
        /* fall back to the connection-captured list */
      }
    }
    return list.map((p) => ({
      title: p.name,
      project_root: p.project_id,
      session_count: 0,
      managed_session_id: null,
      current: p.current,
      source: p.source,
    }));
  }
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
  reason?: string,
): Promise<ConfigSaveResponse> {
  return invoke<ConfigSaveResponse>("save_config_setting", {
    projectRoot,
    settingPath,
    value,
    scope,
    sessionId,
    reason,
  });
}

export type ConfigBatchOperation = {
  action: "set" | "delete";
  setting_path: string;
  value?: string | number | boolean | string[];
  scope?: string;
  session_id?: string;
};

export async function batchConfigSettings(
  operations: ConfigBatchOperation[],
  projectRoot?: string,
): Promise<ConfigSaveResponse> {
  return invoke<ConfigSaveResponse>("batch_config_settings", {
    projectRoot,
    operations,
  });
}

export async function deleteConfigSetting(
  settingPath: string,
  projectRoot?: string,
  scope?: string,
  sessionId?: string,
): Promise<ConfigSaveResponse> {
  return invoke<ConfigSaveResponse>("delete_config_setting", {
    projectRoot,
    settingPath,
    scope,
    sessionId,
  });
}

export interface PalaceMaintenanceResult {
  ok: boolean;
  blocked_by?: string;
  reason?: string;
  login_required?: boolean;
  authorized_via?: string;
  user_id?: string;
  role?: string;
  source?: string;
  scanned?: number;
  retired_legacy?: number;
  reingested?: number;
  failed?: number;
  lookup_lag?: number;
  dry_run?: boolean;
}

/**
 * Guarded MemPalace maintenance (admin-only). The Rust command attaches the
 * cached operator token; corpo without login returns {login_required:true}.
 */
export async function palaceMaintenance(
  opts: {
    mode?: string;
    dryRun?: boolean;
    force?: boolean;
    sessionId?: string;
    projectRoot?: string;
  } = {},
): Promise<PalaceMaintenanceResult> {
  return invoke<PalaceMaintenanceResult>("palace_maintenance", {
    projectRoot: opts.projectRoot,
    mode: opts.mode ?? "backfill_legacy_memory_drawers",
    dryRun: opts.dryRun ?? false,
    force: opts.force ?? false,
    sessionId: opts.sessionId,
  });
}

export async function toggleSkill(
  skillId: string,
  enabled: boolean,
  projectRoot?: string,
  sessionId?: string,
): Promise<ConfigSaveResponse> {
  return invoke<ConfigSaveResponse>("toggle_skill", {
    projectRoot,
    sessionId,
    skillId,
    enabled,
  });
}

export async function deleteSkill(
  skillId: string,
  projectRoot?: string,
  sessionId?: string,
): Promise<ConfigSaveResponse> {
  return invoke<ConfigSaveResponse>("delete_skill", { projectRoot, sessionId, skillId });
}

export async function setSkillProviderOverride(
  providerId: string,
  choice?: string | null,
  projectRoot?: string,
): Promise<{ ok: boolean } & SkillProviderStatus> {
  return invoke("set_skill_provider_override", { projectRoot, providerId, choice: choice ?? null });
}

export type InstalledMcpServer = {
  name: string;
  transport: string;
  command: string;
  config: Record<string, unknown>;
};

export async function listMcpServers(projectRoot?: string): Promise<{ ok: boolean; servers: InstalledMcpServer[] }> {
  return invoke("list_mcp_servers", { projectRoot });
}

export async function installMcpServer(
  name: string,
  command: string,
  args: string[],
  projectRoot?: string,
  transport?: string,
): Promise<{ ok: boolean; name: string; message: string }> {
  return invoke("install_mcp_server", { projectRoot, name, command, args, transport });
}

export async function deleteMcpServer(
  name: string,
  projectRoot?: string,
): Promise<{ ok: boolean; name: string; removed?: boolean; message: string }> {
  return invoke("delete_mcp_server", { projectRoot, name });
}

export async function loadTomlDocuments(
  projectRoot?: string,
  sessionId?: string,
): Promise<DashboardTomlDocument[]> {
  // Phased out (2026-06-30): the TOML document panel read DEPRECATED legacy
  // files from disk AND spawned a second Python subprocess (load_toml_documents
  // then a dashboard_toml_editability check) just to render a read-only view whose
  // data already rides in the snapshot (config lives in SQLite). AIDOCS is
  // retiring file-based artifacts, so this no longer touches disk; the panel
  // renders empty pending its component removal. No subprocess, no file reads.
  void projectRoot;
  void sessionId;
  return [];
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

// ── Empire intent-tokens vocab + gate messages (Phase 4 schema-aware editor) ──

export type VocabGroup = {
  parent_key: string;
  parent_mode: string;
  tokens: string[];
  attrs: Record<string, unknown>;
};

export type GateMsgRow = { key: string; body: string; source: string };

export async function vocabListKinds(): Promise<string[]> {
  const r = await invoke<{ kinds: string[] }>("vocab_list_kinds", {});
  return r.kinds;
}

export async function vocabListLangs(): Promise<string[]> {
  const r = await invoke<{ langs: string[] }>("vocab_list_langs", {});
  return r.langs;
}

export async function vocabGetGrouped(
  kind: string,
  lang: string,
): Promise<Record<string, VocabGroup>> {
  const r = await invoke<{
    groups: Record<string, VocabGroup>;
    kind: string;
    lang: string;
  }>("vocab_get_grouped", { kind, lang });
  return r.groups;
}

export async function vocabUpsertGroup(
  kind: string,
  lang: string,
  parentKey: string,
  tokens: string[],
  attrs?: Record<string, unknown>,
  parentMode?: string,
): Promise<{ deleted: number; inserted: number }> {
  return invoke("vocab_upsert_group", {
    kind,
    lang,
    parentKey,
    tokens,
    attrs,
    parentMode,
  });
}

export async function vocabDeleteGroup(
  kind: string,
  lang: string,
  parentKey: string,
  parentMode?: string,
): Promise<{ deleted: number }> {
  return invoke("vocab_delete_group", { kind, lang, parentKey, parentMode });
}

export async function gateMsgList(lang: string = "en"): Promise<GateMsgRow[]> {
  const r = await invoke<{ rows: GateMsgRow[] }>("gate_msg_list", { lang });
  return r.rows;
}

export async function gateMsgUpsert(
  key: string,
  body: string,
  lang: string = "en",
): Promise<{ deleted: number; inserted: number }> {
  return invoke("gate_msg_upsert", { key, body, lang });
}

export async function gateMsgDelete(
  key: string,
  lang: string = "en",
): Promise<{ deleted: boolean }> {
  return invoke("gate_msg_delete", { key, lang });
}

// ── Conductor chat ──

export async function conductorStart(
  projectRoot: string,
  sessionId: string,
  backend?: string,
  model?: string,
): Promise<{ started: boolean; backend: string; project_root: string; session_id: string; model?: string }> {
  return invoke("conductor_start", { projectRoot, sessionId, backend, model });
}

export async function conductorSend(
  projectRoot: string,
  sessionId: string,
  message: string,
): Promise<{ sent: boolean }> {
  return invoke("conductor_send", { projectRoot, sessionId, message });
}

export async function opencodeModels(): Promise<{ ok: boolean; models: string[] }> {
  return invoke("opencode_models", {});
}

export interface ConductorOutputLine {
  timestamp: number;
  stream: string;
  text: string;
}

export async function conductorOutput(
  projectRoot: string,
  sessionId: string,
  since?: number,
): Promise<{ running: boolean; lines: ConductorOutputLine[]; total_buffered: number }> {
  return invoke("conductor_output", { projectRoot, sessionId, since: since ?? null });
}

export async function conductorStop(
  projectRoot: string,
  sessionId: string,
): Promise<{ stopped: boolean }> {
  return invoke("conductor_stop", { projectRoot, sessionId });
}

// ── Message substrate — role-addressed comms available to all agents ──
// Phoenix 2026-05-12: renamed from cerberus_* per Empire directive
// (single canonical name end-to-end). Targets stay predefined.

export type MsgRoleApi = "king" | "conductor" | "co-conductor";

export interface MsgInboxMessage {
  id: string;
  from_role: MsgRoleApi;
  to_roles: MsgRoleApi[];
  body: string;
  thread_id: string;
  created_at: number;
  status: string;
}

export async function msgSend(
  projectRoot: string,
  sessionId: string,
  toRoles: MsgRoleApi[],
  body: string,
  inReplyTo?: string,
): Promise<{ ok: boolean; id: string; thread_id: string; to_roles: MsgRoleApi[] }> {
  return invoke("tauri_msg_send", {
    projectRoot,
    sessionId,
    toRoles,
    body,
    inReplyTo: inReplyTo ?? null,
  });
}

export async function msgInbox(
  projectRoot: string,
  sessionId: string,
  role: MsgRoleApi,
): Promise<{ ok: boolean; messages: MsgInboxMessage[] }> {
  return invoke("tauri_msg_inbox", { projectRoot, sessionId, role });
}

// ── ai_backlog / task todos (ai_task todo modes since #83; Phoenix backlog-todo dashboard) ──

export interface BacklogItem {
  id: number;
  title?: string | null;
  content?: string | null;
  status?: string | null;
  priority?: string | null;
  tags?: string[];
  session_id?: string | null;
  promoted_from_todo_id?: number | null;
  created_at?: string | number | null;
  updated_at?: string | number | null;
  [key: string]: unknown;
}

export interface TodoItem {
  id: number;
  content?: string | null;
  status?: string | null;
  urgency?: string | null;
  tags?: string[];
  task_id?: string | null;
  session_id?: string | null;
  promoted_to_backlog_id?: number | null;
  promoted_from_todo_id?: number | null;
  linked_task_id?: string | null;
  created_at?: string | number | null;
  updated_at?: string | number | null;
  [key: string]: unknown;
}

/**
 * Authority metadata a gated backlog call returns when it REFUSES (2026-07-30).
 *
 * Backlog CRUD is permission-gated (backlog.read / .write / .remove) and a
 * refusal is a first-class RESULT, not an exception and not an empty payload.
 * `items` / `item` / `result` are OPTIONAL for exactly that reason: a refusal
 * carries none of them, so a caller that renders `items ?? []` would show an
 * empty list where a refusal belongs. Check `ok` first.
 */
export interface BacklogAuthority {
  ok: boolean;
  blocked_by?: string;
  reason?: string;
  required_permission?: string;
  action?: string;
  message?: string;
}

export async function backlogList(
  projectRoot?: string,
  filters?: { status?: string; priority?: string; limit?: number },
): Promise<BacklogAuthority & { items?: BacklogItem[] }> {
  return invoke("tauri_backlog_list", {
    projectRoot,
    status: filters?.status ?? null,
    priority: filters?.priority ?? null,
    limit: filters?.limit ?? null,
  });
}

export async function backlogGet(
  backlogId: number,
  projectRoot?: string,
): Promise<BacklogAuthority & { item?: BacklogItem | null }> {
  return invoke("tauri_backlog_get", { projectRoot, backlogId });
}

export async function backlogAdd(
  content: string,
  projectRoot?: string,
  opts?: { priority?: string; status?: string; kind?: string; tags?: string[]; sessionId?: string },
): Promise<BacklogAuthority & { result?: Record<string, unknown> }> {
  return invoke("tauri_backlog_add", {
    projectRoot,
    content,
    priority: opts?.priority ?? null,
    status: opts?.status ?? null,
    // #573 kind. Never send "" — KIND_UNSET is the stored default and is NOT
    // storable, so "don't touch it" and "set it to unrated" must stay distinct.
    kind: opts?.kind || null,
    tags: opts?.tags ?? null,
    sessionId: opts?.sessionId ?? null,
  });
}

export async function backlogUpdate(
  backlogId: number,
  patch: { status?: string; priority?: string; content?: string; kind?: string; tags?: string[] },
  projectRoot?: string,
): Promise<BacklogAuthority & { result?: Record<string, unknown> }> {
  return invoke("tauri_backlog_update", {
    projectRoot,
    backlogId,
    status: patch.status ?? null,
    priority: patch.priority ?? null,
    content: patch.content ?? null,
    // #573 kind — see the note in backlogAdd.
    kind: patch.kind || null,
    tags: patch.tags ?? null,
  });
}

export async function backlogRemove(
  backlogId: number,
  reason: string,
  projectRoot?: string,
): Promise<{ ok: boolean; result: Record<string, unknown> }> {
  return invoke("tauri_backlog_remove", { projectRoot, backlogId, reason });
}

export async function todoList(
  projectRoot?: string,
  scope?: { sessionId?: string; taskId?: string },
): Promise<{ ok: boolean; items: TodoItem[] }> {
  return invoke("tauri_todo_list", {
    projectRoot,
    sessionId: scope?.sessionId ?? null,
    taskId: scope?.taskId ?? null,
  });
}

export async function todoUpdate(
  todoId: number,
  patch: {
    status?: string;
    content?: string;
    tags?: string[];
    urgency?: string;
    taskId?: string;
    sessionId?: string;
  },
  projectRoot?: string,
): Promise<{ ok: boolean; result: Record<string, unknown> }> {
  return invoke("tauri_todo_update", {
    projectRoot,
    todoId,
    status: patch.status ?? null,
    content: patch.content ?? null,
    tags: patch.tags ?? null,
    urgency: patch.urgency ?? null,
    taskId: patch.taskId ?? null,
    sessionId: patch.sessionId ?? null,
  });
}

export async function todoRemove(
  todoId: number,
  reason: string,
  projectRoot?: string,
): Promise<{ ok: boolean; result: Record<string, unknown> }> {
  return invoke("tauri_todo_remove", { projectRoot, todoId, reason });
}

// ── Memory knowledge-graph (dashboard-war (d), #200) ──
export interface MemoryKgNode {
  id: string;
  label: string;
  group: string;
  type: "memory" | "unit" | "keyword";
  path?: string;
  kind?: string;
  file?: string;
  symbol?: string;
}
export interface MemoryKgEdge {
  from: string;
  to: string;
  type: "anchor" | "keyword" | "link";
  confidence?: string;
}
export interface MemoryKgGraph {
  ok: boolean;
  nodes: MemoryKgNode[];
  edges: MemoryKgEdge[];
  counts?: { memories?: number; units?: number; keywords?: number; edges?: number };
}
export interface MemoryDetail {
  ok: boolean;
  path?: string;
  kind?: string;
  title?: string;
  content?: string;
  source?: string;
  status?: string;
  updated_at?: string;
  _detail?: string;
}

export async function memoryKgGraph(projectRoot?: string): Promise<MemoryKgGraph> {
  return invoke("memory_kg_graph", { projectRoot: projectRoot ?? "" });
}

export async function memoryKgGet(path: string, projectRoot?: string): Promise<MemoryDetail> {
  return invoke("memory_kg_get", { projectRoot: projectRoot ?? "", path });
}

/** Start the Rust cursor-watcher thread (desktop): pushes aidocs://live-cursor
 * events on change so the frontend needs no 2s cursor-poll invoke (#204 (c)). */
export async function startCursorWatcher(projectRoot: string): Promise<void> {
  await invoke("start_cursor_watcher", { projectRoot });
}

export interface MemoryCaptureResult {
  ok: boolean;
  target?: string;
  checksum?: string;
  message?: string;
  _error?: string;
  _detail?: string;
}

/** Governed memory write (#200): same doctrine path as the memory_capture tool. */
export async function memoryCapture(
  kind: string,
  content: string,
  targetHint?: string,
  projectRoot?: string,
): Promise<MemoryCaptureResult> {
  return invoke("memory_capture", {
    projectRoot: projectRoot ?? "",
    kind,
    content,
    targetHint: targetHint ?? "",
  });
}

export async function mcpRegistrySearch(query: string, limit = 20, cursor?: string): Promise<{ ok: boolean; servers: RegistrySearchResult[]; next_cursor: string | null }> {
  return invoke("mcp_registry_search", { query, limit, cursor });
}

export async function skillScanResults(projectRoot?: string, sessionId?: string): Promise<{ ok: boolean; results: SkillScanResult[] }> {
  return invoke("skill_scan_results", { projectRoot, sessionId });
}

// ── Ref-integrity (broken references) — Empire goal 2026-06-20 ──
export interface BrokenRefSample {
  path: string;
  line: number;
  token: string;
}
export interface BrokenRefKind {
  kind: string;
  resolvable: boolean;
  reason?: string;
  reference_count: number;
  definition_count?: number;
  broken_count?: number;
  broken_sample?: BrokenRefSample[];
  truncated?: boolean;
}
export interface BrokenReferencesReport {
  evidence: { kind: string; proves: string; limitations: string };
  total_broken: number;
  kinds: BrokenRefKind[];
}
export async function brokenReferencesCheck(
  projectRoot?: string,
  limit = 200,
): Promise<{ ok: boolean; report: BrokenReferencesReport | null }> {
  return invoke("broken_references", { projectRoot, limit });
}

export interface LaneScope {
  ok: boolean;
  tools: string[];
  tool_source?: string;
  files: string[];
  _detail?: string;
}
export async function laneScope(
  laneId: string,
  sessionId?: string,
  projectRoot?: string,
): Promise<LaneScope> {
  return invoke("lane_scope", { projectRoot, sessionId, laneId });
}

export async function contextBudgetCheck(projectRoot?: string, sessionId?: string): Promise<{ ok: boolean; result: ContextBudgetResult }> {
  return invoke("context_budget_check", { projectRoot, sessionId });
}

export async function contextCompact(projectRoot?: string, sessionId?: string): Promise<{ ok: boolean; result: Record<string, unknown> }> {
  return invoke("context_compact", { projectRoot, sessionId });
}
export async function conductorStatus(
  projectRoot: string,
  sessionId: string,
): Promise<{ running: boolean; backend?: string | null; model?: string | null; session_id: string; claude_session_id?: string | null }> {
  return invoke("conductor_status", { projectRoot, sessionId });
}

// `reason` is REQUIRED by session_deletion_law (>=6 chars) and recorded in the
// audit trail. It is typed optional only so existing callers keep compiling; a
// missing reason is refused by the law, not silently defaulted here — inventing
// one client-side would put a fabricated justification in the audit record.
export async function deleteSession(
  sessionId: string,
  projectRoot?: string,
  reason?: string,
): Promise<{
  ok: boolean;
  deleted_session_id?: string;
  snapshot?: DashboardSnapshot;
  // A refused delete returns ok:false with the control-plane reason (e.g.
  // operator_auth) instead of throwing — callers must check ok, not assume.
  blocked_by?: string;
  reason?: string;
  message?: string;
}> {
  return invoke("delete_session", { projectRoot, sessionId, reason });
}

export async function createSession(
  title: string,
  options: { sessionId?: string; goal?: string; projectRoot?: string } = {},
): Promise<{
  ok: boolean;
  session_id?: string;
  owner_grant?: string;       // granted | not_required | failed
  ownership_degraded?: boolean;
  owner_user_id?: string;
  warning?: string;
  blocked_by?: string;        // operator_auth on refusal
  reason?: string;
  message?: string;
}> {
  return invoke("create_session", {
    projectRoot: options.projectRoot,
    title,
    sessionId: options.sessionId,
    goal: options.goal,
  });
}

export async function connectSession(
  sessionId: string,
  projectRoot?: string,
): Promise<{
  ok: boolean;
  connected?: boolean;
  session_id?: string;
  blocked_by?: string;        // operator_auth / session_not_in_project
  reason?: string;
  message?: string;
}> {
  return invoke("connect_session", { projectRoot, sessionId });
}

// ── Execution management ──

// #885: Clear Tokens is a DISPLAY reset, not a deletion. It appends a
// token_usage_reset watermark to the append-only execution_events ledger and
// the token queries floor on it; events_deleted / runs_deleted are 0 by
// construction and are returned so nothing can report this as a deletion.
export async function executionClearTokens(projectRoot?: string, sessionId?: string): Promise<{
  ok: boolean;
  cleared: string;
  reset: boolean;
  event_id: string;
  scope: string;
  sessions_floored: number;
  events_deleted: 0;
  runs_deleted: 0;
}> {
  return invoke("execution_clear_tokens", { projectRoot, sessionId });
}

// #885: this one really deletes, so it is routed through audit_deletion_law
// and returns the LAW'S VERDICT, not a success shape. `ok: false` with
// `blocked_by` is the normal answer from the dashboard, which carries no
// authenticated operator context — callers must check `ok` and must not
// announce a clear that did not happen.
export async function executionClearToolCalls(projectRoot?: string, sessionId?: string, reason?: string): Promise<{
  ok: boolean;
  cleared: string;
  blocked_by?: string;
  error?: string;
  result?: { events_deleted: number; runs_deleted: number };
}> {
  return invoke("execution_clear_tool_calls", { projectRoot, sessionId, reason });
}

export async function executionPrune(projectRoot?: string, keepDays?: number, maxEvents?: number): Promise<{ ok: boolean; counts: { events: number; runs: number } }> {
  return invoke("execution_prune_events", { projectRoot, keepDays, maxEvents });
}

// ── Operator auth + host-operator bindings (desktop; 1 dashboard = 1 user = bind) ──

export type OperatorAuthStatus = {
  ok: boolean;
  authenticated: boolean;
  user_id: string;
  role: string;
};

/** Desktop operator sign-in status — reflects ACTUAL token validity (never a
 *  fake-connected state). Resolves in-process OR shared machine-cache token. */
export async function dashboardAuthStatus(projectRoot?: string): Promise<OperatorAuthStatus> {
  return invoke<OperatorAuthStatus>("dashboard_auth_status", { projectRoot });
}

// REMOVED 2026-08-31 — `operatorLogin(email, password)` and its
// `OperatorLoginResult`. It invoked the `operator_login` Tauri command, which
// shells `dashboard-login --email --password` against the LOCAL bcrypt store:
// a password authority ON THIS MACHINE, i.e. the second source of truth #207
// forbids. Operator ruling 2026-08-31: "there is no 'locally created user' —
// users are stored on codenexus.cloud"; "having a local operator makes the
// whole process swiss cheese". It had no caller left in the app either — the
// desktop login became CodeNexus-only at #507 — so this was a live door with
// nothing behind it.
//
// WHAT WAS NOT REMOVED, AND WHY: the CLI's own `--method password` on-ramp
// (`cli.py::_login_core`). That is the PRODUCER of the machine-cached device
// session for CI, headless installs and dashboard binding approval; retiring it
// broke TestBindingApprovalDeadlock, a real lockout guard, and was reverted
// (cli.py:6846-6880). A consumer can retire without a substitute; a producer
// cannot. This was the consumer.

/** Sign out: revoke the operator token row and clear BOTH the in-process cache
 *  and the shared machine cache (~/.aidocs/operator_token.json). This is the
 *  only revocation path the UI has — see WebmcpModePanel's UserMenu. */
export async function operatorLogout(projectRoot?: string): Promise<{ ok: boolean; logged_out: boolean }> {
  return invoke("operator_logout", { projectRoot });
}

export type HostBindingRow = {
  binding_id: string;
  status: string; // "pending" | "approved" | "revoked" | ...
  host_kind: string;
  host_session_id: string;
  operator_user_id: string;
  created_at: string;
  age_seconds: number | null;
  expires_at: string;
  approved_at: string | null;
};

/** Pending + approved host-operator bindings for this project. */
export async function bindingsList(projectRoot?: string): Promise<{ ok: boolean; bindings: HostBindingRow[]; count: number }> {
  return invoke("bindings_list", { projectRoot });
}

export type BindingApproveResult = {
  ok: boolean;
  binding_id?: string;
  operator_user_id?: string;
  blocked_by?: string; // "operator_auth" when unauthenticated
  reason?: string;
  message?: string;
};

/** Approve a pending binding ("Bind to me") — rides the cached operator token
 *  through the SAME audited approve path. Fail-closed without a valid token. */
export async function bindingApprove(bindingId: string, projectRoot?: string): Promise<BindingApproveResult> {
  return invoke<BindingApproveResult>("binding_approve", { projectRoot, bindingId });
}

