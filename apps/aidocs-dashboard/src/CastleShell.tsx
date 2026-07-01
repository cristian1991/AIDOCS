/**
 * CastleShell — Phase 6b (2026-05-02).
 *
 * The new dashboard's outer chrome: left rail with grouped nav,
 * top strip with command palette + project/session selectors,
 * main pane (children), right context rail (children), bottom
 * status bar.
 *
 * Design language: Tailwind utilities + semantic castle palette
 * (allow=emerald, deny=red, warn=amber, info=cyan, flow=violet,
 * mute=slate). Dense layout, keyboard-first hooks, dark-only.
 *
 * Pages render inside <CastleShell>...</CastleShell> as children.
 * The shell is presentational — it doesn't own page state. App.tsx
 * still owns active-nav, project/session selection, snapshot data;
 * passes them in via props.
 */
import { useEffect, useState } from "react";
import {
  Activity,
  AlertTriangle,
  Archive,
  BookOpen,
  Boxes,
  BrainCircuit,
  Command,
  FileCode2,
  GitFork,
  Gauge,
  KeyRound,
  MonitorDot,
  Network,
  RefreshCcw,
  Search,
  Settings as SettingsIcon,
  ShieldCheck,
  TerminalSquare,
  UserCog,
} from "lucide-react";
import type { NavKey } from "./dashboardUtils";
import logoUrl from "./cn-logo.svg";
import { invoke } from "@tauri-apps/api/core";
import { WebmcpModePanel } from "./WebmcpModePanel";
import { isWebBuild, loadGateConnection } from "./webmcpScope";

// ── Nav definition — grouped, semantic ─────────────────────────────

type NavGroup = {
  label: string;
  items: Array<{
    key: NavKey;
    label: string;
    Icon: React.ComponentType<{ className?: string }>;
    badge?: number;
  }>;
};

export type CastleNavCounts = {
  approvals?: number;
  incidents?: number;
  pending_escalations?: number;
};

function buildNavGroups(counts: CastleNavCounts): NavGroup[] {
  return [
    {
      label: "Control",
      items: [
        { key: "overview", label: "Live", Icon: Gauge },
        { key: "sessions", label: "Sessions", Icon: Archive },
        { key: "conductor", label: "Conductor", Icon: Command },
      ],
    },
    {
      label: "Operations",
      items: [
        { key: "execution", label: "Events", Icon: MonitorDot },
        {
          key: "rbac",
          label: "Approvals",
          Icon: KeyRound,
          badge: counts.pending_escalations,
        },
        {
          key: "monitoring",
          label: "Incidents",
          Icon: AlertTriangle,
          badge: counts.incidents,
        },
      ],
    },
    {
      label: "Config",
      items: [
        { key: "settings", label: "Settings", Icon: SettingsIcon },
        { key: "shell_policy", label: "Shell Policy", Icon: TerminalSquare },
        { key: "skills", label: "Skills", Icon: BrainCircuit },
        { key: "memory", label: "Memory", Icon: GitFork },
        { key: "conductor_agents", label: "Agents", Icon: Network },
        { key: "config_toml", label: "TOML Configs", Icon: FileCode2 },
      ],
    },
    {
      label: "System",
      items: [
        { key: "usage", label: "Usage", Icon: Activity },
        { key: "registry", label: "MCP", Icon: Boxes },
      ],
    },
  ];
}

// ── Top strip ───────────────────────────────────────────────────────

export type CastleTopStripProps = {
  projectLabel?: string;
  projectPath?: string;
  sessionLabel?: string;
  managedMode?: boolean;
  onCommandPalette?: () => void;
  onRefresh?: () => void;
  /** Replaces the read-only project pill when provided. */
  projectSelector?: React.ReactNode;
  /** Replaces the read-only session pill when provided. */
  sessionSelector?: React.ReactNode;
  /** Extra slot rendered between session and refresh — for
   * project-add / quick-action triggers. */
  extras?: React.ReactNode;
};

