import type {
  DashboardConfigEntry,
  DashboardSeriesItem,
  DashboardSnapshot,
  DashboardTomlDocument,
  SettingValueFormat,
} from "./dashboardApi";

export type NavKey = "overview" | "sessions" | "backlog" | "conductor" | "conductor_agents" | "execution" | "settings" | "config_toml" | "usage" | "registry" | "monitoring" | "skills" | "rbac" | "shell_policy" | "ref_integrity" | "memory" | "updates";
export type TomlCategory = "intent_tokens" | "gate_messages" | "language_descriptors";
export type SettingsScope = "global" | "project" | "session";

export type DropdownOption = {
  value: string;
  label: string;
  subtitle?: string;
};

export const navigation: Array<{ name: string; value: NavKey }> = [
  { name: "Overview", value: "overview" },
  { name: "Sessions", value: "sessions" },
  // Backlog + Todos. Nav entry added 2026-07-30: the page shipped with a
  // hash-only route (#backlog-todo) that nothing ever set, so on desktop —
  // where there is no address bar — the operator's own backlog was
  // unreachable. See navReachability.test.ts.
  { name: "Backlog", value: "backlog" },
  { name: "Conductor", value: "conductor" },
  { name: "Monitoring", value: "execution" },
  { name: "Usage", value: "usage" },
  { name: "MCP", value: "registry" },
];

export const globalNavigation: Array<{ name: string; value: NavKey }> = [
  { name: "Agents", value: "conductor_agents" },
  { name: "Memory", value: "memory" },
  { name: "Skills", value: "skills" },
  { name: "Settings", value: "settings" },
  { name: "Roles & Users", value: "rbac" },
  { name: "TOML Configs", value: "config_toml" },
  { name: "Ref Integrity", value: "ref_integrity" },
];

