/**
 * LaneDetailPanel — Phase 6f (2026-05-02).
 *
 * Lane detail surface for the Live page's right context rail.
 * Renders when the operator clicks a lane card. Shows everything
 * about that lane the snapshot exposes today, with graceful
 * "not yet wired" notes for fields that need backend additions
 * (granted tools, files touched, per-lane worker info).
 *
 * The panel is presentational. Pause / Resume / Cancel buttons fire
 * onPause / onResume / onCancel callbacks; the parent decides
 * whether to wire them to plan_conductor_pause_lane etc.
 */
import {
  AlertTriangle,
  CircleDot,
  FileText,
  GitBranch,
  PauseCircle,
  PlayCircle,
  StopCircle,
  Users,
  X,
} from "lucide-react";
import type { DashboardSnapshot } from "./dashboardApi";
import { CastlePill, CastleButton } from "./CastleShell";
import { formatTimeOfDay } from "./dashboardUtils";

export type LaneDetailPanelProps = {
  laneId: string;
  snapshot: DashboardSnapshot;
  onClose: () => void;
  onPause?: () => void;
  onResume?: () => void;
  onCancel?: () => void;
};

function deriveLaneInfo(
  snapshot: DashboardSnapshot,
  laneId: string,
): {
  name: string;
  depends_on: string[];
  state: "running" | "blocked" | "queued";
  blocked_reasons: string[];
  related_events: typeof snapshot.execution.recent;
} {
  const sel = snapshot.selected_session;
  const lane = sel?.conductor?.graph?.lanes?.find((l) => l.lane_id === laneId);
  const runnable = new Set(sel?.conductor?.runnable?.runnable_lane_ids ?? []);
  const blockedMap = sel?.conductor?.runnable?.blocked_reasons ?? {};
  const blocked_reasons = blockedMap[laneId] ?? [];
  const state: "running" | "blocked" | "queued" = blocked_reasons.length
    ? "blocked"
    : runnable.has(laneId)
    ? "running"
    : "queued";
  const related_events = (snapshot.execution.recent || []).filter((e) => {
    const lid = (e.payload as Record<string, unknown> | undefined)?.["lane_id"];
    return lid === laneId;
  });
  return {
    name: lane?.name ?? laneId,
    depends_on: lane?.depends_on ?? [],
    state,
    blocked_reasons,
    related_events,
  };
}