function CastleTopStrip({
  projectLabel,
  projectPath,
  sessionLabel,
  managedMode,
  onCommandPalette,
  onRefresh,
  projectSelector,
  sessionSelector,
  extras,
}: CastleTopStripProps) {
  return (
    <div className="flex h-14 items-center gap-3 border-b border-castle-line bg-castle-panel/85 px-4">
      <button
        type="button"
        onClick={onCommandPalette}
        className="flex flex-1 items-center gap-3 rounded-xl border border-castle-line bg-black/20 px-3 py-2 text-left transition hover:bg-black/30"
        title="Open command palette (Ctrl-K)"
      >
        <Search className="h-4 w-4 text-castle-mute" />
        <span className="text-sm text-castle-mute">Command palette</span>
        <span className="ml-auto rounded-md border border-castle-line bg-white/[0.04] px-2 py-0.5 text-[11px] text-castle-mute">
          Ctrl K
        </span>
      </button>
      {projectSelector ? (
        <div className="hidden md:block">{projectSelector}</div>
      ) : (
        <div className="hidden min-w-[230px] rounded-xl border border-castle-line bg-black/20 px-3 py-2 md:block" title={projectPath}>
          <div className="text-[9px] font-bold uppercase tracking-widest text-castle-mute">Project</div>
          <div className="truncate text-sm text-slate-200">{projectLabel || projectPath || "—"}</div>
        </div>
      )}
      {sessionSelector ? (
        <div className="hidden lg:block">{sessionSelector}</div>
      ) : (
        <div className="hidden min-w-[230px] rounded-xl border border-castle-line bg-black/20 px-3 py-2 lg:block">
          <div className="text-[9px] font-bold uppercase tracking-widest text-castle-mute">Session</div>
          <div className="truncate text-sm text-castle-allow">{sessionLabel || "—"}</div>
        </div>
      )}
      {extras && <div className="flex items-center gap-2">{extras}</div>}
      <WebmcpModePanel />
      <button
        type="button"
        onClick={onRefresh}
        className="grid h-10 w-10 place-items-center rounded-xl border border-castle-line bg-white/[0.035] text-castle-mute hover:bg-white/[0.07] hover:text-slate-200"
        title="Refresh snapshot (Ctrl-R)"
      >
        <RefreshCcw className="h-4 w-4" />
      </button>
    </div>
  );
}

// ── Left rail ───────────────────────────────────────────────────────

export type CastleNavProps = {
  active: NavKey;
  onSelect: (key: NavKey) => void;
  counts?: CastleNavCounts;
  managedMode?: boolean;
  version?: string;
};

function CastleNavLeft({
  active,
  onSelect,
  counts = {},
  managedMode,
  version,
}: CastleNavProps) {
  const groups = buildNavGroups(counts);
  return (
    <aside className="flex w-[220px] shrink-0 flex-col border-r border-castle-line bg-black/25">
      <div
          onClick={() =>
            invoke("open_url", { url: "https://codenexus.cloud" }).catch(() =>
              window.open("https://codenexus.cloud", "_blank"),
            )
          }
          role="button"
          title="Open CodeNexus.cloud"
          className="flex cursor-pointer items-center gap-3 border-b border-castle-line px-4 py-4 transition hover:bg-white/[0.03]">
        <div className="relative grid h-9 w-9 place-items-center rounded-lg border border-castle-allow/50 bg-castle-bg shadow-castle-glow">
          <div className="absolute inset-0.5 rounded-md border border-castle-allow/20" />
          <img src={logoUrl} alt="CodeNexus" className="h-6 w-6" />
        </div>
        <div className="leading-tight">
          <div className="text-sm font-black tracking-tight text-white">
            AIDOCS
          </div>
          <div className="text-[10px] uppercase tracking-widest text-castle-mute">
            Empire console
          </div>
        </div>
      </div>
      <nav className="mt-3 flex-1 overflow-y-auto px-3 pb-3">
        {groups.map((group) => (
          <div key={group.label} className="mt-3 first:mt-0">
            <div className="mb-1 px-2 text-[10px] font-black uppercase tracking-widest text-castle-mute">
              {group.label}
            </div>
            {group.items.map(({ key, label, Icon, badge }) => {
              const isActive = active === key;
              return (
                <button
                  key={key}
                  type="button"
                  onClick={() => onSelect(key)}
                  className={
                    "group mb-0.5 flex h-9 w-full items-center gap-3 rounded-lg border px-3 text-sm transition " +
                    (isActive
                      ? "border-castle-allow/35 bg-castle-allow/10 text-white"
                      : "border-transparent text-castle-mute hover:border-castle-line hover:bg-white/[0.035] hover:text-slate-200")
                  }
                >
                  <Icon
                    className={
                      "h-4 w-4 shrink-0 " +
                      (isActive
                        ? "text-castle-allow"
                        : "text-castle-mute group-hover:text-slate-300")
                    }
                  />
                  <span className="truncate text-left">{label}</span>
                  {badge !== undefined && badge > 0 && (
                    <span
                      className={
                        "ml-auto rounded-full px-2 py-0.5 text-[10px] font-bold " +
                        (key === "rbac"
                          ? "bg-castle-warn/20 text-castle-warn"
                          : "bg-castle-deny/20 text-castle-deny")
                      }
                    >
                      {badge}
                    </span>
                  )}
                </button>
              );
            })}
          </div>
        ))}
      </nav>
      {version && (
        <div className="mx-3 mb-3 rounded-xl border border-castle-line bg-black/25 px-3 py-2">
          <div className="text-[9px] font-bold uppercase tracking-widest text-castle-mute">
            Version
          </div>
          <div className="mt-0.5 font-mono text-xs text-slate-300">{version}</div>
        </div>
      )}
    </aside>
  );
}

