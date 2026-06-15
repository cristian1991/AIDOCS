import { useEffect, useMemo, useState } from "react";
import type { SkillProviderStatus, SkillScanResult } from "./dashboardApi";

function riskBadge(level: string) {
  const colors: Record<string, string> = { safe: "#4ade80", low: "#a3e635", medium: "#fbbf24", high: "#f87171", critical: "#ef4444" };
  return <span style={{ color: colors[level] ?? "#999", fontWeight: 600, textTransform: "uppercase", fontSize: "0.75rem" }}>{level}</span>;
}

function severityColor(s: string) {
  return { low: "#a3e635", medium: "#fbbf24", high: "#f87171", critical: "#ef4444" }[s] ?? "#999";
}

function providerStateColor(state: string) {
  return {
    compatible: "#4ade80",
    incompatible_but_user_override: "#fbbf24",
    detected_incompatible: "#f87171",
    disabled: "#94a3b8",
    missing: "#f87171",
    unknown: "#a3a3a3",
  }[state] ?? "#999";
}

function activationTagStyle(tag: string) {
  const mapping: Record<string, { color: string; border: string; title: string }> = {
    "session helper": { color: "#60a5fa", border: "#60a5fa", title: "Persistent helper behavior seeded from session state" },
    "prompt-triggered": { color: "#c084fc", border: "#c084fc", title: "Activated for matching prompts or provider-specific triggers" },
    "provider content": { color: "#f59e0b", border: "#f59e0b", title: "Provider content is adapted into AIDOCS runtime guidance" },
    "runtime-owned": { color: "#fb7185", border: "#fb7185", title: "Selection resolves to runtime-owned capability instead of prompt prose" },
    "session active": { color: "#34d399", border: "#34d399", title: "Present in the current cached session host state" },
  };
  return mapping[tag] ?? { color: "#94a3b8", border: "#94a3b8", title: "Activation tag" };
}

function activationBadges(tags: string[] | undefined) {
  if (!Array.isArray(tags) || tags.length === 0) return null;
  return tags.map((tag) => {
    const style = activationTagStyle(tag);
    return (
      <span
        key={tag}
        className="setting-inherited"
        title={style.title}
        style={{ color: style.color, borderColor: style.border, opacity: 1 }}
      >
        {tag}
      </span>
    );
  });
}

type SkillsTab = "bundled" | "user";

