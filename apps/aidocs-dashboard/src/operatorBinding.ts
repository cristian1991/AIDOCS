// Pure helpers for the host-operator bindings surface (Empire directive
// 2026-07-17: 1 dashboard = 1 user = bind). Kept side-effect-free so it can be
// unit-tested and reused by the poll loop.
import type { HostBindingRow } from "./dashboardApi";

// AUTO-BIND REMOVED 2026-08-27 (#559) by operator ruling, verbatim: "559. no,
// remove it, no auto-bind."
//
// This module used to export AUTO_BIND_HOST_KIND, AUTO_BIND_SETTING_PATH,
// AutoBindContext, selectAutoBindable, readAutoBindSetting and
// autoBindToggleWrite. The settings key they wrote —
// `dashboard.auto_bind_local_sessions` — had ZERO backend readers and is now
// gone from SETTINGS_CATALOG and tombstoned in _REMOVED_SETTINGS. Leaving the
// TypeScript behind would have re-created the original defect in mirror image:
// a UI writing a key `config_set` refuses as unknown_setting, reporting success
// to the operator either way.
//
// Binding a new local session is now what it always was underneath — the
// one-click "Bind to me" in BindingsPanel, which rides the same audited approve
// path. There is deliberately no standing grant.

/** Count of pending bindings — drives the "new pending" badge/notification. */
export function pendingCount(bindings: readonly HostBindingRow[] | null | undefined): number {
  return (bindings ?? []).filter((b) => b.status === "pending").length;
}
