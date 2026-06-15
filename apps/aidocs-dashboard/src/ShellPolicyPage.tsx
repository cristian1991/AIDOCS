/**
 * ShellPolicyPage — Phase 6g.2 (2026-05-02).
 *
 * Top-level page for the bash policy 3-state ledger. The panel
 * itself is unchanged from Phase 5c; this wrapper gives it its
 * own chamber inside the new shell, with a proper page header
 * and a generous main-pane layout instead of being embedded as
 * a collapsible section in the Settings page.
 */
import { TerminalSquare } from "lucide-react";
import { BashPolicyPanel } from "./BashPolicyPanel";
import { GovernedBashPanel } from "./GovernedBashPanel";
import { CastlePageHeader } from "./CastleShell";
import type { BashPolicySnapshot, BashCommandTriState } from "./dashboardApi";

export type ShellPolicyPageProps = {
  policy: BashPolicySnapshot | null | undefined;
  activeLayer: "global" | "project" | "session";
  hasProject: boolean;
  hasSession: boolean;
  projectRoot: string | null;
  saving: string | null;
  onSetCommandState: (
    cmd: string,
    layer: "global" | "project" | "session",
    nextState: BashCommandTriState,
  ) => void;
  onLayerChange: (layer: "global" | "project" | "session") => void;
};

export function ShellPolicyPage({
  policy,
  activeLayer,
  hasProject,
  hasSession,
  projectRoot,
  saving,
  onSetCommandState,
  onLayerChange,
}: ShellPolicyPageProps) {
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <CastlePageHeader
        eyebrow="Shell Policy"
        title="Command allow / deny ledger"
        subtitle="Per-layer 3-state toggle for every shell command. deny overpowers allow within a layer; lower layers override upper. The active edit-layer is ringed in emerald."
        actions={
          <div className="flex rounded-xl border border-castle-line bg-black/30 p-1">
            {(["global", "project", "session"] as const).map((scope) => {
              const enabled =
                scope === "global"
                  ? true
                  : scope === "project"
                  ? hasProject
                  : hasSession;
              const isActive = activeLayer === scope;
              return (
                <button
                  key={scope}
                  type="button"
                  disabled={!enabled}
                  onClick={() => onLayerChange(scope)}
                  className={
                    "rounded-lg px-3 py-1 text-xs font-bold uppercase tracking-widest transition " +
                    (isActive
                      ? "bg-castle-allow text-castle-bg"
                      : enabled
                      ? "text-castle-mute hover:text-slate-200"
                      : "cursor-not-allowed text-castle-mute/40")
                  }
                >
                  {scope}
                </button>
              );
            })}
          </div>
        }
      />
      <div className="flex min-h-0 flex-1 flex-col overflow-y-auto p-4">
        <GovernedBashPanel projectRoot={projectRoot} />
        {policy ? (
          <BashPolicyPanel
            policy={policy}
            activeLayer={activeLayer}
            hasProject={hasProject}
            hasSession={hasSession}
            saving={saving}
            onSetCommandState={onSetCommandState}
          />
        ) : (
          <div className="rounded-2xl border border-dashed border-castle-line bg-white/[0.02] p-8 text-center text-sm text-castle-mute">
            <TerminalSquare className="mx-auto mb-3 h-8 w-8 text-castle-mute/60" />
            Shell policy data unavailable. Confirm a project is selected
            and the dashboard snapshot has populated config.bash_policy.
          </div>
        )}
      </div>
    </div>
  );
}