export function SkillsPage({
  results,
  onToggleSkill,
  onSetProviderOverride,
  onDeleteSkill,
  onUploadSkill,
  providerOverridePending,
}: {
  results: SkillScanResult[];
  onToggleSkill?: (skillId: string, enabled: boolean) => void;
  onSetProviderOverride?: (providerId: string, choice: string | null) => void;
  onDeleteSkill?: (skillId: string) => void;
  onUploadSkill?: () => void;
  providerOverridePending?: string | null;
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
    const origin = (r.skill as Record<string, unknown>)?.origin;
    return source !== "project" && source !== "project_local" && source !== "user" && origin !== "project_local";
  });
  const user = results.filter((r) => {
    const source = (r.skill as Record<string, unknown>)?.source;
    const origin = (r.skill as Record<string, unknown>)?.origin;
    return source === "project" || source === "project_local" || source === "user" || origin === "project_local";
  });
  const displayed = tab === "bundled" ? bundled : user;
  const providerStatuses = useMemo(() => {
    const byProvider = new Map<string, SkillProviderStatus>();
    for (const item of displayed) {
      const status = item.provider_status;
      if (!status?.provider_id || byProvider.has(status.provider_id)) continue;
      byProvider.set(status.provider_id, status);
    }
    return Array.from(byProvider.values());
  }, [displayed]);
  const providerAttention = providerStatuses.filter((status) => status.provider_state !== "compatible" || status.user_choice);

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
        {providerAttention.length > 0 && (
          <div style={{ display: "flex", flexDirection: "column", gap: "10px", marginBottom: "14px" }}>
            {providerAttention.map((status) => {
              const pending = providerOverridePending === status.provider_id;
              return (
                <div key={status.provider_id} className="setting-row" style={{ borderLeft: `3px solid ${providerStateColor(status.provider_state)}`, paddingLeft: "10px", background: "rgba(255,255,255,0.02)" }}>
                  <div className="setting-copy">
                    <div className="setting-title-row">
                      <strong>{status.provider_id}</strong>
                      <span className="setting-info" title="Provider compatibility state">P</span>
                      <span className="setting-inherited" style={{ color: providerStateColor(status.provider_state), borderColor: providerStateColor(status.provider_state), opacity: 1 }}>
                        {status.provider_state}
                      </span>
                      {status.user_choice ? <span className="setting-own" title="Current override choice">override: {status.user_choice}</span> : null}
                    </div>
                    <p style={{ marginBottom: 0 }}>
                      Provider version: {status.provider_version ?? "unknown"}
                      {status.compatible_version_range ? ` · expected ${status.compatible_version_range}` : ""}
                    </p>
                  </div>
                  <div className="setting-control" style={{ display: "flex", gap: "6px", justifyContent: "flex-end", alignItems: "center", flexWrap: "wrap" }}>
                    {status.choices.includes("keep_enabled_anyway") ? (
                      <button type="button" className={`action-button action-button-compact ${status.user_choice === "keep_enabled_anyway" ? "action-button-active" : ""}`} disabled={pending} onClick={() => onSetProviderOverride?.(status.provider_id, "keep_enabled_anyway")}>
                        Keep Enabled
                      </button>
                    ) : null}
                    {status.choices.includes("disable") ? (
                      <button type="button" className={`action-button action-button-compact ${status.user_choice === "disable" ? "action-button-active" : ""}`} disabled={pending} onClick={() => onSetProviderOverride?.(status.provider_id, "disable")}>
                        Disable
                      </button>
                    ) : null}
                    {status.user_choice ? (
                      <button type="button" className="action-button action-button-compact" disabled={pending} onClick={() => onSetProviderOverride?.(status.provider_id, null)}>
                        Auto
                      </button>
                    ) : null}
                  </div>
                </div>
              );
            })}
          </div>
        )}
        <div className="settings-flat-list">
          {displayed.map((item) => {
            const skill = item.skill as {
              skill_id?: string;
              name?: string;
              description?: string;
              provider?: string;
              source?: string;
               origin?: string;
               skill_kind?: string;
               provider_state?: string;
               selectable?: boolean;
             };
            const skillId = skill.skill_id ?? skill.name ?? "unknown";
            const isUser = skill.source === "project" || skill.source === "project_local" || skill.source === "user" || skill.origin === "project_local";
            const providerStatus = item.provider_status;
            const providerState = providerStatus?.provider_state ?? skill.provider_state ?? "compatible";
            const selectable = skill.selectable !== false;
            const selectDisabled = !item.selected && !selectable;
            return (
              <div key={skillId} className={`setting-row${item.scan.finding_count > 0 ? " setting-row-danger" : ""}`} style={item.scan.finding_count > 0 ? { borderLeft: "3px solid #f87171", paddingLeft: "8px" } : undefined}>
                <div className="setting-copy">
                  <div className="setting-title-row">
                    <strong style={{ cursor: "pointer", textDecoration: "underline", textDecorationColor: "var(--line)" }} onClick={() => setSelectedSkill(item)}>{skill.name ?? "Unnamed"}</strong>
                    <span className="setting-info" title={`Kind: ${skill.skill_kind ?? "unknown"}`}>
                      {skill.skill_kind?.charAt(0)?.toUpperCase() ?? "S"}
                    </span>
                     {item.selected ? <span className="setting-own" title="Selected for this session">selected</span> : null}
                     {item.active ? <span className="setting-inherited" title="Currently active in host guidance">active</span> : null}
                     {activationBadges(item.activation_tags)}
                     {providerState !== "compatible" ? <span className="setting-inherited" style={{ color: providerStateColor(providerState), borderColor: providerStateColor(providerState), opacity: 1 }} title="Provider compatibility state">{providerState}</span> : null}
                     {riskBadge(item.scan.risk_level)}
                    </div>
                  <p>{skill.description ?? "No description"}</p>
                   <small style={{ color: "var(--text-faint)" }}>
                     {skill.provider ?? "unknown"} · {skill.source ?? "unknown"}
                   </small>
                   {!selectable && !item.selected ? (
                     <div style={{ marginTop: "4px", fontSize: "0.75rem", color: providerStateColor(providerState) }}>
                       Skill cannot be selected until the provider is enabled.
                     </div>
                   ) : null}
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
                    disabled={selectDisabled}
                    onClick={() => onToggleSkill?.(skillId, !item.selected)}
                  >
                    {item.selected ? "Selected" : "Select"}
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
                <div className="tool-detail-row"><span>Active</span><strong>{selectedSkill.active ? "Yes" : "No"}</strong></div>
                {Array.isArray(selectedSkill.activation_tags) && selectedSkill.activation_tags.length > 0 ? (
                  <div className="tool-detail-row"><span>Activation</span><strong>{selectedSkill.activation_tags.join(", ")}</strong></div>
                ) : null}
                {selectedSkill.provider_status ? <div className="tool-detail-row"><span>Provider State</span><strong>{selectedSkill.provider_status.provider_state}</strong></div> : null}
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
