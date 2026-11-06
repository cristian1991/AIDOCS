/**
 * Authority presentation — single source of UI truth for the control-plane
 * authority metadata the backend already emits (2026-05-25).
 *
 * The Python control plane stamps every action result/audit with authority
 * fields: blast_radius/broadening, blocked_by/reason, owner_grant/
 * ownership_degraded, dashboard_only, etc. These pure helpers map that metadata
 * to operator-facing labels/badges/banners using the SAME actor/scope/target/
 * status/reason language the backend uses, so the UI can never imply authority
 * the backend did not grant. No React, no I/O — fully unit-testable.
 */

export type Scope = "global" | "project" | "session";
export type Severity = "info" | "warning" | "danger";

/** Subset of any backend action result that carries authority metadata. */
export interface AuthorityResult {
  ok?: boolean;
  blocked_by?: string;
  reason?: string;
  session_id?: string;
  scope?: string;
  blast_radius?: string;
  broadening?: boolean;
  warning?: string;
  owner_grant?: string; // "granted" | "not_required" | "failed"
  ownership_degraded?: boolean;
  dashboard_only?: boolean;
  is_t0?: boolean;
  /**
   * The permission that would have authorized the refused act. Emitted by the
   * RBAC gate sites (hub.require_permission, dashboard_backlog_service). Present
   * ONLY on refusals, and the reason a refusal can name its own remedy instead
   * of rendering as a blank panel.
   */
  required_permission?: string;
  /**
   * Some refusals answer in `error` + `message` rather than `blocked_by` +
   * `reason` — operator_surface.expert_set is the one that bit us: it returns
   * `{ok: false, error: "confirmation_required", message: "...echo the exact
   * confirmation: 'confirm-set <key>'", expected_confirm}`. Until 2026-08-03
   * this interface did not model those fields, so the renderer could not see
   * them and fell through to "The control plane refused this action" —
   * discarding a reply that named the precise remedy. The operator concluded
   * the setting was unsettable; the conductor reported it unreachable by every
   * path. Both wrong, and both downstream of a dropped string.
   */
  error?: string;
  message?: string;
  /** The exact phrase the backend will accept, when it names one. */
  expected_confirm?: string;
}

export interface Badge {
  label: string;
  severity: Severity;
  tooltip: string;
}

export interface Banner {
  title: string;
  message: string;
  severity: Severity;
  hint: string;
  /** the backend blocked_by/reason code, surfaced verbatim for support */
  code: string;
}

// ── blast radius: a global write reaches every project (broadening) ──────
export function blastRadiusBadge(r: AuthorityResult): Badge | null {
  const radius = (r.blast_radius || r.scope || "").toLowerCase();
  if (radius === "global") {
    return {
      label: "Install-wide",
      severity: "warning",
      tooltip:
        "GLOBAL write: applies to EVERY project on this install, not just " +
        "the current one. A project or session may still override it.",
    };
  }
  if (radius === "project") {
    return { label: "Project", severity: "info", tooltip: "Affects this project only." };
  }
  if (radius === "session") {
    return { label: "Session", severity: "info", tooltip: "Affects this session only." };
  }
  return null;
}

/** The broadening warning string for a coarser/global write, or null. */
export function broadeningWarning(r: AuthorityResult): string | null {
  if (!r.broadening) return null;
  return (
    r.warning ||
    "This is an install-wide (global) write — it changes the default for " +
      "EVERY project on this machine. Confirm the broad reach is intended."
  );
}

// ── refusal banners: map blocked_by → operator-facing, backend-aligned ───
const REFUSAL_TEXT: Record<
  string,
  { title: string; message: string; hint: string }
