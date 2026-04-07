import type {
  DashboardConfigEntry,
  DashboardSeriesItem,
  DashboardSnapshot,
  DashboardTomlDocument,
} from "./dashboardApi";

export type NavKey = "overview" | "sessions" | "conductor" | "execution" | "settings" | "config_toml" | "usage" | "registry" | "monitoring" | "skills";
export type TomlCategory = "action_tokens" | "action_hooks" | "language_descriptors";
export type SettingsScope = "global" | "project" | "session";

export type DropdownOption = {
  value: string;
  label: string;
  subtitle?: string;
};

export const navigation: Array<{ name: string; value: NavKey }> = [
  { name: "Overview", value: "overview" },
  { name: "Sessions", value: "sessions" },
  { name: "Conductor", value: "conductor" },
  { name: "Monitoring", value: "execution" },
  { name: "Usage", value: "usage" },
  { name: "MCP", value: "registry" },
];

export const globalNavigation: Array<{ name: string; value: NavKey }> = [
  { name: "Skills", value: "skills" },
  { name: "Settings", value: "settings" },
  { name: "TOML Configs", value: "config_toml" },
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

export function parseEntryValue(entry: DashboardConfigEntry, rawValue: string): string | number | boolean | string[] {
  const normalized = rawValue.trim();
  if (entry.type === "integer") {
    const parsed = Number.parseInt(normalized, 10);
    if (!Number.isFinite(parsed)) {
      throw new Error(`${entry.path} requires an integer value.`);
    }
    return parsed;
  }
  if (entry.type === "boolean") {
    return normalized === "true";
  }
  if (entry.type === "string_list") {
    return rawValue
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);
  }
  return rawValue;
}

export function scaleRows(items: DashboardSeriesItem[]) {
  const max = Math.max(...items.map((item) => item.count), 1);
  return items.map((item) => ({
    ...item,
    width: `${Math.max((item.count / max) * 100, 8)}%`,
  }));
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
