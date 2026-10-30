/**
 * settingsModel — pure Settings-page logic: grouping, ordering, search,
 * counts, draft lifetime and profile-card values. Kept out of the
 * components so each rule is testable without rendering.
 */
import type { DashboardConfigEntry } from "./dashboardApi";
import { asText, canonicalSettingText, parseEntryValue } from "./dashboardUtils";

/** The one name for settings whose catalog section is blank. */
export const GENERAL_SECTION = "general";

/** Sections that change enforcement; listed after the everyday ones. */
const RISKY_SECTIONS = new Set(["dev", "gate", "security", "rbac", "audit", "policies"]);

/** Shown in the dashboard header instead, never as a setting. */
const HIDDEN_PATHS = new Set(["global.aidocs_core_version"]);

export function sectionName(entry: { section?: string | null }): string {
  return (entry.section ?? "").trim() || GENERAL_SECTION;
}

function compareSections(a: string, b: string): number {
  const ar = RISKY_SECTIONS.has(a) ? 1 : 0;
  const br = RISKY_SECTIONS.has(b) ? 1 : 0;
  return ar - br || a.localeCompare(b);
}

export function isSettableAt(entry: DashboardConfigEntry, scope: string): boolean {
  return entry.allowed_scopes.includes(scope);
}

/**
 * Every setting, grouped and ordered. A setting that cannot be set at the
 * current scope is still listed (the page greys it out): dropping it made
 * settings vanish whenever the scope tab changed.
 */
export function buildConfigSections(
  entries: DashboardConfigEntry[],
): Array<{ section: string; entries: DashboardConfigEntry[] }> {
  const groups = new Map<string, DashboardConfigEntry[]>();
  for (const entry of entries) {
    if (HIDDEN_PATHS.has(entry.path)) continue;
    const name = sectionName(entry);
    groups.set(name, [...(groups.get(name) ?? []), entry]);
  }
  return Array.from(groups.entries())
    .sort(([a], [b]) => compareSections(a, b))
    .map(([section, list]) => ({
      section,
      entries: [...list].sort((x, y) => x.key.localeCompare(y.key) || x.path.localeCompare(y.path)),
    }));
}

/** True when any field contains the term (case-insensitive); a blank term matches. */
export function matchesTerm(fields: Array<string | null | undefined>, term: string): boolean {
  const t = term.trim().toLowerCase();
  if (!t) return true;
  return fields.some((f) => (f ?? "").toLowerCase().includes(t));
}

/** Per-section counts of the settings the table can actually show. */
export function categoryCounts(
  shown: DashboardConfigEntry[],
): Array<{ name: string; count: number; isDanger: boolean }> {
  const counts = new Map<string, number>();
  for (const e of shown) {
    const name = sectionName(e);
    counts.set(name, (counts.get(name) ?? 0) + 1);
  }
  return Array.from(counts.entries())
    .sort(([a], [b]) => compareSections(a, b))
    .map(([name, count]) => ({ name, count, isDanger: RISKY_SECTIONS.has(name) }));
}

/** A drive (C:/) or UNC (//server/share) root: Windows, case-insensitive. */
const WINDOWS_ROOT = /^([a-z]:|\/\/)/i;

/**
 * A project root in comparable form: one separator, no trailing slash, and
 * case folded ONLY for Windows roots. On POSIX /srv/Proj and /srv/proj are
 * different folders, so their case is kept.
 */
export function normRoot(p: string | null | undefined): string {
  const s = (p ?? "").replace(/\\/g, "/").replace(/\/+$/, "");
  return WINDOWS_ROOT.test(s) ? s.toLowerCase() : s;
}

export function sameProjectRoot(a: string | null | undefined, b: string | null | undefined): boolean {
  return normRoot(a) === normRoot(b);
}

/**
 * Where a settings write may go: the project the LOADED snapshot belongs to,
 * and only once it is the project picked in the header. null = refuse, so a
 * value read from one project is never written into another mid-switch.
 */
export function writeTargetRoot(
  picked: string | null | undefined,
  loaded: string | null | undefined,
): string | null {
  if (!loaded) return null;
  if (picked && !sameProjectRoot(picked, loaded)) return null;
  return loaded;
}

export type LoadTarget = { project: string | undefined; session: string | undefined };

/** Unsaved edits survive a refresh of the same project and session; a switch drops them. */
export function draftsSurviveReload(prev: LoadTarget | null, next: LoadTarget): boolean {
  return prev !== null && prev.project === next.project && prev.session === next.session;
}

/** A profile card's form, prefilled from each member's current value. */
export function profileFormValues(
  keys: string[],
  entries: DashboardConfigEntry[],
): Record<string, string> {
  const byPath = new Map(entries.map((e) => [e.path, e]));
  return Object.fromEntries(
    keys.map((k) => {
      const e = byPath.get(k);
      if (!e) return [k, ""];
      const v = e.current_value ?? e.default;
      return [k, v === undefined || v === null ? "" : asText(v)];
    }),
  );
}

/** The apply payload: only members the operator changed, typed by the catalog. */
export function profileValuesJson(
  form: Record<string, string>,
  start: Record<string, string>,
  entries: DashboardConfigEntry[],
): string {
  const byPath = new Map(entries.map((e) => [e.path, e]));
  const out: Record<string, unknown> = {};
  for (const [k, raw] of Object.entries(form)) {
    const e = byPath.get(k);
    const before = start[k] ?? "";
    if (e ? canonicalSettingText(e, raw) === canonicalSettingText(e, before) : raw === before) continue;
    out[k] = e ? parseEntryValue(e, raw) : raw;
  }
  return JSON.stringify(out);
}