export function asText(value: unknown): string {
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

/** The catalog facts a typed settings control needs. Structural, so both the
 * normal rows (DashboardConfigEntry, from the snapshot) and the protected
 * editor (operator_surface.inspect_key rows) can be validated by one rule. */
export type SettingTypeInfo = {
  type: string;
  allowed_values?: string[] | null;
  value_format?: SettingValueFormat | string | null;
  minimum?: number | null;
  maximum?: number | null;
};

function splitList(raw: string): string[] {
  return raw
    .split(/[\n,]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

const DECIMAL_RE = /^-?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$/;

/**
 * Why a draft is not a valid value for this setting, or null when it is.
 *
 * Presentation-side validation only: it keeps the editor from sending a value
 * of the wrong shape. Whether the write is ALLOWED stays the server's call
 * (config_set: RBAC, the dashboard config-edit switch, user intent).
 */
export function validateSettingDraft(info: SettingTypeInfo, rawValue: string): string | null {
  const value = rawValue.trim();
  if (info.type === "integer") {
    if (!/^-?\d+$/.test(value)) return "Must be a whole number.";
    const n = Number.parseInt(value, 10);
    // Beyond 2^53-1 parseInt rounds ('9007199254740993' -> ...992), so a value
    // that looks valid would be saved as a DIFFERENT number. Refuse it.
    if (!Number.isSafeInteger(n)) return "Whole number is out of range.";
    if (info.minimum != null && n < info.minimum) return `Must be at least ${info.minimum}.`;
    if (info.maximum != null && n > info.maximum) return `Must be at most ${info.maximum}.`;
    return null;
  }
  if (info.type === "boolean") {
    return value === "true" || value === "false" ? null : "Must be true or false.";
  }
  if (info.type === "string_list") return null;
  if (info.allowed_values?.length && !info.allowed_values.includes(rawValue)) {
    return `Must be one of: ${info.allowed_values.join(", ")}.`;
  }
  if (info.value_format === "json_object" || info.value_format === "json_array") {
    let parsed: unknown;
    try {
      parsed = JSON.parse(value);
    } catch {
      return "Must be valid JSON.";
    }
    if (info.value_format === "json_array") {
      return Array.isArray(parsed) ? null : "Must be a JSON array, e.g. [].";
    }
    return parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)
      ? null
      : "Must be a JSON object, e.g. {}.";
  }
  if (info.value_format === "number") {
    return DECIMAL_RE.test(value) && Number.isFinite(Number(value)) ? null : "Must be a number, e.g. 0.85.";
  }
  return null;
}

/** A draft in comparable form, so a list typed one-per-line equals the same
 * list stored as "a, b" (no false "unsaved"). Other types compare as text. */
export function canonicalSettingText(info: SettingTypeInfo, rawValue: string): string {
  if (info.type === "string_list") return splitList(rawValue).join(", ");
  if (info.type === "integer" || info.type === "boolean") return rawValue.trim();
  return rawValue;
}

export function parseEntryValue(entry: DashboardConfigEntry, rawValue: string): string | number | boolean | string[] {
  const problem = validateSettingDraft(entry, rawValue);
  if (problem) {
    throw new Error(`${entry.path}: ${problem}`);
  }
  const normalized = rawValue.trim();
  if (entry.type === "integer") return Number.parseInt(normalized, 10);
  if (entry.type === "boolean") return normalized === "true";
  if (entry.type === "string_list") return splitList(rawValue);
  return rawValue;
}

export function scaleRows(items: DashboardSeriesItem[]) {
  const max = Math.max(...items.map((item) => item.count), 1);
  return items.map((item) => ({
    ...item,
    width: `${Math.max((item.count / max) * 100, 8)}%`,
  }));
}

// CLOCK-TRUTH (2026-05-26): event timestamps land in the DB as UTC ISO
// strings (e.g. "2026-05-26T16:21:53Z"). LivePage and LaneDetailPanel
// used to render the time column via `observed_at.slice(11, 19)` — a
// raw substring that returns the UTC hours verbatim and never converts
// to the operator's local timezone. For a UTC+3 operator this surfaced
// 16:21 in the dashboard while the OS clock read 19:21 — a 3-hour
// silent drift labelled as "now". This helper parses the ISO string
// and renders local `HH:MM:SS` via Intl, so the live feed shows the
// same wall-clock time the operator reads from their OS. Falls back to
// the raw slice only when the value is not a parseable date — exactly
// the previous behavior, so unknown formats degrade identically.
export function formatTimeOfDay(value: string | null | undefined): string {
  if (!value) return "";
  const d = new Date(value);
  if (isNaN(d.getTime())) return value.slice(11, 19);
  return d.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

export function formatTimestamp(value: string | null | undefined): string {
  if (!value) return "n/a";
  try {
    const d = new Date(value);
    if (isNaN(d.getTime())) return value.replace("T", " ");
    const now = new Date();
    const diffMs = now.getTime() - d.getTime();
    if (diffMs < 60_000) return `${Math.floor(diffMs / 1000)}s ago`;
    if (diffMs < 3_600_000) return `${Math.floor(diffMs / 60_000)}m ago`;
    if (diffMs < 86_400_000) return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    return d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  } catch {
    return value.replace("T", " ");
  }
}

export function parseProgressPercent(progress: string | null | undefined): number {
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

export function buildSettingTooltip(entry: DashboardConfigEntry, value: string): string {
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

export function isDashboardEditable(entry: DashboardConfigEntry): boolean {
  // Dashboard is the user, not an agent — security_sensitive settings are editable here
  return entry.editable || entry.security_sensitive;
}

export function readAidocsVersion(snapshot: DashboardSnapshot | null): string {
  const entry = snapshot?.config.entries.find((item) => item.path === "global.aidocs_core_version");
  if (entry) {
    return String(entry.current_value ?? entry.default ?? "unknown");
  }
  return "unknown";
}

export function isDocumentActive(document: DashboardTomlDocument): boolean {
  return !/not selected|inactive|disabled/i.test(document.active);
}