// ── Status bar ──────────────────────────────────────────────────────

export type CastleStatusBarProps = {
  mcpConnected: boolean;
  projectName?: string;
  sessionId?: string;
  activeLayer?: string;
  saveState?: string;
};

function CastleStatusBar({
  mcpConnected,
  projectName,
  sessionId,
  activeLayer,
  saveState,
}: CastleStatusBarProps) {
  // In the web build the dashboard's live link is the WebAgent GATE (a valid bearer
  // session), not a local managed conductor, so report THAT truthfully instead of
  // a misleading red "MCP disconnected". (mcpConnected = local managed mode.)
  const web = isWebBuild();
  const connected = web ? !!loadGateConnection() : mcpConnected;
  const connLabel = web
    ? (connected ? "WebAgent gate" : "WebAgent gate offline")
    : (connected ? "MCP connected" : "MCP disconnected");
  return (
    <div className="flex h-7 shrink-0 items-center gap-3 border-t border-castle-line bg-black/35 px-3 text-[11px] text-castle-mute">
      <span
        className={
          "flex items-center gap-1 " +
          (connected ? "text-castle-allow" : "text-castle-deny")
        }
        title={connLabel}
      >
        <span
          className={
            "h-1.5 w-1.5 rounded-full " +
            (connected ? "bg-castle-allow" : "bg-castle-deny")
          }
        />
        {connLabel}
      </span>
      {projectName && <span>{projectName}</span>}
      {sessionId && <span className="truncate">{sessionId}</span>}
      {activeLayer && (
        <span className="text-castle-flow">active layer: {activeLayer}</span>
      )}
      <span className="ml-auto">{saveState ?? "—"}</span>
      <span>Ctrl-K command center</span>
    </div>
  );
}

// ── Right context rail ──────────────────────────────────────────────

export type CastleContextRailProps = {
  title?: string;
  children: React.ReactNode;
  open?: boolean;
};

function CastleContextRail({
  title = "Context",
  children,
  open = true,
}: CastleContextRailProps) {
  if (!open) return null;
  return (
    <aside className="hidden w-[340px] shrink-0 flex-col border-l border-castle-line bg-castle-panel/85 xl:flex">
      <div className="flex items-center gap-2 border-b border-castle-line px-4 py-3">
        <BookOpen className="h-3.5 w-3.5 text-castle-info" />
        <div className="text-[10px] font-black uppercase tracking-widest text-castle-mute">
          {title}
        </div>
      </div>
      <div className="flex-1 overflow-y-auto p-4">{children}</div>
    </aside>
  );
}

// ── Page header ─────────────────────────────────────────────────────

export type CastlePageHeaderProps = {
  eyebrow?: string;
  title: string;
  subtitle?: string;
  actions?: React.ReactNode;
};

export function CastlePageHeader({
  eyebrow,
  title,
  subtitle,
  actions,
}: CastlePageHeaderProps) {
  return (
    <div className="flex shrink-0 items-center justify-between border-b border-castle-line px-4 py-3">
      <div>
        {eyebrow && (
          <div className="text-[10px] font-black uppercase tracking-widest text-castle-allow">
            {eyebrow}
          </div>
        )}
        <h1 className="mt-1 text-2xl font-black tracking-tight text-white">
          {title}
        </h1>
        {subtitle && (
          <div className="mt-1 text-sm text-castle-mute">{subtitle}</div>
        )}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  );
}

// ── Shell wrapper ───────────────────────────────────────────────────

export type CastleShellProps = {
  active: NavKey;
  onSelect: (key: NavKey) => void;
  counts?: CastleNavCounts;
  managedMode?: boolean;
  version?: string;
  // Top strip
  projectLabel?: string;
  projectPath?: string;
  sessionLabel?: string;
  onCommandPalette?: () => void;
  onRefresh?: () => void;
  projectSelector?: React.ReactNode;
  sessionSelector?: React.ReactNode;
  topStripExtras?: React.ReactNode;
  // Status bar
  mcpConnected: boolean;
  activeLayer?: string;
  saveState?: string;
  // Context rail
  contextTitle?: string;
  contextOpen?: boolean;
  contextRail?: React.ReactNode;
  // Main content
  children: React.ReactNode;
};

