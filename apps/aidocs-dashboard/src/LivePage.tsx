/**
 * LivePage — Phase 6d (2026-05-02).
 *
 * The dashboard's centerpiece — operator's "what's happening right
 * now" view. Three-column dense layout per king's screenshot:
 *
 *   ┌─────────────┬──────────────────┬──────────────────┐
 *   │ ACTIVE      │ EXECUTION STREAM │ CONTROL PANEL    │
 *   │   LANES     │  (live event     │  Pending         │
 *   │ (plans      │   feed)          │   Approvals      │
 *   │  accordion) │                  │  Quick Actions   │
 *   └─────────────┴──────────────────┴──────────────────┘
 *   ┌─────────────────────────────────────────────────────┐
 *   │ EVENTS · TOOL CALLS · BLOCKED · FAILED · TOKENS    │
 *   └─────────────────────────────────────────────────────┘
 *
 * Plans are accordion-grouped (each plan = one collapsible section
 * containing its lanes). Today AIDOCS surfaces a single active plan
 * per session; the accordion still works (one section, expanded by
 * default) and is ready for multi-plan when the backend ships it.
 *
 * Lane card click → opens the right context rail with lane detail
 * (Phase 6d.2 next commit; for now the click is a no-op — rail
 * gets wired when the LaneDetailPanel lands).
 */
import { useMemo, useState } from "react";
import {
  AlertTriangle,
  ChevronRight,
  CircleDot,
  Info,
  Key,
  PauseOctagon,
  Play,
  Sparkle,
  StopCircle,
} from "lucide-react";
import type { DashboardSnapshot } from "./dashboardApi";
import { CastlePill, CastleButton } from "./CastleShell";
import { formatTimeOfDay } from "./dashboardUtils";

export type LivePageProps = {
  snapshot: DashboardSnapshot;
  /** Notify wrapper to open the danger-confirm modal for kill switch.
   * Phase 6d ships the button only if a handler is given. */
  onKillSwitch?: () => void;
  /** Phase 6f: callback fires when the operator clicks a lane card.
   * The wrapper renders LaneDetailPanel into the right context rail. */
  onLaneSelect?: (laneId: string) => void;
  /** AUTHENTICATED ESCALATION WIRING (2026-05-26): wrapper passes
   * Tauri-backed handlers that invoke `approve_escalation` /
   * `deny_escalation`. The CLI hardens approver identity (email +
   * permission check) — these callbacks are presentational only.
   * Buttons render disabled when the handler is absent so an
   * unconfigured embedding never silently no-ops. */
  onApproveEscalation?: (requestId: string) => void | Promise<void>;
  onDenyEscalation?: (requestId: string) => void | Promise<void>;
};

type LaneState = "running" | "waiting" | "blocked" | "completed" | "queued";

type LaneRow = {
  lane_id: string;
  name: string;
  state: LaneState;
  progress: number; // 0..100
  step_label?: string;
  worker?: string;
  blocked_reason?: string;
};

type PlanGroup = {
  plan_id: string;
  plan_label: string;
  state: LaneState;
  lanes: LaneRow[];
};

const STATE_TONE: Record<LaneState, "ok" | "warn" | "danger" | "muted" | "info"> = {
  running: "ok",
  waiting: "warn",
  blocked: "danger",
  completed: "muted",
  queued: "muted",
};

const STATE_LABEL: Record<LaneState, string> = {
  running: "Running",
  waiting: "Waiting",
  blocked: "Blocked",
  completed: "Done",
  queued: "Queued",
};

function deriveLaneRow(
  lane: { lane_id: string; name: string; depends_on?: string[] },
  runnableIds: Set<string>,
  blockedReasons: Record<string, string[]>,
): LaneRow {
  let state: LaneState;
  let blocked_reason: string | undefined;
  if (blockedReasons[lane.lane_id]?.length) {
    state = "blocked";
    blocked_reason = blockedReasons[lane.lane_id].join("; ");
  } else if (runnableIds.has(lane.lane_id)) {
    state = "running";
  } else {
    state = "queued";
  }
  return {
    lane_id: lane.lane_id,
    name: lane.name,
    state,
    progress: state === "running" ? 50 : 0,
    worker: undefined,
    blocked_reason,
  };
}