> = {
  operator_auth: {
    title: "Sign in required",
    message: "This action needs an authenticated operator.",
    hint: "Sign in to the Dashboard as ADMIN/SUPERADMIN, then retry.",
  },
  unauthenticated: {
    title: "Sign in required",
    message: "No operator is authenticated.",
    hint: "Sign in to the Dashboard as ADMIN/SUPERADMIN, then retry.",
  },
  missing_admin_manage_config: {
    title: "Insufficient permission",
    message: "This action requires the admin.manage_config permission.",
    hint: "Ask an admin to grant it, or sign in as ADMIN/SUPERADMIN.",
  },
  // Backlog CRUD (2026-07-30). Three separate grants, three separate refusals:
  // an operator who can read but not remove must be told exactly that, not
  // handed a generic "refused" — and never handed a blank list.
  missing_backlog_read: {
    title: "Not permitted to read the backlog",
    message: "Reading the project backlog requires the backlog.read permission.",
    hint: "Ask an admin to grant backlog.read, or sign in with a role that holds it (admin, dev, audit, tester, observer).",
  },
  missing_backlog_write: {
    title: "Not permitted to change the backlog",
    message: "Creating or updating backlog items requires the backlog.write permission.",
    hint: "Ask an admin to grant backlog.write, or sign in as admin/dev.",
  },
  missing_backlog_remove: {
    title: "Not permitted to remove backlog items",
    message: "Removing a backlog item requires the backlog.remove permission — it is destructive, so it sits on the admin tier.",
    hint: "Ask an admin to remove it, or to grant backlog.remove.",
  },
  dashboard_only_setting: {
    title: "Dashboard-only (T0) setting",
    message:
      "This guardrail can only be changed from the Dashboard with explicit " +
      "confirmation — agents and the natural-language path cannot toggle it.",
    hint: "Use the confirm dialog on this setting.",
  },
  dashboard_gate: {
    title: "Config edits disabled",
    message: "Config editing is turned off (security.allow_config_edit=false).",
    hint: "Enable the config-edit toggle in the Dashboard first.",
  },
  session_not_in_project: {
    title: "Session not in this project",
    message:
      "That session is not a member of this project (SQL session_membership " +
      "is the sole authority — a SESSION.md file alone is not).",
    hint: "Pick a session that belongs here, or run migrate-control-authority for legacy sessions.",
  },
  relation_not_approved: {
    title: "Cross-project relation not approved",
    message:
      "The target project is not in security.approved_external_roots — " +
      "cross-project access is refused.",
    hint: "Approve the relation in the Dashboard (Trust) before retrying.",
  },
  target_not_commissioned: {
    title: "Target project not commissioned",
    message: "The target is not a commissioned AIDOCS project.",
    hint: "Commission the target project first.",
  },
  operator_surface: {
    title: "Setting not editable here",
    message:
      "This key is service-managed, deprecated, hidden-owned, or unknown and " +
      "cannot be written through this surface.",
    hint: "Use the owning service's surface (e.g. Governed Bash).",
  },
  confirmation_required: {
    title: "Confirmation needed",
    message:
      "This key is a guardrail, so the write needs an explicit confirmation " +
      "phrase before it will apply. Nothing has changed yet.",
    hint:
      "Click the confirm button once to fill in the phrase, then again to " +
      "apply — or type the phrase shown beside the value.",
  },
  readback_verification: {
    title: "Write did not verify",
    message: "The value did not read back after the write; it was rolled back.",
    hint: "Retry; if it persists, check the store.",
  },
};

/**
 * "A refusal is not an empty result" — the sentence that keeps a gated read
 * from being mistaken for an absent one. Appended to every refusal whose act
 * would otherwise have rendered as blank data.
 */
const NOT_ABSENCE = "This is a refusal, NOT an empty result — the data exists, this session may not read it.";

