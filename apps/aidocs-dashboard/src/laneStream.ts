/**
 * Lane-scoped execution-stream filtering for the Live view.
 *
 * Workers tag every tool call with `agent_id = "lane:<lane_id>"`; some events
 * also carry `lane_id` directly. Clicking a lane filters the central stream to
 * that lane's activity (king directive 2026-06-20 — lane detail lives in the
 * view, not a side panel). Pure + unit-tested so the filter can never silently
 * fall back to "all" for an unknown lane.
 */

export function laneIdOfEvent(ev: { payload?: unknown } | null | undefined): string | null {
  const p = ev?.payload;
  if (!p || typeof p !== "object") return null;
  const rec = p as Record<string, unknown>;
  const direct = rec["lane_id"];
  if (typeof direct === "string" && direct) return direct;
  const agent = rec["agent_id"];
  if (typeof agent === "string" && agent.startsWith("lane:")) {
    return agent.slice("lane:".length) || null;
  }
  return null;
}

export function filterEventsByLane<T extends { payload?: unknown }>(
  events: readonly T[],
  laneId: string | null | undefined,
): T[] {
  if (!laneId) return [...events];
  return events.filter((e) => laneIdOfEvent(e) === laneId);
}
