import { useState } from "react";
import { PieChart as RechartsPie, Pie, Cell, Tooltip, ResponsiveContainer, BarChart, Bar, XAxis, YAxis, CartesianGrid, Legend } from "recharts";
import type { UsagePageProps } from "./dashboardTypes";

export const CHART_COLORS = ["#338441", "#8ce0af", "#4a90d9", "#e8a838", "#d94a6b", "#7c5cbf", "#5cb8a8", "#d97a4a"];

export function ChartTooltip({ active, payload }: { active?: boolean; payload?: Array<{ name: string; value: number; payload?: { tokens?: number; count?: number; name?: string } }> }) {
  if (!active || !payload?.length) return null;
  const item = payload[0];
  const data = item.payload as { name?: string; tokens?: number; count?: number } | undefined;
  const name = data?.name ?? item.name;
  const tokenValue = typeof data?.tokens === "number" && data.tokens > 0 ? data.tokens : Number(item.value ?? 0);
  const callCount = typeof data?.count === "number" ? data.count : 0;
  return (
    <div className="chart-tooltip">
      <strong>{name}</strong>
      <span>~{tokenValue.toLocaleString()} tokens</span>
      {callCount > 0 ? <span style={{ opacity: 0.6 }}>{callCount} calls</span> : null}
    </div>
  );
}