export function CastleShell({
  active,
  onSelect,
  counts,
  managedMode,
  version,
  projectLabel,
  projectPath,
  sessionLabel,
  onCommandPalette,
  onRefresh,
  projectSelector,
  sessionSelector,
  topStripExtras,
  mcpConnected,
  activeLayer,
  saveState,
  contextTitle,
  contextOpen = true,
  contextRail,
  children,
}: CastleShellProps) {
  // Keyboard hooks: cmd-k / Ctrl-K → command palette; cmd-r / Ctrl-R
  // → refresh. Lives at the shell so every page benefits.
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const ctrlOrCmd = e.ctrlKey || e.metaKey;
      if (ctrlOrCmd && (e.key === "k" || e.key === "K")) {
        e.preventDefault();
        onCommandPalette?.();
      } else if (ctrlOrCmd && (e.key === "r" || e.key === "R")) {
        e.preventDefault();
        onRefresh?.();
      } else if (e.key === "/") {
        // Don't intercept / when an input has focus.
        const target = e.target as HTMLElement | null;
        if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable)) {
          return;
        }
        e.preventDefault();
        onCommandPalette?.();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onCommandPalette, onRefresh]);

  return (
    <div className="flex h-screen flex-col bg-castle-bg text-slate-200">
      <div className="flex min-h-0 flex-1 overflow-hidden">
        <CastleNavLeft
          active={active}
          onSelect={onSelect}
          counts={counts}
          managedMode={managedMode}
          version={version}
        />
        <main className="flex min-w-0 flex-1 flex-col">
          <CastleTopStrip
            projectLabel={projectLabel}
            projectPath={projectPath}
            sessionLabel={sessionLabel}
            managedMode={managedMode}
            onCommandPalette={onCommandPalette}
            onRefresh={onRefresh}
            projectSelector={projectSelector}
            sessionSelector={sessionSelector}
            extras={topStripExtras}
          />
          <div className="flex min-h-0 flex-1">
            <div className="flex min-h-0 min-w-0 flex-1 flex-col">{children}</div>
            {contextRail && (
              <CastleContextRail title={contextTitle} open={contextOpen}>
                {contextRail}
              </CastleContextRail>
            )}
          </div>
        </main>
      </div>
      <CastleStatusBar
        mcpConnected={mcpConnected}
        projectName={projectLabel}
        sessionId={sessionLabel}
        activeLayer={activeLayer}
        saveState={saveState}
      />
    </div>
  );
}

// ── Tiny shared primitives — pages reuse these ─────────────────────

export function CastlePill({
  children,
  tone = "default",
  className = "",
}: {
  children: React.ReactNode;
  tone?: "default" | "ok" | "warn" | "danger" | "info" | "flow" | "muted";
  className?: string;
}) {
  const toneClass =
    {
      default: "border-castle-line bg-white/[0.045] text-slate-300",
      ok: "border-castle-allow/35 bg-castle-allow/10 text-castle-allow",
      warn: "border-castle-warn/35 bg-castle-warn/10 text-castle-warn",
      danger: "border-castle-deny/35 bg-castle-deny/10 text-castle-deny",
      info: "border-castle-info/35 bg-castle-info/10 text-castle-info",
      flow: "border-castle-flow/35 bg-castle-flow/10 text-castle-flow",
      muted: "border-castle-line bg-black/30 text-castle-mute",
    }[tone];
  return (
    <span
      className={
        "inline-block rounded-full border px-2 py-0.5 text-[10px] font-bold uppercase tracking-widest " +
        toneClass +
        " " +
        className
      }
    >
      {children}
    </span>
  );
}

export function CastleButton({
  children,
  onClick,
  tone = "default",
  disabled,
  title,
  className = "",
  type = "button",
}: {
  children: React.ReactNode;
  onClick?: () => void;
  tone?: "default" | "primary" | "danger" | "warn";
  disabled?: boolean;
  title?: string;
  className?: string;
  type?: "button" | "submit";
}) {
  const toneClass =
    {
      default:
        "border-castle-line bg-white/[0.035] text-slate-300 hover:bg-white/[0.07]",
      primary:
        "border-castle-allow/35 bg-castle-allow/15 text-castle-allow hover:bg-castle-allow/25",
      danger:
        "border-castle-deny/35 bg-castle-deny/15 text-castle-deny hover:bg-castle-deny/25",
      warn:
        "border-castle-warn/35 bg-castle-warn/15 text-castle-warn hover:bg-castle-warn/25",
    }[tone];
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={
        "rounded-xl border px-3 py-2 text-sm font-bold transition disabled:cursor-not-allowed disabled:opacity-50 " +
        toneClass +
        " " +
        className
      }
    >
      {children}
    </button>
  );
}

// Re-export icons commonly used by pages so callers don't need to
// know lucide-react's name catalog.
export {
  Activity,
  AlertTriangle,
  Archive,
  BookOpen,
  Boxes,
  BrainCircuit,
  Command,
  FileCode2,
  Gauge,
  KeyRound,
  MonitorDot,
  Network,
  RefreshCcw,
  Search,
  ShieldCheck,
  TerminalSquare,
  UserCog,
};

// Suppress unused-warning: `useState` may be needed by future shell
// extensions (sidebar collapsed state, theme toggle).
void useState;
