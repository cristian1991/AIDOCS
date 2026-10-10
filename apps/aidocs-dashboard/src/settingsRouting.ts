/**
 * settingsRouting — pure (no React/DOM) classification + save-routing the
 * Settings UI uses to decide which keys may appear as NORMAL editable rows
 * and how each key must be written.
 *
 * The backend (operator_surface) is the source of truth: it returns the
 * catalog already split into `normal` and `advanced_raw`. These helpers
 * mirror that rule on the client so the UI can never accidentally render
 * or save a guardrail key as a normal row, and so the save path is routed
 * to the correct operator-surface API:
 *
 *   service-managed → owning profile action (operatorSurfaceApply)
 *   deprecated      → blocked (read-only; show migration message)
 *   dashboard-only / security-sensitive → Advanced Raw expert path
 *                     (operatorSurfaceExpertSet, exact confirmation)
 *   everything else → a normal config save
 */

export interface SettingRowFlags {
  key: string;
  service_managed?: string | null;
  deprecated?: string | null;
  dashboard_only?: boolean;
  security_sensitive?: boolean;
}

export type SaveRoute = "normal" | "expert" | "profile" | "blocked";

/**
 * Static mirror of the backend's service-managed + deprecated +
 * hidden-owned keys. Used to fail CLOSED: if operator_surface_rows cannot
 * load, the UI still hides these (and disables saves), so a guardrail key
 * — even a hidden-owned NON-dashboard one like
 * tools.shell_policy_shadow_enabled — can never appear or save as a normal
 * row. Keep in sync with operator_surface._PROFILES (a backend test pins
 * the catalog; this is the client's last line of defense).
 */
export const KNOWN_GUARDRAIL_KEYS: ReadonlySet<string> = new Set<string>([
  // Governed Bash — service-managed
  "tools.shell_enforcement_live",
  "tools.native_shell_provider_enabled",
  "tools.native_shell_readonly_enabled",
  "tools.native_shell_provider_path",
  "tools.native_shell_trusted_roots",
  "tools.native_shell_provider_sha256",
  "tools.native_shell_require_os_signature",
  // Governed Bash — hidden-owned low-level flags (NOT all dashboard-only)
  "tools.native_shell_readonly_extra_commands",
  "tools.shell_lifecycle_preflight_enforce",
  "tools.shell_disconnect_after_seconds",
  "tools.shell_policy_shadow_enabled",
  // Deprecated / reserved aliases
  "security.allow_raw_shell",
  "security.repeated_violation_freeze_threshold",
]);

/** A key that must NOT appear as a normal editable Settings row — it
 * belongs in Advanced Raw diagnostics. Mirrors
 * operator_surface.is_advanced_only_key. */
export function isAdvancedOnly(r: SettingRowFlags): boolean {
  return Boolean(
    r.service_managed ||
      r.deprecated ||
      r.dashboard_only ||
      KNOWN_GUARDRAIL_KEYS.has(r.key),
  );
}

/** Snapshot-entry flags the Settings table has WITHOUT operator surface. */
export interface SnapshotEntryFlags {
  path: string;
  dashboard_only?: boolean;
  is_t0?: boolean;
  security_sensitive?: boolean;
}

/**
 * Whether a snapshot entry may render as a NORMAL editable row. Fails
 * closed: when the operator surface rows have not loaded
 * (``rowsLoaded === false``), security-sensitive keys are ALSO hidden, on
 * top of the always-hidden known-guardrail / dashboard-only / T0 / backend
 * advanced keys. This is what guarantees a hidden-owned non-dashboard key
 * stays out of normal rows even if operatorSurfaceRows() failed.
 */
export function isNormalRowAllowed(
  entry: SnapshotEntryFlags,
  opts: { rowsLoaded: boolean; advancedKeys: Set<string> },
): boolean {
  if (KNOWN_GUARDRAIL_KEYS.has(entry.path)) return false;
  if (entry.dashboard_only || entry.is_t0) return false;
  if (opts.advancedKeys.has(entry.path)) return false;
  if (!opts.rowsLoaded && entry.security_sensitive) return false;
  return true;
}

/** The exact phrase an operator must echo to apply a dangerous profile —
 * mirrors operator_surface.profile_confirm_token. */
export function profileConfirmToken(profileId: string): string {
  return `confirm-apply ${profileId}`;
}

/**
 * Split the dirty (drafted) entries into the ones a normal Save All may
 * persist and the ones that must be QUARANTINED. Save All must operate on
 * this `savable` list only — never the raw entry list — so a draft made
 * for a guardrail key (e.g. before the operator surface loaded, or via a
 * stale selectedPath) can never ride through the normal save path. A
 * quarantined draft must instead go through the expert / profile action,
 * or be surfaced to the operator as refused.
 */
export function partitionDirtyForSave<T extends SnapshotEntryFlags>(
  dirty: T[],
  opts: { rowsLoaded: boolean; advancedKeys: Set<string> },
): { savable: T[]; quarantined: T[] } {
  const savable: T[] = [];
  const quarantined: T[] = [];
  for (const e of dirty) {
    if (isNormalRowAllowed(e, opts)) savable.push(e);
    else quarantined.push(e);
  }
  return { savable, quarantined };
}

export function isNormalEditable(r: SettingRowFlags): boolean {
  return !isAdvancedOnly(r);
}

/** Which write path the UI must use for this key. */
export function saveRouteFor(r: SettingRowFlags): SaveRoute {
  if (r.service_managed) return "profile"; // must go through owning profile
  if (r.deprecated) return "blocked"; // read-only; migration only
  if (r.dashboard_only || r.security_sensitive) return "expert";
  return "normal";
}

/** Split a flat list of rows into the two surfaces the UI renders. */
export function partitionRows<T extends SettingRowFlags>(
  rows: T[],
): { normal: T[]; advanced: T[] } {
  const normal: T[] = [];
  const advanced: T[] = [];
  for (const r of rows) {
    if (isAdvancedOnly(r)) advanced.push(r);
    else normal.push(r);
  }
  return { normal, advanced };
}

/** Build the set of keys that must be hidden from the normal table. The UI
 * derives this from the backend's authoritative advanced_raw list. */
export function advancedKeySet(advancedRows: SettingRowFlags[]): Set<string> {
  return new Set(advancedRows.map((r) => r.key));
}

/** Hard invariant the Settings UI must never violate: no guardrail key may
 * be present in the normal-rows list. Throws (in dev/test) if one leaks. */
export function assertNoForbiddenInNormal(normal: SettingRowFlags[]): void {
  for (const r of normal) {
    if (isAdvancedOnly(r)) {
      throw new Error(
        `settings invariant violated: advanced-only key '${r.key}' ` +
          `appeared in normal Settings rows`,
      );
    }
  }
}