export function refusalBanner(r: AuthorityResult): Banner | null {
  if (r.ok !== false && !r.blocked_by) return null;
  // PREFER the specific `reason` over the broad `blocked_by` when we have text
  // for it. `blocked_by` is a family ("operator_auth"); `reason` is the actual
  // verdict ("missing_backlog_write"). Telling an authenticated operator to
  // "sign in" because his refusal was filed under operator_auth is a wrong
  // answer that sends him chasing the wrong fix. Falls back to blocked_by, so
  // payloads that carry only that are unchanged.
  // Resolution order: the most SPECIFIC code we have copy for wins. `reason` is
  // the actual verdict, `blocked_by` the broad family — and `error` is a third
  // vocabulary some services answer in (operator_surface.expert_set returns
  // `error: "confirmation_required"`), which was invisible here until 2026-08-03.
  const code =
    (r.reason && REFUSAL_TEXT[r.reason] ? r.reason : undefined) ??
    (r.error && REFUSAL_TEXT[r.error] ? r.error : undefined) ??
    r.blocked_by ??
    r.error ??
    "unknown";
  const t = REFUSAL_TEXT[code];
  if (!t && r.required_permission) {
    // A gate we have no bespoke copy for still names its own remedy rather
    // than degrading to "action refused".
    return {
      title: "Insufficient permission",
      message: `This action requires the ${r.required_permission} permission. ${NOT_ABSENCE}`,
      severity: "danger",
      hint: `Ask an admin to grant ${r.required_permission}, or sign in with a role that holds it.`,
      code,
    };
  }
  if (!t) {
    // A refusal we have no bespoke copy for still SPEAKS THE BACKEND'S OWN
    // WORDS. Preferring `message` over the catch-all is the whole point: the
    // service that refused knows why and usually names the remedy, and
    // rendering "The control plane refused this action" over the top of that
    // destroys the only useful thing in the payload. The generic sentence is
    // the last resort, not the default.
    return {
      title: "Action refused",
      message:
        r.message ||
        r.reason ||
        "The control plane refused this action.",
      severity: "danger",
      hint: r.expected_confirm
        ? `Confirm with the exact phrase: ${r.expected_confirm}`
        : "See the reason code for details.",
      code,
    };
  }
  // When the gate told us WHICH permission it wanted, say so — and say that
  // the absence of data is a policy verdict, not an empty store.
  if (r.required_permission) {
    const named = t.message.includes(r.required_permission)
      ? t.message
      : `${t.message} Required permission: ${r.required_permission}.`;
    return {
      ...t,
      message: `${named} ${NOT_ABSENCE}`,
      severity: "danger",
      code,
    };
  }
  return { ...t, severity: "danger", code };
}

// ── ownership truth: owner_grant + ownership_degraded ────────────────────
export function ownershipNote(r: AuthorityResult): {
  status: string;
  severity: Severity;
  message: string;
} | null {
  const g = (r.owner_grant || "").toLowerCase();
  if (!g) return null;
  if (g === "granted") {
    return {
      status: "granted",
      severity: "info",
      message: "Session owner granted (session_owner role, session-scoped).",
    };
  }
  if (g === "not_required") {
    return {
      status: "not_required",
      severity: "info",
      message: "No owner grant required (local-admin flavor).",
    };
  }
  // failed → degraded
  return {
    status: "failed",
    severity: "warning",
    message:
      r.warning ||
      "Ownership is DEGRADED: the session was created (SQL member) but the " +
        "session_owner grant did not take. Grant the session_owner role at " +
        "session scope via the Dashboard before relying on it.",
  };
}

// ── T0 confirmation dialog text ──────────────────────────────────────────
export function t0ConfirmText(key: string): {
  title: string;
  body: string;
  requiresReason: boolean;
} {
  return {
    title: `Confirm guardrail change: ${key}`,
    body:
      `"${key}" is a dashboard-only (T0) guardrail. Changing it can disable ` +
      `AIDOCS protections. This requires an explicit confirmation and a reason.`,
    requiresReason: true,
  };
}

// ── action result notice (session delete/connect/etc.) ──────────────────
export interface ActionNotice {
  ok: boolean;
  severity: Severity;
  text: string;
}

/**
 * Turn a control-plane ACTION result into a single operator notice, honestly.
 * A refusal (ok===false or any blocked_by) NEVER reads as success — it surfaces
 * the shared refusal banner text (title + message + hint). Use for action
 * results that return a payload rather than throwing (e.g. a refused
 * delete_session returns {ok:false, blocked_by:'operator_auth'}, which must not
 * render as "Deleted"). Ownership degradation is appended when present.
 */
