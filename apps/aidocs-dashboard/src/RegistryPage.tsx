import { useEffect, useMemo, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import type { RegistrySearchResult } from "./dashboardApi";

function openUrl(url: string) {
  invoke("open_url", { url }).catch(() => window.open(url, "_blank"));
}

type McpTab = "registry" | "installed";

type RegistryPageProps = {
  results: RegistrySearchResult[];
  query: string;
  setQuery: (value: string) => void;
  nextCursor: string | null;
  loading: boolean;
  onSearch: () => void;
  onMore: () => void;
};

export function RegistryPage({ results, query, setQuery, nextCursor, loading, onSearch, onMore }: RegistryPageProps) {
  const [tab, setTab] = useState<McpTab>("installed");

  // Load registry servers when switching to registry tab
  useEffect(() => {
    if (tab === "registry" && results.length === 0 && !loading) {
      onSearch();
    }
  }, [tab]);

  const rows = useMemo(() => results, [results]);

  // TODO: read installed MCP servers from .mcp.json via Tauri command
  const installed: Array<{ name: string; command: string; transport: string }> = [];

  return (
    <section className="page page-config">
      <div className="page-fixed-header config-header-row">
        <div className="config-tabs">
          <button type="button" className={tab === "installed" ? "config-tab is-active" : "config-tab"} onClick={() => setTab("installed")}>
            Installed ({installed.length})
          </button>
          <button type="button" className={tab === "registry" ? "config-tab is-active" : "config-tab"} onClick={() => setTab("registry")}>
            Registry
          </button>
        </div>
        {tab === "registry" && (
          <div className="config-header-actions" style={{ flex: "1 1 auto", display: "flex", gap: "8px" }}>
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") onSearch(); }}
              placeholder="Search MCP servers..."
              style={{ flex: "1 1 auto", minWidth: 0 }}
            />
            <button className="action-button action-button-compact" type="button" onClick={onSearch} disabled={loading} style={{ flex: "0 0 auto" }}>
              {loading ? "..." : "Search"}
            </button>
          </div>
        )}
      </div>
      <div className="page-scroll-region">
        {tab === "installed" ? (
          <div className="settings-flat-list">
            {installed.length === 0 && (
              <div style={{ padding: "24px 0", color: "var(--text-faint)", textAlign: "center" }}>
                No MCP servers installed. Browse the Registry tab to discover and install servers.
              </div>
            )}
            {installed.map((srv) => (
              <div key={srv.name} className="setting-row">
                <div className="setting-copy">
                  <strong>{srv.name}</strong>
                  <small style={{ color: "var(--text-faint)" }}>{srv.transport} · {srv.command}</small>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <>
            {rows.length === 0 && !loading && (
              <div style={{ padding: "24px 0", color: "var(--text-faint)", textAlign: "center" }}>
                No servers found. Try a different search term.
              </div>
            )}
            {loading && rows.length === 0 && (
              <div style={{ padding: "24px 0", color: "var(--text-faint)", textAlign: "center" }}>
                Loading MCP servers...
              </div>
            )}
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))", gap: "12px" }}>
              {rows.map((server) => {
                const cmds = server.install_commands ?? [];
                const firstCmd = cmds[0];
                const installText = typeof firstCmd === "string" ? firstCmd : typeof firstCmd === "object" && firstCmd ? String((firstCmd as Record<string, string>).command ?? "") : "";
                const installType = typeof firstCmd === "object" && firstCmd ? String((firstCmd as Record<string, string>).type ?? "") : "";
                const websiteUrl = String((server as Record<string, unknown>).website_url ?? "");
                return (
                  <section key={`${server.name}-${server.version}`} className="flat-panel" style={{ display: "flex", flexDirection: "column", gap: "6px" }}>
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "start" }}>
                      <div>
                        <h3 style={{ margin: 0, fontSize: "0.92rem" }}>{server.name?.split("/").pop() ?? server.name}</h3>
                        <small style={{ color: "var(--text-faint)", fontSize: "0.72rem" }}>{server.name}</small>
                      </div>
                      <span style={{ color: "var(--accent-bright)", fontSize: "0.72rem", whiteSpace: "nowrap" }}>{server.version ?? "latest"}</span>
                    </div>
                    <p style={{ margin: 0, fontSize: "0.78rem", color: "var(--text-soft)", lineHeight: 1.4 }}>
                      {server.description ? (server.description.length > 140 ? server.description.slice(0, 140) + "..." : server.description) : "No description"}
                    </p>
                    {installText && (
                      <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
                        {installType && <span style={{ color: "var(--accent-bright)", fontSize: "0.68rem", textTransform: "uppercase", fontWeight: 600 }}>{installType}</span>}
                        <pre style={{ margin: 0, padding: "4px 8px", background: "rgba(0,0,0,0.2)", borderRadius: "4px", fontSize: "0.72rem", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1 }}>
                          {installText}
                        </pre>
                      </div>
                    )}
                    <div style={{ display: "flex", gap: "6px", marginTop: "auto" }}>
                      {installText && (
                        <button className="action-button action-button-compact" type="button" style={{ fontSize: "0.72rem" }} onClick={() => navigator.clipboard.writeText(installText)}>
                          Copy Install
                        </button>
                      )}
                      {websiteUrl && websiteUrl !== "undefined" && (
                        <button className="action-button action-button-compact" type="button" style={{ fontSize: "0.72rem" }} onClick={() => openUrl(websiteUrl)}>
                          Website
                        </button>
                      )}
                      <button className="action-button action-button-compact" type="button" style={{ fontSize: "0.72rem" }} onClick={() => openUrl(`https://registry.modelcontextprotocol.io/servers/${encodeURIComponent(server.name ?? "")}`)}>
                        Registry
                      </button>
                    </div>
                  </section>
                );
              })}
            </div>
            {nextCursor && (
              <div style={{ textAlign: "center", padding: "12px 0" }}>
                <button className="action-button action-button-compact" type="button" onClick={onMore} disabled={loading}>
                  {loading ? "..." : "Load More"}
                </button>
              </div>
            )}
          </>
        )}
      </div>
    </section>
  );
}
