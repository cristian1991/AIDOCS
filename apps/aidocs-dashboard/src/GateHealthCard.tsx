import type { GateHealth } from "./dashboardApi";

/**
 * Gate liveness card — the Empire's requirement: log in, stamp a session, and
 * SEE whether the security gate is actually running (hooks firing, no
 * declines, NLP alive). Server-side signal from aidocs_mcp/gate_health.py.
 *
 * NEVER-FAKE-GREEN FLOOR (test-pinned in GateHealthCard.test.tsx): ONLY an
 * explicit `status === "ok"` renders as alive/green. A missing section, an
 * `unknown` status, or any unrecognized value renders as UNKNOWN (amber
 * warning). A badge that says green because it merely FAILED TO CHECK is
 * worse than no badge at all — it manufactures exactly the false confidence
 * this card exists to cure (empire law: truth before green).
 */
type GateStatus = "ok" | "degraded" | "unknown";

function normalize(health: GateHealth | null | undefined): GateStatus {
  if (health?.status === "ok") return "ok";
  if (health?.status === "degraded") return "degraded";
  // Everything else — undefined, null, "unknown", garbage — is UNKNOWN.
  return "unknown";
}

const STATUS_LABEL: Record<GateStatus, string> = {
  ok: "GATE ALIVE",
  degraded: "GATE DEGRADED",
  unknown: "GATE UNKNOWN",
};

const STATUS_COLOR: Record<GateStatus, { fg: string; bg: string; border: string }> = {
  ok: { fg: "#34d399", bg: "#0c2b1e", border: "#1d6a4a" },
  degraded: { fg: "#ff8a80", bg: "#5a1a1a", border: "#a33" },
  unknown: { fg: "#f59e0b", bg: "#3a2c0a", border: "#8a6d1d" },
};

const STATUS_ICON: Record<GateStatus, string> = {
  ok: "🟢",
  degraded: "🛑",
  unknown: "⚠️",
};

export function GateHealthCard({ health }: { health: GateHealth | null | undefined }) {
  const status = normalize(health);
  const palette = STATUS_COLOR[status];
  const probes = health?.probes ?? {};
  // "idle" (no MCP traffic) is a NORMAL state, not a fault — never listed as
  // a problem, so the card does not cry wolf on a quiet session.
  const badProbes = Object.entries(probes).filter(
    ([, p]) => p && p.status !== "ok" && p.status !== "idle",
  );
  return (
    <div
      className="card gate-health"
      data-testid="gate-health"
      style={{
        marginBottom: 10,
        padding: "8px 10px",
        border: `1px solid ${palette.border}`,
        borderRadius: 8,
        background: palette.bg,
      }}
    >
      <span
        data-testid="gate-health-status"
        style={{
          color: palette.fg,
          fontWeight: 700,
          fontSize: "0.72rem",
          letterSpacing: 0.4,
          textTransform: "uppercase",
        }}
        title={
          status === "ok"
            ? `Hooks firing, no recent declines, NLP alive (computed ${health?.computed_at ?? "n/a"})`
            : "The AIDOCS security gate could not be verified as running — see the probe details"
        }
      >
        {STATUS_ICON[status]} {STATUS_LABEL[status]}
      </span>
      {status === "unknown" && (
        <span
          data-testid="gate-health-unknown-warn"
          style={{ marginLeft: 8, fontSize: "0.68rem", color: palette.fg, opacity: 0.9 }}
        >
          liveness could not be verified — unknown is a warning, never a pass
        </span>
      )}
      {status === "degraded" && (
        <span
          data-testid="gate-health-degraded-warn"
          style={{ marginLeft: 8, fontSize: "0.68rem", color: palette.fg, opacity: 0.9 }}
        >
          the gate may NOT be governing this session
        </span>
      )}
      {badProbes.length > 0 && (
        <ul style={{ margin: "6px 0 0", paddingLeft: 16, fontSize: "0.68rem", color: palette.fg }}>
          {badProbes.map(([name, p]) => (
            <li key={name} data-testid={`gate-health-probe-${name}`}>
              {name}: {p.status} — {p.reason || "no detail"}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
