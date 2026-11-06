import type { DashboardSnapshot } from "./dashboardApi";

export function MonitoringPage({ snapshot }: { snapshot: DashboardSnapshot | null }) {
  const exec = snapshot?.execution;
  const tokens = snapshot?.token_usage?.token_estimates;
  const byAction = Object.entries(exec?.summary?.by_action_kind ?? {}).sort((a, b) => b[1] - a[1]);
  const byEvent = Object.entries(exec?.summary?.by_event_kind ?? {}).sort((a, b) => b[1] - a[1]);
  const bySource = Object.entries(exec?.summary?.by_source ?? {}).sort((a, b) => b[1] - a[1]);

  return (
    <section className="page">
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: "12px" }}>
        <article className="flat-panel">
          <div className="section-label">Total Events</div>
          <strong style={{ fontSize: "1.4rem" }}>{exec?.summary?.total_events?.toLocaleString() ?? 0}</strong>
        </article>
        <article className="flat-panel">
          <div className="section-label">Tokens In</div>
          <strong style={{ fontSize: "1.4rem" }}>{tokens?.tokens_in?.toLocaleString() ?? 0}</strong>
        </article>
        <article className="flat-panel">
          <div className="section-label">Tokens Out</div>
          <strong style={{ fontSize: "1.4rem" }}>{tokens?.tokens_out?.toLocaleString() ?? 0}</strong>
        </article>
        <article className="flat-panel">
          <div className="section-label">Sessions</div>
          <strong style={{ fontSize: "1.4rem" }}>{snapshot?.project?.session_count ?? 0}</strong>
        </article>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: "12px", marginTop: "12px" }}>
        <section className="flat-panel">
          <div className="section-label">By Action Kind</div>
          {byAction.length === 0 && <div style={{ color: "var(--text-faint)", padding: "8px 0" }}>No data</div>}
          {byAction.map(([key, value]) => <div key={key} className="summary-row"><span>{key}</span><strong>{value}</strong></div>)}
        </section>
        <section className="flat-panel">
          <div className="section-label">By Event Kind</div>
          {byEvent.length === 0 && <div style={{ color: "var(--text-faint)", padding: "8px 0" }}>No data</div>}
          {byEvent.slice(0, 10).map(([key, value]) => <div key={key} className="summary-row"><span>{key}</span><strong>{value}</strong></div>)}
        </section>
        <section className="flat-panel">
          <div className="section-label">By Source</div>
          {bySource.length === 0 && <div style={{ color: "var(--text-faint)", padding: "8px 0" }}>No data</div>}
          {bySource.map(([key, value]) => <div key={key} className="summary-row"><span>{key}</span><strong>{value}</strong></div>)}
        </section>
      </div>
    </section>
  );
}
