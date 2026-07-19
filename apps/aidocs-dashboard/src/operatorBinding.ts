// Pure decision logic for host-operator binding auto-approval (Empire directive
// 2026-07-17: 1 dashboard = 1 user = bind). Kept side-effect-free so it can be
// unit-tested and reused by the poll loop.
import type { HostBindingRow } from "./dashboardApi";

export const AUTO_BIND_HOST_KIND = "claude_code";
/** Config key persisted via the existing config surface (config_get/config_set). */
export const AUTO_BIND_SETTING_PATH = "dashboard.auto_bind_local_sessions";

export type AutoBindContext = {
  /** A valid operator token resolves (fail-closed: false => no auto-approve). */
  authenticated: boolean;
  /** The settings toggle "Auto-bind new local sessions while logged in". */
  enabled: boolean;
};

/**
 * Given the current bindings ledger (already project-scoped — the CLI reads the
 * dashboard project's own store) and context, return the binding_ids that
 * SHOULD be auto-approved right now: pending, host_kind=claude_code, only when
 * logged in AND the toggle is ON. Fail-closed on either gate.
 *
 * project_root match is implicit: `bindings` only ever returns rows for the
 * dashboard's own project, so a pending for another project is never in the
 * list — auto-approve can never silently reach across projects.
 */
export function selectAutoBindable(
  bindings: readonly HostBindingRow[] | null | undefined,
  ctx: AutoBindContext,
): string[] {
  if (!ctx.authenticated || !ctx.enabled) return [];
  return (bindings ?? [])
    .filter((b) => b.status === "pending" && b.host_kind === AUTO_BIND_HOST_KIND)
    .map((b) => b.binding_id);
}

/** Count of pending bindings — drives the "new pending" badge/notification. */
export function pendingCount(bindings: readonly HostBindingRow[] | null | undefined): number {
  return (bindings ?? []).filter((b) => b.status === "pending").length;
}

/**
 * Read the auto-bind toggle from the effective config map. Default OFF
 * (fail-closed): absent/unknown values never enable auto-approval.
 */
export function readAutoBindSetting(
  effective: Record<string, unknown> | null | undefined,
): boolean {
  const v = effective?.[AUTO_BIND_SETTING_PATH];
  return v === true || v === "true" || v === 1 || v === "1";
}

/** Map the toggle to the config-write pair used by the settings surface. */
export function autoBindToggleWrite(enabled: boolean): {
  settingPath: string;
  value: boolean;
} {
  return { settingPath: AUTO_BIND_SETTING_PATH, value: enabled };
}
