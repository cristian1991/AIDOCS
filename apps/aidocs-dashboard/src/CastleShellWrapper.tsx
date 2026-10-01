/**
 * CastleShellWrapper — thin adapter between App.tsx's existing state
 * shape and the new CastleShell component.
 *
 * App.tsx still owns project/session/nav state. This wrapper picks
 * the relevant fields, derives the small handful of shell props
 * (managed mode flag, version, counts), and renders the new shell
 * around the active page. The wrapper exists so App.tsx's JSX
 * change is one-line: replace the old `<div className="shell">`
 * tree with `<CastleShellWrapper ...>{renderActivePage()}<...>`.
 *
 * The old hand-rolled `.shell` / `.sidebar` / `.topbar` CSS is no
 * longer used by this wrapper; it stays in styles.css for any
 * other surfaces that still reference it (will be cleaned up in
 * later phases as pages migrate fully).
 */
import { CastleShell, type CastleNavCounts } from "./CastleShell";
import type { DashboardSnapshot } from "./dashboardApi";
import type { NavKey } from "./dashboardUtils";

export type CastleShellWrapperProps = {
  active: NavKey;
  onSelect: (key: NavKey) => void;
  snapshot: DashboardSnapshot | null | undefined;
  projectLabel?: string;
  projectPath?: string;
  sessionLabel?: string;
  onCommandPalette?: () => void;
  onRefresh?: () => void;
  saveState?: string;
  projectSelector?: React.ReactNode;
  sessionSelector?: React.ReactNode;
  topStripExtras?: React.ReactNode;
  contextRail?: React.ReactNode;
  contextTitle?: string;
  children: React.ReactNode;
};

export function CastleShellWrapper({
  active,
  onSelect,
  snapshot,
  projectLabel,
  projectPath,
  sessionLabel,
  onCommandPalette,
  onRefresh,
  saveState,
  projectSelector,
  sessionSelector,
  topStripExtras,
  contextRail,
  contextTitle,
  children,
}: CastleShellWrapperProps) {
  const counts: CastleNavCounts = {
    pending_escalations: snapshot?.config?.rbac?.summary?.pending_escalation_count,
  };

  const managedMode = Boolean(snapshot?.managed_mode?.active);
  // Real build version, injected by Vite at build time from the latest git release tag
  // (define __APP_VERSION__, e.g. "v2.3.0b5"). Falls back to package.json / "dev".
  // The commit stamp makes web↔desktop drift visible at a glance: the same
  // frontend built from the same commit shows the same sha on both surfaces.
  const version = `${__APP_VERSION__} @${__BUILD_SHA__}`;

  return (
    <CastleShell
      active={active}
      onSelect={onSelect}
      counts={counts}
      managedMode={managedMode}
      version={version}
      projectLabel={projectLabel}
      projectPath={projectPath}
      sessionLabel={sessionLabel}
      onCommandPalette={onCommandPalette}
      onRefresh={onRefresh}
      projectSelector={projectSelector}
      sessionSelector={sessionSelector}
      topStripExtras={topStripExtras}
      mcpConnected={managedMode}
      activeLayer={undefined}
      saveState={saveState}
      contextTitle={contextTitle}
      contextRail={contextRail}
    >
      {children}
    </CastleShell>
  );
}