export function LaneDetailPanel({
  laneId,
  snapshot,
  onClose,
  onPause,
  onResume,
  onCancel,
}: LaneDetailPanelProps) {
  const info = deriveLaneInfo(snapshot, laneId);

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="flex items-start justify-between gap-2 border-b border-castle-line pb-3">
        <div className="min-w-0">
          <div className="text-[10px] font-black uppercase tracking-widest text-castle-mute">
            Lane
          </div>
          <h3 className="mt-1 break-all font-mono text-base font-bold text-slate-100">
            {info.name}
          </h3>
          <div className="mt-1 break-all font-mono text-[10px] text-castle-mute">
            {laneId}
          </div>
        </div>
        <div className="flex flex-col items-end gap-2">
          <CastlePill
            tone={
              info.state === "blocked"
                ? "danger"
                : info.state === "running"
                ? "ok"
                : "muted"
            }
          >
            {info.state === "blocked" && <CircleDot className="mr-1 inline h-2 w-2" />}
            {info.state}
          </CastlePill>
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border border-castle-line p-1 text-castle-mute hover:text-slate-200"
            title="Close (Esc)"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      {/* Body — scrollable */}
      <div className="flex-1 overflow-y-auto py-3">
        {/* Blocked reasons (when blocked) */}
        {info.blocked_reasons.length > 0 && (
          <Section icon={AlertTriangle} label="Blocked reasons">
            <ul className="mt-1 space-y-1">
              {info.blocked_reasons.map((r, i) => (
                <li
                  key={i}
                  className="rounded-lg border border-castle-deny/30 bg-castle-deny/5 px-2 py-1.5 text-xs text-castle-deny"
                >
                  {r}
                </li>
              ))}
            </ul>
          </Section>
        )}

        {/* Lane control */}
        <Section icon={PlayCircle} label="Lane control">
          <div className="mt-2 grid grid-cols-3 gap-1.5">
            <CastleButton
              onClick={onResume}
              disabled={!onResume || info.state === "running"}
              tone="primary"
              className="!py-1.5 !text-xs"
            >
              <PlayCircle className="mr-1 inline h-3 w-3" />
              Resume
            </CastleButton>
            <CastleButton
              onClick={onPause}
              disabled={!onPause || info.state !== "running"}
              tone="warn"
              className="!py-1.5 !text-xs"
            >
              <PauseCircle className="mr-1 inline h-3 w-3" />
              Pause
            </CastleButton>
            <CastleButton
              onClick={onCancel}
              disabled={!onCancel}
              tone="danger"
              className="!py-1.5 !text-xs"
            >
              <StopCircle className="mr-1 inline h-3 w-3" />
              Cancel
            </CastleButton>
          </div>
        </Section>

        {/* Dependencies */}
        <Section icon={GitBranch} label="Depends on">
          {info.depends_on.length === 0 ? (
            <Empty>No dependencies — this lane runs as soon as its parent plan is dispatched.</Empty>
          ) : (
            <div className="mt-1 flex flex-wrap gap-1">
              {info.depends_on.map((dep) => (
                <code
                  key={dep}
                  className="rounded-md border border-castle-info/30 bg-castle-info/5 px-2 py-0.5 text-[11px] text-castle-info"
                >
                  {dep}
                </code>
              ))}
            </div>
          )}
        </Section>

        {/* Granted tools — placeholder until session_query_gate exposed */}
        <Section icon={Users} label="Granted tools">
          <Empty>
            Per-lane lane_allowed_tools / lane_extra_tools aren't yet
            in the snapshot. Available via session_query_gate_store on
            the backend; dashboard wire-up is a follow-up phase.
          </Empty>
        </Section>

        {/* Files touched — placeholder */}
        <Section icon={FileText} label="Files touched">
          <Empty>
            Per-lane edit history isn't yet in the snapshot. Available
            via edit_history.files_touched_summary; dashboard wire-up
            is a follow-up phase.
          </Empty>
        </Section>

        {/* Recent events — filtered to this lane */}
        <Section icon={CircleDot} label={`Recent events (${info.related_events.length})`}>
          {info.related_events.length === 0 ? (
            <Empty>No events tagged with this lane_id yet.</Empty>
          ) : (
            <div className="mt-2 space-y-1">
              {info.related_events.slice(0, 8).map((e) => (
                <div
                  key={e.event_id}
                  className="flex items-start gap-2 rounded-md bg-black/25 px-2 py-1.5 text-[11px]"
                >
                  <time className="shrink-0 font-mono text-castle-mute">
                    {formatTimeOfDay(e.observed_at)}
                  </time>
                  <div className="min-w-0 flex-1">
                    <div className="truncate font-bold text-slate-200">
                      {e.capability_name || e.event_kind}
                    </div>
                    {e.action_kind && (
                      <div className="text-castle-mute">{e.action_kind}</div>
                    )}
                  </div>
                  {e.status && (
                    <CastlePill
                      tone={
                        e.status === "applied" || e.status === "allowed"
                          ? "ok"
                          : e.status === "refused" || e.status === "blocked"
                          ? "danger"
                          : "muted"
                      }
                    >
                      {e.status}
                    </CastlePill>
                  )}
                </div>
              ))}
            </div>
          )}
        </Section>
      </div>
    </div>
  );
}

function Section({
  icon: Icon,
  label,
  children,
}: {
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="mb-4 last:mb-0">
      <div className="mb-1 flex items-center gap-1.5 text-[10px] font-black uppercase tracking-widest text-castle-mute">
        <Icon className="h-3 w-3" />
        {label}
      </div>
      {children}
    </div>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-md border border-dashed border-castle-line bg-white/[0.02] px-2 py-2 text-[11px] italic text-castle-mute">
      {children}
    </div>
  );
}