export function actionResultNotice(
  r: AuthorityResult,
  successText: string,
): ActionNotice {
  if (r.ok === false || r.blocked_by) {
    const b = refusalBanner(r);
    const text = b
      ? `${b.title}: ${b.message} ${b.hint}`.trim()
      : r.reason || "Action refused.";
    return { ok: false, severity: "danger", text };
  }
  const note = ownershipNote(r);
  if (note && note.status === "failed") {
    return { ok: true, severity: "warning", text: `${successText} — ${note.message}` };
  }
  return { ok: true, severity: "info", text: successText };
}

// ── audit / event rows: actor · scope · target · status · reason ─────────
export interface AuthorityEvent {
  event_kind?: string;
  status?: string;
  capability_name?: string;
  action_kind?: string;
  target_entity?: string;
  user_id?: string;
  effective_role?: string;
  scope_id?: string;
  payload?: Record<string, unknown> | null;
}

export interface EventAuthorityRow {
  actor: string;
  scope: string;
  target: string;
  status: string;
  statusSeverity: Severity;
  reason: string | null;
  blastRadius: Badge | null;
}

/** Severity for an audit event status, matching backend status vocabulary. */
export function statusSeverity(status: string): Severity {
  const s = (status || "").toLowerCase();
  if (["refused", "failed", "blocked", "rolled_back", "denied"].includes(s)) {
    return "danger";
  }
  if (s === "allowed_degraded" || s === "degraded" || s === "no_op") {
    return "warning";
  }
  return "info"; // applied / allowed / ok / unknown
}

/**
 * Map an audit/event row to the SAME actor/scope/target/status/reason language
 * the backend emitted (control_plane_mutation / config_set rows carry
 * user_id, effective_role, scope_id, status, and payload.reason/blocked_by/
 * blast_radius). Reads from top-level columns first, then the mirrored payload.
 */
export function eventAuthority(e: AuthorityEvent): EventAuthorityRow {
  const p = (e.payload || {}) as Record<string, unknown>;
  const str = (v: unknown): string => (v == null ? "" : String(v));
  const uid = e.user_id || str(p.user_id);
  const role = e.effective_role || str(p.role);
  const actor = uid ? (role ? `${uid} (${role})` : uid) : "system";
  const scope = e.scope_id || str(p.scope_id) || str(p.scope) || "—";
  const target =
    e.target_entity || e.capability_name || e.action_kind || str(p.command) || "—";
  const status = str(e.status) || "unknown";
  const reason = str(p.reason) || str(p.blocked_by) || null;
  const blastRadius = blastRadiusBadge({
    blast_radius: str(p.blast_radius) || undefined,
    broadening: p.broadening === true,
  });
  return {
    actor,
    scope,
    target,
    status,
    statusSeverity: statusSeverity(status),
    reason,
    blastRadius,
  };
}

// ── read-only vs mutating action label (+ T0 badge) ──────────────────────
export interface ActionAuthority {
  kind: "read-only" | "mutating";
  t0: boolean;
  label: string;
  severity: Severity;
}

/**
 * Classify a dashboard action for display. `mutating` actions carry the
 * operator-auth wall; `read-only` ones are open (read-scoped) and must NOT be
 * shown as if they need admin. Mirrors the backend action-gate inventory.
 */
const READ_ONLY_ACTIONS = new Set<string>([
  "auth_status",
  "binding_list",
  "mcp_list",
  "toml_editability",
  "status",
  "list",
  "report",
  "doctor",
]);

export function actionAuthorityLabel(
  action: string,
  opts: { dashboardOnly?: boolean } = {},
): ActionAuthority {
  const readOnly = READ_ONLY_ACTIONS.has(action);
  if (readOnly) {
    return { kind: "read-only", t0: false, label: "Read-only", severity: "info" };
  }
  const t0 = Boolean(opts.dashboardOnly);
  return {
    kind: "mutating",
    t0,
    label: t0 ? "Admin · T0 (confirm)" : "Admin",
    severity: t0 ? "danger" : "warning",
  };
}
