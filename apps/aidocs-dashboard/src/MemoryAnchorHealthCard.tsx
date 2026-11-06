import type { MemoryAnchorHealth } from "./dashboardApi";

/**
 * Memory ↔ code-unit anchor health (clause 3). Operator-visible signal of
 * whether the auto-anchor wire is feeding memory_symbol_anchors (live) or has
 * gone dormant (starved) — the 3-of-82 starvation that hid for weeks would show
 * here. Cheap COUNT-only data; dashboard-only, never on the ai_palace_status
 * hot path (an earlier attempt to put it there hung status for >1 min).
 */
export function MemoryAnchorHealthCard({
  health,
}: {
  health: MemoryAnchorHealth | null;
}) {
  if (!health) {
    return (
      <div
        className="card memory-anchor-health"
        data-testid="memory-anchor-health"
      >
        <h3>Memory ↔ Code Anchors</h3>
        <div className="wire wire-unknown" data-testid="anchor-wire">
          —
        </div>
      </div>
    );
  }
  return (
    <div className="card memory-anchor-health" data-testid="memory-anchor-health">
      <h3>Memory ↔ Code Anchors</h3>
      <div className={`wire wire-${health.wire}`} data-testid="anchor-wire">
        wire: {health.wire}
      </div>
      <dl>
        <dt>active memories</dt>
        <dd data-testid="anchor-active">{health.active_memories}</dd>
        <dt>anchored memories</dt>
        <dd data-testid="anchor-anchored">{health.anchored_memories}</dd>
        <dt>total anchors</dt>
        <dd data-testid="anchor-total">{health.total_anchors}</dd>
        <dt>coverage</dt>
        <dd data-testid="anchor-coverage">{health.coverage_pct}%</dd>
      </dl>
      {health.wire === "starved" && (
        <div className="warn" data-testid="anchor-starved-warn">
          anchor wire starved — memories are not surfacing on read
        </div>
      )}
    </div>
  );
}