function deriveActivePlan(snapshot: DashboardSnapshot): PlanGroup | null {
  const sel = snapshot.selected_session;
  if (!sel) return null;
  const graph = sel.conductor?.graph;
  if (!graph || !graph.lanes?.length) return null;
  const runnableIds = new Set(sel.conductor?.runnable?.runnable_lane_ids ?? []);
  const blocked = sel.conductor?.runnable?.blocked_reasons ?? {};
  const lanes = graph.lanes.map((l) => deriveLaneRow(l, runnableIds, blocked));
  // Plan-level state = worst lane state.
  const planState: LaneState = lanes.some((l) => l.state === "blocked")
    ? "blocked"
    : lanes.some((l) => l.state === "running")
    ? "running"
    : lanes.every((l) => l.state === "completed")
    ? "completed"
    : "queued";
  return {
    plan_id: "active",
    plan_label: sel.overview?.title || "Active plan",
    state: planState,
    lanes,
  };
}

function PlanAccordion({
  plan,
  expanded,
  onToggle,
  onLaneClick,
}: {
  plan: PlanGroup;
  expanded: boolean;
  onToggle: () => void;
  onLaneClick?: (laneId: string) => void;
}) {
  const runningCount = plan.lanes.filter((l) => l.state === "running").length;
  const totalCount = plan.lanes.length;
  return (
    <div className="rounded-2xl border border-castle-line bg-white/[0.02]">
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-center gap-2 px-3 py-2.5 text-left transition hover:bg-white/[0.02]"
      >
        <ChevronRight
          className={
            "h-3.5 w-3.5 text-castle-mute transition-transform " +
            (expanded ? "rotate-90" : "")
          }
        />
        <strong className="font-mono text-sm text-slate-100">
          {plan.plan_label}
        </strong>
        <span className="ml-auto flex items-center gap-2">
          <CastlePill tone={STATE_TONE[plan.state]}>
            {STATE_LABEL[plan.state]}
          </CastlePill>
          <span className="text-[11px] font-mono text-castle-mute">
            {runningCount}/{totalCount}
          </span>
          <span className="flex items-center gap-1">
            <button
              type="button"
              className="grid h-6 w-6 place-items-center rounded-md border border-castle-line text-castle-mute hover:text-slate-200"
              title="Plan info"
              onClick={(e) => {
                e.stopPropagation();
              }}
            >
              <Info className="h-3 w-3" />
            </button>
          </span>
        </span>
      </button>
      {expanded && (
        <div className="space-y-2 border-t border-castle-line p-2">
          {plan.lanes.map((lane) => (
            <button
              key={lane.lane_id}
              type="button"
              onClick={() => onLaneClick?.(lane.lane_id)}
              className="block w-full rounded-xl border border-castle-line bg-castle-card px-3 py-2.5 text-left transition hover:border-castle-allow/30 hover:bg-white/[0.04]"
            >
              <div className="flex items-center justify-between gap-2">
                <div className="min-w-0">
                  <div className="truncate text-sm font-bold text-slate-100">
                    {lane.name}
                  </div>
                  {lane.worker && (
                    <div className="truncate text-[11px] text-castle-mute">
                      {lane.worker}
                    </div>
                  )}
                </div>
                <CastlePill tone={STATE_TONE[lane.state]}>
                  {STATE_LABEL[lane.state]}
                </CastlePill>
              </div>
              <div className="mt-2 flex items-center gap-2">
                <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-black/40">
                  <div
                    className={
                      "h-full rounded-full transition-all " +
                      (lane.state === "blocked"
                        ? "bg-castle-deny/70"
                        : lane.state === "running"
                        ? "bg-castle-allow/70"
                        : "bg-white/10")
                    }
                    style={{ width: `${Math.min(100, Math.max(0, lane.progress))}%` }}
                  />
                </div>
                {lane.step_label && (
                  <span className="font-mono text-[11px] text-castle-mute">
                    {lane.step_label}
                  </span>
                )}
              </div>
              {lane.blocked_reason && (
                <div className="mt-2 truncate text-[11px] text-castle-deny">
                  {lane.blocked_reason}
                </div>
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function statusTone(status: string | null | undefined) {
  if (!status) return "muted" as const;
  const s = status.toLowerCase();
  if (s === "applied" || s === "allowed" || s === "ok") return "ok" as const;
  if (s === "refused" || s === "blocked" || s === "denied") return "danger" as const;
  if (s === "needs_approval" || s === "warned") return "warn" as const;
  return "muted" as const;
}

function eventStripeClass(status: string | null | undefined) {
  const t = statusTone(status);
  if (t === "ok") return "border-l-castle-allow/70";
  if (t === "danger") return "border-l-castle-deny/70";
  if (t === "warn") return "border-l-castle-warn/70";
  return "border-l-castle-line";
}

export function LivePage({
  snapshot,
  onKillSwitch,
  onLaneSelect,
  onApproveEscalation,
  onDenyEscalation,
}: LivePageProps) {
  const [expandedPlans, setExpandedPlans] = useState<Set<string>>(
    () => new Set(["active"]),
  );

  const activePlan = useMemo(() => deriveActivePlan(snapshot), [snapshot]);
  const recent = snapshot.execution?.recent ?? [];
  const pending = snapshot.config?.rbac?.pending_escalations ?? [];
  const tokens = snapshot.token_usage?.token_estimates;
  const totalEvents = snapshot.execution?.summary?.total_events ?? 0;
  const byEventKind = snapshot.execution?.summary?.by_event_kind ?? {};
  const blockedCount =
    (byEventKind["rbac_denied"] ?? 0) + (byEventKind["gate_refused"] ?? 0);
  const failedCount =
    (byEventKind["worker_failed"] ?? 0) +
    (byEventKind["task_failed"] ?? 0) +
    (byEventKind["tool_failed"] ?? 0);
  const toolCalls = byEventKind["tool_call"] ?? byEventKind["tool_use"] ?? 0;

  function togglePlan(id: string) {
    setExpandedPlans((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function fmtNumber(n: number | undefined): string {
    if (n === undefined || n === null) return "0";
    if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
    return String(n);
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* Three-column main grid */}
      <div className="grid min-h-0 flex-1 grid-cols-12 gap-3 overflow-hidden p-4">
        {/* ── Active Lanes (left) ──────────────────────────────── */}
        <section className="col-span-12 flex min-h-0 flex-col xl:col-span-3">
          <header className="flex items-center justify-between pb-2">
            <h2 className="text-[10px] font-black uppercase tracking-widest text-castle-mute">
              Active Lanes
            </h2>
            <button
              type="button"
              className="grid h-6 w-6 place-items-center rounded-md border border-castle-line text-castle-mute hover:text-slate-200"
              title="New plan"
            >
              +
            </button>
          </header>
          <div className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto">
            {activePlan ? (
              <PlanAccordion
                plan={activePlan}
                expanded={expandedPlans.has(activePlan.plan_id)}
                onToggle={() => togglePlan(activePlan.plan_id)}
                onLaneClick={onLaneSelect}
              />
            ) : (
              <div className="rounded-2xl border border-dashed border-castle-line bg-white/[0.02] p-6 text-center text-sm text-castle-mute">
                No active plan. The conductor isn't running lanes for this
                session yet.
              </div>
            )}
          </div>
        </section>

        {/* ── Execution Stream (middle) ────────────────────────── */}
        <section className="col-span-12 flex min-h-0 flex-col xl:col-span-6">
          <header className="flex items-center justify-between pb-2">
            <h2 className="text-[10px] font-black uppercase tracking-widest text-castle-mute">
              Execution Stream
            </h2>
            <CastlePill tone="ok">
              <span className="flex items-center gap-1">
                <CircleDot className="h-2.5 w-2.5 animate-pulse" /> live
              </span>
            </CastlePill>
          </header>
          <div className="flex min-h-0 flex-1 flex-col overflow-y-auto rounded-2xl border border-castle-line bg-castle-card">
            {recent.length === 0 ? (
              <div className="p-6 text-center text-sm text-castle-mute">
                No recent events. The session hasn't dispatched anything yet.
              </div>
            ) : (
              recent.map((event) => {
                const tone = statusTone(event.status);
                const stripeClass = eventStripeClass(event.status);
                return (
                  <div
                    key={event.event_id}
                    className={
                      "flex items-start gap-3 border-b border-castle-line/60 border-l-2 px-3 py-2.5 last:border-b-0 " +
                      stripeClass
                    }
                  >
                    <time className="shrink-0 font-mono text-[11px] text-castle-mute">
                      {formatTimeOfDay(event.observed_at)}
                    </time>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <strong className="truncate text-sm text-slate-100">
                          {event.capability_name || event.event_kind}
                        </strong>
                        {event.action_kind && (
                          <span className="text-[11px] text-castle-mute">
                            {event.action_kind}
                          </span>
                        )}
                        <CastlePill tone={tone} className="ml-auto">
                          {event.status || "ok"}
                        </CastlePill>
                      </div>
                      {event.payload?.["lane_id"] !== undefined && (
                        <div className="mt-1 text-[11px] text-castle-mute">
                          lane:{" "}
                          <code className="text-castle-info">
                            {String(event.payload["lane_id"])}
                          </code>
                        </div>
                      )}
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </section>

        {/* ── Control Panel (right) ────────────────────────────── */}
        <aside className="col-span-12 flex min-h-0 flex-col xl:col-span-3">
          <header className="flex items-center justify-between pb-2">
            <h2 className="text-[10px] font-black uppercase tracking-widest text-castle-mute">
              Control Panel
            </h2>
          </header>
          <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto">
            {/* Pending approvals */}
            <div className="rounded-2xl border border-castle-line bg-castle-card p-3">
              <div className="flex items-center justify-between pb-2">
                <h3 className="text-[10px] font-black uppercase tracking-widest text-castle-mute">
                  Pending Approvals
                </h3>
                <CastlePill
                  tone={pending.length > 0 ? "warn" : "muted"}
                  className="ml-2"
                >
                  {pending.length}
                </CastlePill>
              </div>
              {pending.length === 0 ? (
                <div className="rounded-xl border border-dashed border-castle-line p-3 text-center text-xs text-castle-mute">
                  Queue empty. The kingdom is at peace.
                </div>
              ) : (
                <div className="space-y-2">
                  {pending.slice(0, 3).map((req) => (
                    <div
                      key={req.request_id}
                      className="rounded-xl border border-castle-warn/30 bg-castle-warn/5 p-2.5"
                    >
                      <div className="flex items-center gap-2">
                        <Key className="h-3.5 w-3.5 text-castle-warn" />
                        <strong className="truncate text-sm text-slate-100">
                          {req.gate_permission}
                        </strong>
                      </div>
                      <div className="mt-1 truncate text-[11px] text-castle-mute">
                        from <em>{req.requester_label}</em>
                      </div>
                      <div className="mt-2 flex items-center gap-1">
                        <CastleButton
                          tone="primary"
                          className="flex-1 !py-1.5 !text-xs"
                          disabled={!onApproveEscalation}
                          onClick={() => {
                            // Buttons render disabled when handler is
                            // absent (props default to undefined). The
                            // wrapper's handler prompts the operator for
                            // their email + reason and invokes the
                            // Tauri `approve_escalation` command — same
                            // RBAC path as `aidocs admin
                            // approve-escalation`, no shortcut.
                            void onApproveEscalation?.(req.request_id);
                          }}
                          title={
                            onApproveEscalation
                              ? `Approve ${req.gate_permission}`
                              : "Approve wiring not configured"
                          }
                        >
                          Approve
                        </CastleButton>
                        <CastleButton
                          tone="danger"
                          className="flex-1 !py-1.5 !text-xs"
                          disabled={!onDenyEscalation}
                          onClick={() => {
                            void onDenyEscalation?.(req.request_id);
                          }}
                          title={
                            onDenyEscalation
                              ? `Reject ${req.gate_permission}`
                              : "Reject wiring not configured"
                          }
                        >
                          Reject
                        </CastleButton>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>

            {/* Quick actions */}
            <div className="rounded-2xl border border-castle-line bg-castle-card p-3">
              <h3 className="pb-2 text-[10px] font-black uppercase tracking-widest text-castle-mute">
                Quick Actions
              </h3>
              <div className="space-y-2">
                <CastleButton tone="danger" className="flex w-full items-center justify-center gap-2">
                  <StopCircle className="h-3.5 w-3.5" />
                  Stop All Lanes
                </CastleButton>
                <CastleButton className="flex w-full items-center justify-center gap-2">
                  <PauseOctagon className="h-3.5 w-3.5" />
                  Pause All
                </CastleButton>
                <CastleButton tone="primary" className="flex w-full items-center justify-center gap-2">
                  <Sparkle className="h-3.5 w-3.5" />
                  Validate Plan
                </CastleButton>
                <CastleButton tone="primary" className="flex w-full items-center justify-center gap-2">
                  <Play className="h-3.5 w-3.5" />
                  Start Plan
                </CastleButton>
                {onKillSwitch && (
                  <CastleButton
                    tone="warn"
                    onClick={onKillSwitch}
                    className="mt-3 flex w-full items-center justify-center gap-2 border-castle-deny/40 bg-castle-deny/10 text-castle-deny hover:bg-castle-deny/20"
                    title="Deactivate AIDOCS — posture toggle, not a runtime action"
                  >
                    <AlertTriangle className="h-3.5 w-3.5" />
                    Deactivate AIDOCS
                  </CastleButton>
                )}
              </div>
            </div>
          </div>
        </aside>
      </div>

      {/* Bottom stat tile strip */}
      <div className="grid shrink-0 grid-cols-5 gap-3 border-t border-castle-line bg-black/15 p-3">
        <StatTile label="Events (last 1h)" value={fmtNumber(totalEvents)} />
        <StatTile label="Tool Calls" value={fmtNumber(toolCalls)} />
        <StatTile
          label="Blocked"
          value={fmtNumber(blockedCount)}
          tone={blockedCount > 0 ? "danger" : "ok"}
        />
        <StatTile
          label="Failed"
          value={fmtNumber(failedCount)}
          tone={failedCount > 0 ? "danger" : "ok"}
        />
        <StatTile
          label="Tokens (in/out)"
          value={
            tokens
              ? `${fmtNumber(tokens.tokens_in)} / ${fmtNumber(tokens.tokens_out)}`
              : "—"
          }
        />
      </div>
    </div>
  );
}

function StatTile({
  label,
  value,
  tone = "default",
}: {
  label: string;
  value: string;
  tone?: "default" | "ok" | "warn" | "danger";
}) {
  const valueClass =
    tone === "ok"
      ? "text-castle-allow"
      : tone === "warn"
      ? "text-castle-warn"
      : tone === "danger"
      ? "text-castle-deny"
      : "text-slate-100";
  return (
    <div className="rounded-xl border border-castle-line bg-castle-card px-3 py-2.5">
      <div className="text-[9px] font-black uppercase tracking-widest text-castle-mute">
        {label}
      </div>
      <div className={"mt-1 text-2xl font-black tracking-tight " + valueClass}>
        {value}
      </div>
    </div>
  );
}
