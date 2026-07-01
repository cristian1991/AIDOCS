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
import { useEffect, useMemo, useState } from "react";
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
import { laneScope } from "./dashboardApi";
import type { DashboardSnapshot, LaneScope } from "./dashboardApi";
import { filterEventsByLane } from "./laneStream";
import { CastlePill, CastleButton } from "./CastleShell";
import { formatTimeOfDay } from "./dashboardUtils";
import { conductorLocked } from "./webmcpScope";
import { openUpgrade } from "./entitlements";

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
  /** Lane-scoped stream filter: when set, the Execution Stream shows only this
   * lane's events (king 2026-06-20 - lane detail in the view, not a side panel). */
  selectedLaneId?: string | null;
  onClearLane?: () => void;
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

type ConductorBlock =
  | {
      graph?: { lanes: Array<{ lane_id: string; name: string; depends_on?: string[] }> } | null;
      runnable?: { runnable_lane_ids: string[]; blocked_reasons: Record<string, string[]> } | null;
    }
  | null
  | undefined;

function derivePlanGroup(conductor: ConductorBlock, planId: string, label: string, sessionStatus?: string | null): PlanGroup | null {
  const graph = conductor?.graph;
  if (!graph || !graph.lanes?.length) return null;
  const runnableIds = new Set(conductor?.runnable?.runnable_lane_ids ?? []);
  const blocked = conductor?.runnable?.blocked_reasons ?? {};
  const done = sessionStatus === "done";
  const lanes = graph.lanes.map((l) => {
    const row = deriveLaneRow(l, runnableIds, blocked);
    // Finished plan with no live running/blocked signal -> its lanes are completed
    // (truthful: the conductor no longer tracks live state for a done plan).
    if (done && row.state === "queued") row.state = "completed";
    return row;
  });
  // Plan-level state = worst lane state.
  const planState: LaneState = lanes.some((l) => l.state === "blocked")
    ? "blocked"
    : lanes.some((l) => l.state === "running")
    ? "running"
    : lanes.every((l) => l.state === "completed")
    ? "completed"
    : "queued";
  return { plan_id: planId, plan_label: label, state: planState, lanes };
}

