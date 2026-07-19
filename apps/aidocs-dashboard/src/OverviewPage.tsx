import { PieChart as RechartsPie, Pie, Cell, Tooltip, ResponsiveContainer, Legend } from "recharts";
import type { OverviewPageProps } from "./dashboardTypes";
import { CHART_COLORS, ChartTooltip } from "./dashboardCharts";

export function OverviewPage({ snapshot, selectedSession, contextBudget, compactingContext, onCompactContext }: OverviewPageProps) {
  const tokenEstimates = snapshot.token_usage.token_estimates;
  const inOutData = [
    { name: "Tokens in", value: tokenEstimates.tokens_in, tokens: tokenEstimates.tokens_in, count: tokenEstimates.tokens_in_calls ?? 0 },
    { name: "Tokens out", value: tokenEstimates.tokens_out, tokens: tokenEstimates.tokens_out, count: tokenEstimates.tokens_out_calls ?? 0 },
  ];
  return (
    <section className="page page-overview">
      <div className="overview-top">
        <section className="flat-panel overview-token-panel">
          <div className="section-label">Token Distribution</div>
          <div className="chart-container chart-container-fill">
            <ResponsiveContainer width="100%" height="100%">
              <RechartsPie>
                <Pie data={inOutData} dataKey="value" nameKey="name" cx="50%" cy="45%" innerRadius="55%" outerRadius="80%">
                  {inOutData.map((_, i) => <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />)}
                </Pie>
                <Tooltip content={<ChartTooltip />} />
                <Legend iconType="circle" iconSize={8} wrapperStyle={{ fontSize: "0.78rem", color: "#b9d0c2" }} />
              </RechartsPie>
            </ResponsiveContainer>
          </div>
        </section>
        <div className="overview-info">
          <section className="flat-panel">
            <div className="section-label">Project</div>
            <div className="summary-row"><span>Root</span><strong>{snapshot.project.project_root}</strong></div>
            <div className="summary-row"><span>Code files</span><strong>{snapshot.project.code_file_count}</strong></div>
            <div className="summary-row"><span>Schema entities</span><strong>{snapshot.project.schema_entity_count}</strong></div>
            <div className="summary-row"><span>Sessions</span><strong>{snapshot.project.session_count}</strong></div>
          </section>
          <section className="flat-panel">
            <div className="section-label">Session</div>
            <div className="summary-row"><span>Status</span><strong>{selectedSession?.overview.status ?? "unknown"}</strong></div>
            <div className="summary-row"><span>Goal</span><strong>{selectedSession?.overview.goal ?? "none"}</strong></div>
            <div className="summary-row"><span>Owner</span><strong>{selectedSession?.overview.owner ?? "unknown"}</strong></div>
          </section>
          <section className="flat-panel">
            <div className="section-label">Context Budget</div>
            <div className="summary-row"><span>Status</span><strong>{contextBudget?.status ?? contextBudget?.reason ?? "n/a"}</strong></div>
            <div className="summary-row"><span>Estimated tokens</span><strong>{contextBudget?.estimated_tokens?.toLocaleString?.() ?? "0"}</strong></div>
            <div className="summary-row"><span>Journal entries</span><strong>{contextBudget?.journal_entries ?? 0}</strong></div>
            <button className="action-button action-button-small" type="button" disabled={!contextBudget?.available || compactingContext} onClick={onCompactContext}>
              {compactingContext ? "Compacting..." : "Compact"}
            </button>
          </section>
        </div>
      </div>
      <section className="flat-panel overview-execution-panel">
        <div className="section-label">Recent Execution</div>
        <div className="flat-table">
          <div className="table-head execution-table-row" aria-hidden="true">
            <span>Capability</span>
            <span>Action</span>
            <span>Observed</span>
          </div>
          {snapshot.execution.recent.slice(0, 6).map((event) => (
            <div key={event.event_id} className="feed-row execution-table-row">
              <strong>{event.capability_name ?? event.event_kind}</strong>
              <span>{event.action_kind ?? event.event_kind}</span>
              <time>{event.observed_at}</time>
            </div>
          ))}
        </div>
      </section>
    </section>
  );
}
