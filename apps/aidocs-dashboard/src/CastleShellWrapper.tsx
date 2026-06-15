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
  // Static for now — Phase 6 doesn't yet need a build-version probe.
  // The legacy bottom-of-sidebar `aidocsVersion` value is still passed
  // by App.tsx; hardcode for shell display until we wire it through.
  const version = "v2.x · castle-rebuild";

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
