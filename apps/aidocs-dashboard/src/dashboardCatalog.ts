/**
 * Shared host/model catalog for the dashboard's Conductor surfaces.
 *
 * Previously these constants were duplicated byte-for-byte across
 * ConductorPage.tsx and ConductorAgentsPage.tsx — adding a new model
 * or backend required two coordinated edits, and at least one drift
 * window per release. Single-source here; both pages import.
 *
 * The CLI/MCP backend has its own canonical list (cli.py + the conductor
 * routing schema). Keep this catalog visually in sync with that when
 * adding a new host or model; a follow-up could derive it from a
 * runtime fetch (`opencode_models`-style) for full deduplication.
 */

export type DropdownOption = {
  value: string;
  label: string;
};

export const TASK_TYPES: string[] = [
  "implement",
  "refactor",
  "design",
  "test",
  "docs",
  "research",
  "debug",
  "review",
  "deploy",
];

/**
 * Host backends the conductor can route tasks to. The empty-value
 * entry means "default — use the project-level default". The Conductor
 * page filters out the empty entry for its backend selector (only
 * concrete hosts are pickable for conductor start); ConductorAgentsPage
 * keeps it for per-task routing where "default" is meaningful.
 */
export const HOST_OPTIONS: DropdownOption[] = [
  { value: "", label: "default" },
  { value: "claude", label: "Claude Code" },
  { value: "codex", label: "Codex" },
  { value: "opencode", label: "OpenCode" },
];

/**
 * HOST_OPTIONS minus the "default" entry — used by the Conductor page's
 * backend selector where a concrete host MUST be chosen to start.
 */
export const CONDUCTOR_HOST_OPTIONS: DropdownOption[] = HOST_OPTIONS.filter(
  (option) => option.value,
);

export const CLAUDE_MODEL_OPTIONS: DropdownOption[] = [
  { value: "", label: "default" },
  { value: "claude-opus-4-6", label: "Opus 4.6" },
  { value: "claude-sonnet-4-6", label: "Sonnet 4.6" },
  { value: "opus", label: "Opus (alias)" },
  { value: "sonnet", label: "Sonnet (alias)" },
];

export const CODEX_MODEL_OPTIONS: DropdownOption[] = [
  { value: "", label: "default" },
  { value: "gpt-5.4", label: "GPT-5.4" },
  { value: "gpt-5.3-codex", label: "GPT-5.3 Codex" },
  { value: "gpt-5-codex", label: "GPT-5 Codex" },
  { value: "o3", label: "o3" },
];

export const THINK_MODE_OPTIONS: DropdownOption[] = [
  { value: "off", label: "off" },
  { value: "low", label: "low" },
  { value: "medium", label: "medium" },
  { value: "high", label: "high" },
];