// Every conductor plan in the PROJECT (active + finished, across ALL sessions),
// not just the selected session's - so a plan is never hidden behind selection.
function deriveProjectPlans(snapshot: DashboardSnapshot): PlanGroup[] {
  const out = (snapshot.plans ?? [])
    .map((p) => derivePlanGroup(p.conductor, p.session_id, p.title || p.session_id, p.status))
    .filter((p): p is PlanGroup => !!p);
  // Fallback for an older gate without `plans`: show the selected session's plan
  // so behaviour never regresses below what shipped before.
  if (!out.length && snapshot.selected_session?.conductor) {
    const g = derivePlanGroup(
      snapshot.selected_session.conductor,
      "selected",
      snapshot.selected_session.overview?.title || "Active plan",
    );
    if (g) return [g];
  }
  return out;
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
  const [openLanes, setOpenLanes] = useState<Set<string>>(new Set());
  const [scopes, setScopes] = useState<Record<string, LaneScope | "loading">>({});

  const toggleLane = (laneId: string) => {
    setOpenLanes((prev) => {
      const next = new Set(prev);
      if (next.has(laneId)) next.delete(laneId);
      else next.add(laneId);
      return next;
    });
    onLaneClick?.(laneId);
    if (!scopes[laneId]) {
      setScopes((s) => ({ ...s, [laneId]: "loading" }));
      laneScope(laneId)
        .then((res) => setScopes((s) => ({ ...s, [laneId]: res })))
        .catch(() =>
          setScopes((s) => ({ ...s, [laneId]: { ok: false, tools: [], files: [] } })),
        );
    }
  };

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
        </span>
      </button>
      {expanded && (
        <div className="space-y-2 border-t border-castle-line p-2">
          {plan.lanes.map((lane) => {
            const laneOpen = openLanes.has(lane.lane_id);
            const scope = scopes[lane.lane_id];
            return (
              <div
                key={lane.lane_id}
                className="rounded-xl border border-castle-line bg-castle-card"
              >
                <button
                  type="button"
                  onClick={() => toggleLane(lane.lane_id)}
                  className="block w-full px-3 py-2.5 text-left transition hover:bg-white/[0.04]"
                >
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex min-w-0 items-center gap-1.5">
                      <ChevronRight
                        className={
                          "h-3 w-3 shrink-0 text-castle-mute transition-transform " +
                          (laneOpen ? "rotate-90" : "")
                        }
                      />
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
                {laneOpen && (
                  <div className="space-y-2.5 border-t border-castle-line px-3 py-2.5">
                    {!scope || scope === "loading" ? (
                      <div className="text-[11px] text-castle-mute">Loading lane scope…</div>
                    ) : !scope.ok ? (
                      <div className="text-[11px] text-castle-deny">
                        Lane scope unavailable.
                      </div>
                    ) : (
                      <>
                        <div>
                          <div className="text-[10px] font-black uppercase tracking-widest text-castle-mute">
                            Allowed tools ({scope.tools.length})
                            {scope.tool_source === "default_lane_toolset" ? " · default" : ""}
                          </div>
                          <div className="mt-1 flex max-h-24 flex-wrap gap-1 overflow-y-auto">
                            {scope.tools.map((t) => (
                              <code
                                key={t}
                                className="rounded border border-castle-line bg-black/30 px-1.5 py-0.5 text-[10px] text-slate-300"
                              >
                                {t}
                              </code>
                            ))}
                          </div>
                        </div>
                        <div>
                          <div className="text-[10px] font-black uppercase tracking-widest text-castle-mute">
                            Files ({scope.files.length})
                          </div>
                          {scope.files.length === 0 ? (
                            <div className="mt-1 text-[11px] text-castle-mute">
                              No declared files — default lane scope.
                            </div>
                          ) : (
                            <ul className="mt-1 space-y-0.5">
                              {scope.files.map((f) => (
                                <li key={f}>
                                  <code className="break-all text-[10px] text-castle-info">
                                    {f}
                                  </code>
                                </li>
                              ))}
                            </ul>
                          )}
                        </div>
                      </>
                    )}
                  </div>
                )}
              </div>
            );
          })}
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
  selectedLaneId,
  onClearLane,
}: LivePageProps) {
  const [expandedPlans, setExpandedPlans] = useState<Set<string>>(
    () => new Set(["active"]),
  );

  // Honest "live" badge: green/pulsing only when the auto-refresh is actually
  // running (tab visible). Paused + dimmed when hidden — never a fake green.
  const [streamLive, setStreamLive] = useState(
    typeof document === "undefined" || document.visibilityState === "visible",
  );
  useEffect(() => {
    const onVis = () => setStreamLive(document.visibilityState === "visible");
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, []);

  const plans = useMemo(() => deriveProjectPlans(snapshot), [snapshot]);
  const recent = snapshot.execution?.recent ?? [];
  const shownEvents = filterEventsByLane(recent, selectedLaneId);
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
            {plans.length ? (
              plans.map((pg) => (
                <PlanAccordion
                  key={pg.plan_id}
                  plan={pg}
                  expanded={expandedPlans.has(pg.plan_id)}
                  onToggle={() => togglePlan(pg.plan_id)}
                  onLaneClick={onLaneSelect}
                />
              ))
            ) : (
              <div className="rounded-2xl border border-dashed border-castle-line bg-white/[0.02] p-6 text-center text-sm text-castle-mute">
                No conductor plans in this project yet.
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
            <div className="flex items-center gap-2">
              {selectedLaneId ? (
                <button
                  type="button"
                  onClick={() => onClearLane?.()}
                  title="Clear lane filter"
                  className="flex items-center gap-1 rounded-full border border-castle-info/40 bg-castle-info/[0.08] px-2 py-0.5 text-[10px] font-bold text-castle-info hover:bg-castle-info/[0.15]"
                >
                  lane: <code className="text-castle-info">{selectedLaneId}</code>
                  <span className="text-castle-mute">✕</span>
                </button>
              ) : null}
              <CastlePill tone="ok">
                <span className={"flex items-center gap-1 " + (streamLive ? "" : "opacity-50")} title={streamLive ? "Auto-refreshing every 2s" : "Paused - tab not visible"}>
                  <CircleDot className={"h-2.5 w-2.5 " + (streamLive ? "animate-pulse" : "")} /> {streamLive ? "live" : "paused"}
                </span>
              </CastlePill>
            </div>
          </header>
          <div className="flex min-h-0 flex-1 flex-col overflow-y-auto rounded-2xl border border-castle-line bg-castle-card">
            {recent.length === 0 ? (
              <div className="flex flex-col gap-3 p-5">
                <div className="text-center text-sm text-castle-mute">
                  No agent activity yet — connect an agent to see live tool calls, lanes + audit here.
                </div>
                <div className="rounded-xl border border-castle-allow/30 bg-castle-allow/[0.06] p-4">
                  <div className="flex items-center gap-2">
                    <span className="text-[11px] font-black uppercase tracking-widest text-castle-allow">WebAgent</span>
                    <span className="rounded-full border border-castle-allow/40 px-2 py-0.5 text-[9px] font-bold uppercase tracking-widest text-castle-allow">available now</span>
                  </div>
                  <p className="mt-1.5 text-xs leading-5 text-castle-mute">Connect ChatGPT or Claude to AIDOCS over MCP — your model becomes the agent.</p>
                  <ol className="mt-2 list-decimal space-y-1 pl-5 text-xs leading-5 text-slate-300">
                    <li>In ChatGPT / Claude → Settings → Connectors → Add custom connector.</li>
                    <li>URL <code className="text-castle-allow">https://mcp.codenexus.cloud</code> — sign in with your CodeNexus account.</li>
                    <li>Pick a project, then ask it to run a read tool (project status, <code>ai_find</code>).</li>
                    <li>Tool calls, lanes + audit stream in live, right here.</li>
                  </ol>
                </div>
                <div className="grid gap-3 sm:grid-cols-2">
                  <div className="rounded-xl border border-castle-line bg-white/[0.02] p-4">
                    <div className="text-[11px] font-black uppercase tracking-widest text-slate-200">CloudAgent</div>
                    <p className="mt-1.5 text-xs leading-5 text-castle-mute">Run the conductor in the cloud — orchestrate lanes with no local box.</p>
                    <button type="button" onClick={() => openUpgrade("cloudagent")} className="mt-2 text-xs font-bold text-castle-allow hover:underline">Upgrade to CloudAgent →</button>
                  </div>
                  <div className="rounded-xl border border-castle-line bg-white/[0.02] p-4">
                    <div className="flex items-center gap-2">
                      <span className="text-[11px] font-black uppercase tracking-widest text-slate-200">RemoteAgent</span>
                      <span className="rounded-full border border-castle-line px-2 py-0.5 text-[9px] font-bold uppercase tracking-widest text-castle-mute">soon</span>
                    </div>
                    <p className="mt-1.5 text-xs leading-5 text-castle-mute">Drive AIDOCS on your own machine from anywhere — secure, seat-shared.</p>
                  </div>
                </div>
              </div>
            ) : shownEvents.length === 0 ? (
              <div className="p-5 text-center text-sm text-castle-mute">
                No events for lane{" "}
                <code className="text-castle-info">{selectedLaneId}</code> yet.
              </div>
            ) : (
              shownEvents.map((event) => {
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
                  <CastleButton tone="danger" onClick={() => { if (conductorLocked()) openUpgrade("cloudagent"); }} className="flex w-full items-center justify-center gap-2">
                  <StopCircle className="h-3.5 w-3.5" />
                  Stop All Lanes
                </CastleButton>
                  <CastleButton onClick={() => { if (conductorLocked()) openUpgrade("cloudagent"); }} className="flex w-full items-center justify-center gap-2">
                  <PauseOctagon className="h-3.5 w-3.5" />
                  Pause All
                </CastleButton>
                  <CastleButton tone="primary" onClick={() => { if (conductorLocked()) openUpgrade("cloudagent"); }} className="flex w-full items-center justify-center gap-2">
                  <Sparkle className="h-3.5 w-3.5" />
                  Validate Plan
                </CastleButton>
                  <CastleButton tone="primary" onClick={() => { if (conductorLocked()) openUpgrade("cloudagent"); }} className="flex w-full items-center justify-center gap-2">
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
