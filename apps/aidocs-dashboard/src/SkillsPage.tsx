import { useEffect, useState } from "react";
import type { SkillScanResult } from "./dashboardApi";

function riskBadge(level: string) {
  const colors: Record<string, string> = { safe: "#4ade80", low: "#a3e635", medium: "#fbbf24", high: "#f87171", critical: "#ef4444" };
  return <span style={{ color: colors[level] ?? "#999", fontWeight: 600, textTransform: "uppercase", fontSize: "0.75rem" }}>{level}</span>;
}

function severityColor(s: string) {
  return { low: "#a3e635", medium: "#fbbf24", high: "#f87171", critical: "#ef4444" }[s] ?? "#999";
}

type SkillsTab = "bundled" | "user";

export function SkillsPage({
  results,
  onToggleSkill,
  onDeleteSkill,
  onUploadSkill,
}: {
  results: SkillScanResult[];
  onToggleSkill?: (skillId: string, enabled: boolean) => void;
  onDeleteSkill?: (skillId: string) => void;
  onUploadSkill?: () => void;
}) {
  const [tab, setTab] = useState<SkillsTab>("bundled");
  const [selectedSkill, setSelectedSkill] = useState<SkillScanResult | null>(null);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === "Escape") setSelectedSkill(null); };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, []);

  const bundled = results.filter((r) => {
    const source = (r.skill as Record<string, unknown>)?.source;
    return source !== "project_local" && source !== "user";
  });
  const user = results.filter((r) => {
    const source = (r.skill as Record<string, unknown>)?.source;
    return source === "project_local" || source === "user";
  });
  const displayed = tab === "bundled" ? bundled : user;

  return (
    <section className="page page-config">
      <div className="page-fixed-header config-header-row">
        <div className="config-tabs">
          <button type="button" className={tab === "bundled" ? "config-tab is-active" : "config-tab"} onClick={() => setTab("bundled")}>
            Bundled ({bundled.length})
          </button>
          <button type="button" className={tab === "user" ? "config-tab is-active" : "config-tab"} onClick={() => setTab("user")}>
            User ({user.length})
          </button>
        </div>
        {tab === "user" && onUploadSkill && (
          <div className="config-header-actions">
            <button type="button" className="action-button action-button-compact" onClick={onUploadSkill}>
              Upload Skill
            </button>
          </div>
        )}
      </div>
      <div className="page-scroll-region">
        <div className="settings-flat-list">
          {displayed.map((item) => {
            const skill = item.skill as {
              skill_id?: string;
              name?: string;
              description?: string;
              provider?: string;
              source?: string;
              skill_kind?: string;
            };
            const skillId = skill.skill_id ?? skill.name ?? "unknown";
            const isUser = skill.source === "project_local" || skill.source === "user";
            return (
              <div key={skillId} className={`setting-row${item.scan.finding_count > 0 ? " setting-row-danger" : ""}`} style={item.scan.finding_count > 0 ? { borderLeft: "3px solid #f87171", paddingLeft: "8px" } : undefined}>
                <div className="setting-copy">
                  <div className="setting-title-row">
                    <strong style={{ cursor: "pointer", textDecoration: "underline", textDecorationColor: "var(--line)" }} onClick={() => setSelectedSkill(item)}>{skill.name ?? "Unnamed"}</strong>
                    <span className="setting-info" title={`Kind: ${skill.skill_kind ?? "unknown"}`}>
                      {skill.skill_kind?.charAt(0)?.toUpperCase() ?? "S"}
                    </span>
                    {riskBadge(item.scan.risk_level)}
                  </div>
                  <p>{skill.description ?? "No description"}</p>
                  <small style={{ color: "var(--text-faint)" }}>
                    {skill.provider ?? "unknown"} · {skill.source ?? "unknown"}
                  </small>
                  {item.scan.finding_count > 0 && (
                    <details style={{ marginTop: "4px", fontSize: "0.75rem", color: "#f87171" }}>
                      <summary>{item.scan.finding_count} finding(s)</summary>
                      <pre style={{ fontSize: "0.72rem", whiteSpace: "pre-wrap", margin: "4px 0 0", color: "var(--text-soft)" }}>
                        {item.scan.findings.map((f: { severity: string; category: string; description: string }) => `[${f.severity}] ${f.category}: ${f.description}`).join("\n")}
                      </pre>
                    </details>
                  )}
                </div>
                <div className="setting-control" style={{ display: "flex", gap: "6px", justifyContent: "flex-end", alignItems: "center" }}>
                  <button
                    type="button"
                    className={`action-button action-button-compact ${item.selected ? "action-button-active" : ""}`}
                    style={{ minHeight: "30px", padding: "4px 14px", fontSize: "0.78rem" }}
                    onClick={() => onToggleSkill?.(skillId, !item.selected)}
                  >
                    {item.selected ? "On" : "Off"}
                  </button>
                  {isUser && onDeleteSkill && (
                    <button
                      type="button"
                      className="action-button action-button-compact action-button-danger"
                      style={{ minHeight: "30px", padding: "4px 10px", fontSize: "0.78rem" }}
                      onClick={() => onDeleteSkill(skillId)}
                    >
                      Del
                    </button>
                  )}
                </div>
              </div>
            );
          })}
          {displayed.length === 0 && (
            <div style={{ padding: "16px 0", color: "var(--text-faint)" }}>
              {tab === "user" ? "No user skills. Click \"Upload Skill\" to add one." : "No bundled skills loaded."}
            </div>
          )}
        </div>
      </div>
      {selectedSkill && (() => {
        const sk = selectedSkill.skill as Record<string, unknown>;
        const findings = selectedSkill.scan.findings;
        return (
          <div className="modal-backdrop" onClick={() => setSelectedSkill(null)}>
            <div className="modal-panel tool-detail-modal" onClick={(e) => e.stopPropagation()}>
              <div className="page-header modal-header">
                <div>
                  <div className="section-label">{String(sk.provider ?? "unknown")}</div>
                  <h3>{String(sk.name ?? "Unnamed Skill")}</h3>
                </div>
                <button className="action-button action-button-small modal-close" type="button" onClick={() => setSelectedSkill(null)}>Close</button>
              </div>
              <div className="tool-detail-body">
                <div className="tool-detail-row"><span>Description</span><strong>{String(sk.description ?? "No description")}</strong></div>
                <div className="tool-detail-row"><span>Kind</span><strong>{String(sk.skill_kind ?? "unknown")}</strong></div>
                <div className="tool-detail-row"><span>Source</span><strong>{String(sk.source ?? "unknown")}</strong></div>
                <div className="tool-detail-row"><span>Provider</span><strong>{String(sk.provider ?? "unknown")}</strong></div>
                <div className="tool-detail-row"><span>Selected</span><strong>{selectedSkill.selected ? "Yes" : "No"}</strong></div>
                <div className="tool-detail-row"><span>Risk Level</span>{riskBadge(selectedSkill.scan.risk_level)}</div>
                {Array.isArray(sk.tags) && (sk.tags as string[]).length > 0 && (
                  <div className="tool-detail-row"><span>Tags</span><strong>{(sk.tags as string[]).join(", ")}</strong></div>
                )}
                {findings.length > 0 && (
                  <div className="tool-detail-code">
                    <div className="section-label" style={{ color: "#f87171" }}>Security Findings ({findings.length})</div>
                    <div style={{ display: "flex", flexDirection: "column", gap: "6px", marginTop: "6px" }}>
                      {findings.map((f: { severity: string; category: string; description: string; evidence?: string }, i: number) => (
                        <div key={i} style={{ padding: "6px 8px", borderRadius: "4px", background: "rgba(248, 113, 113, 0.08)", borderLeft: `3px solid ${severityColor(f.severity)}` }}>
                          <div style={{ fontSize: "0.78rem", fontWeight: 600, color: severityColor(f.severity) }}>[{f.severity.toUpperCase()}] {f.category}</div>
                          <div style={{ fontSize: "0.75rem", color: "var(--text-soft)", marginTop: "2px" }}>{f.description}</div>
                          {f.evidence && <div style={{ fontSize: "0.72rem", color: "var(--text-faint)", marginTop: "2px", fontFamily: "monospace" }}>{f.evidence}</div>}
                        </div>
                      ))}
                    </div>
                  </div>
                )}
                {typeof sk.content === "string" && sk.content.length > 10 && (
                  <div className="tool-detail-code">
                    <div className="section-label">Content</div>
                    <pre style={{ maxHeight: "300px", overflow: "auto" }}>{String(sk.content)}</pre>
                  </div>
                )}
              </div>
            </div>
          </div>
        );
      })()}
    </section>
  );
}
