import type {
  DashboardConfigEntry,
  DashboardSeriesItem,
  DashboardSnapshot,
  DashboardTomlDocument,
} from "./dashboardApi";

export type NavKey = "overview" | "sessions" | "conductor" | "execution" | "settings" | "usage";
export type SettingsView = "typed" | "documents";

export type DropdownOption = {
  value: string;
  label: string;
  subtitle?: string;
};

export const navigation: Array<{ name: string; value: NavKey }> = [
  { name: "Overview", value: "overview" },
  { name: "Sessions", value: "sessions" },
  { name: "Conductor", value: "conductor" },
  { name: "Execution", value: "execution" },
  { name: "Settings", value: "settings" },
  { name: "Usage", value: "usage" },
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

export function parseEntryValue(entry: DashboardConfigEntry, rawValue: string): unknown {
  if (entry.type === "integer") {
    return Number.parseInt(rawValue, 10);
  }
  if (entry.type === "boolean") {
    return rawValue === "true";
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
  if (!value) {
    return "n/a";
  }
  return value.replace("T", " ");
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
  return entry.editable || entry.path === "dev.dev_mode";
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