export function UsagePage({ reason, tokenEstimates, sessionBreakdown, actionRows, eventRows, intentRows, onClearTokens, clearingTokens }: UsagePageProps) {
  const [actionMode, setActionMode] = useState<"bar" | "pie">("bar");
  const [eventMode, setEventMode] = useState<"bar" | "pie">("bar");

  const tokenData = [
    { name: "Tokens in", value: tokenEstimates.tokens_in, tokens: tokenEstimates.tokens_in, count: tokenEstimates.tokens_in_calls ?? 0 },
    { name: "Tokens out", value: tokenEstimates.tokens_out, tokens: tokenEstimates.tokens_out, count: tokenEstimates.tokens_out_calls ?? 0 },
  ];
  const sessionData = sessionBreakdown
    .filter((s) => s.total > 0)
    .map((s) => ({ name: s.session_id.replace(/^\d{4}-\d{2}-\d{2}-/, ""), value: s.total, tokens: s.total, count: s.events }));
  const toolData = eventRows.map((r) => ({ name: r.label, value: r.tokens ?? r.count, count: r.count, tokens: r.tokens }));
  const intentData = intentRows.map((r) => ({ name: r.label, value: r.tokens ?? r.count, count: r.count, tokens: r.tokens }));
  return (
    <section className="page page-usage">
      <div className="usage-header">
        <div className="usage-token-summary">
          <div className="token-stat"><span className="section-label">Tokens In</span><strong>{tokenEstimates.tokens_in.toLocaleString()}</strong></div>
          <div className="token-stat"><span className="section-label">Tokens Out</span><strong>{tokenEstimates.tokens_out.toLocaleString()}</strong></div>
          <div className="token-stat"><span className="section-label">Total</span><strong>{tokenEstimates.total.toLocaleString()}</strong></div>
          </div>
          {onClearTokens && (
            <button type="button" className="action-button action-button-compact" disabled={clearingTokens} onClick={onClearTokens} style={{ flex: "0 0 auto" }}>
              {clearingTokens ? "..." : "Clear Tokens"}
            </button>
          )}
      </div>
      <div className="usage-grid usage-grid-2col usage-grid-fill">
        <section className="flat-panel">
          <div className="section-label">In / Out Distribution</div>
          <div className="chart-container">
            <ResponsiveContainer width="100%" height="100%">
              <RechartsPie>
                <Pie data={tokenData} dataKey="value" nameKey="name" cx="50%" cy="45%" innerRadius="55%" outerRadius="80%">
                  {tokenData.map((_, i) => <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />)}
                </Pie>
                <Tooltip content={<ChartTooltip />} />
                <Legend iconType="circle" iconSize={8} wrapperStyle={{ fontSize: "0.78rem", color: "#b9d0c2" }} />
              </RechartsPie>
            </ResponsiveContainer>
          </div>
        </section>
        <section className="flat-panel">
          <div className="section-label">By Session</div>
          <div className="chart-container">
            {sessionData.length > 0 ? (
              <ResponsiveContainer width="100%" height="100%">
                <RechartsPie>
                  <Pie data={sessionData} dataKey="value" nameKey="name" cx="50%" cy="45%" innerRadius="55%" outerRadius="80%">
                    {sessionData.map((_, i) => <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />)}
                  </Pie>
                  <Tooltip content={<ChartTooltip />} />
                  <Legend iconType="circle" iconSize={8} wrapperStyle={{ fontSize: "0.78rem", color: "#b9d0c2" }} />
                </RechartsPie>
              </ResponsiveContainer>
            ) : <div className="empty-panel">No per-session token data yet</div>}
          </div>
        </section>
      </div>
      <div className="usage-grid usage-grid-2col usage-grid-fill">
        <section className="flat-panel">
          <div className="chart-panel-header">
            <div className="section-label">Tool Usage Breakdown</div>
            <div className="chart-mode-toggle">
              <button type="button" className={actionMode === "bar" ? "toggle-btn is-active" : "toggle-btn"} onClick={() => setActionMode("bar")}>Bars</button>
              <button type="button" className={actionMode === "pie" ? "toggle-btn is-active" : "toggle-btn"} onClick={() => setActionMode("pie")}>Pie</button>
            </div>
          </div>
          <div className="chart-container">
            {actionMode === "pie" ? (
              <ResponsiveContainer width="100%" height="100%">
                <RechartsPie>
                  <Pie data={toolData} dataKey="value" nameKey="name" cx="50%" cy="45%" innerRadius="55%" outerRadius="80%">
                    {toolData.map((_, i) => <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />)}
                  </Pie>
                  <Tooltip content={<ChartTooltip />} />
                  <Legend iconType="circle" iconSize={8} wrapperStyle={{ fontSize: "0.78rem", color: "#b9d0c2" }} />
                </RechartsPie>
              </ResponsiveContainer>
            ) : (
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={toolData} layout="vertical" margin={{ left: 0, right: 20 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="rgba(153,211,180,0.08)" />
                  <XAxis type="number" tick={{ fill: "#7c9688", fontSize: 11 }} />
                  <YAxis type="category" dataKey="name" tick={{ fill: "#b9d0c2", fontSize: 12 }} width={150} />
                  <Tooltip content={<ChartTooltip />} />
                  <Bar dataKey="value" radius={[0, 4, 4, 0]}>
                    {toolData.map((_, i) => <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />)}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            )}
          </div>
        </section>
        <section className="flat-panel">
          <div className="chart-panel-header">
            <div className="section-label">Usage by Intent</div>
            <div className="chart-mode-toggle">
              <button type="button" className={eventMode === "bar" ? "toggle-btn is-active" : "toggle-btn"} onClick={() => setEventMode("bar")}>Bars</button>
              <button type="button" className={eventMode === "pie" ? "toggle-btn is-active" : "toggle-btn"} onClick={() => setEventMode("pie")}>Pie</button>
            </div>
          </div>
          <div className="chart-container">
            {eventMode === "pie" ? (
              <ResponsiveContainer width="100%" height="100%">
                <RechartsPie>
                  <Pie data={intentData} dataKey="value" nameKey="name" cx="50%" cy="45%" innerRadius="55%" outerRadius="80%">
                    {intentData.map((_, i) => <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />)}
                  </Pie>
                  <Tooltip content={<ChartTooltip />} />
                  <Legend iconType="circle" iconSize={8} wrapperStyle={{ fontSize: "0.78rem", color: "#b9d0c2" }} />
                </RechartsPie>
              </ResponsiveContainer>
            ) : (
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={intentData} layout="vertical" margin={{ left: 0, right: 20 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="rgba(153,211,180,0.08)" />
                  <XAxis type="number" tick={{ fill: "#7c9688", fontSize: 11 }} />
                  <YAxis type="category" dataKey="name" tick={{ fill: "#b9d0c2", fontSize: 12 }} width={80} />
                  <Tooltip content={<ChartTooltip />} />
                  <Bar dataKey="value" radius={[0, 4, 4, 0]}>
                    {intentData.map((_, i) => <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />)}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            )}
          </div>
        </section>
      </div>
    </section>
  );
}
