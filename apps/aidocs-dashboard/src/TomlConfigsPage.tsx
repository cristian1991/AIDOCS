import type { TomlConfigsPageProps } from "./dashboardTypes";
import { isDocumentActive } from "./dashboardUtils";
import { TomlCodeEditor } from "./TomlCodeEditor";

export function TomlConfigsPage({
  tomlCategory,
  setTomlCategory,
  tomlDocuments,
  selectedTomlPath,
  setSelectedTomlPath,
  selectedTomlDocument,
  tomlDrafts,
  setTomlDraft,
  savingTomlPath,
  handleTomlSave,
}: TomlConfigsPageProps) {
  const categoryPrefix = tomlCategory === "action_tokens" ? "action_tokens/" : tomlCategory === "action_hooks" ? "action_hooks/" : "mcp/server/aidocs_mcp/index_languages/";
  const filteredDocuments = tomlDocuments.filter((d) => d.path.startsWith(categoryPrefix));

  return (
    <section className="page page-config">
      <div className="page-fixed-header">
        <div className="config-tabs">
          <button type="button" className={tomlCategory === "action_tokens" ? "config-tab is-active" : "config-tab"} onClick={() => setTomlCategory("action_tokens")}>Action Tokens</button>
          <button type="button" className={tomlCategory === "action_hooks" ? "config-tab is-active" : "config-tab"} onClick={() => setTomlCategory("action_hooks")}>Action Hooks</button>
          <button type="button" className={tomlCategory === "language_descriptors" ? "config-tab is-active" : "config-tab"} onClick={() => setTomlCategory("language_descriptors")}>Language Descriptors</button>
        </div>
      </div>
      <div className="page-fill-region">
        <div className="toml-layout settings-documents-layout">
          <div className="flat-table">
            <div className="table-head toml-table-row" aria-hidden="true">
              <span>Target</span>
              <span>Active</span>
              <span>Context</span>
            </div>
            {filteredDocuments.map((document) => (
              <button
                key={document.path}
                type="button"
                className={document.path === selectedTomlPath ? "table-row toml-table-row is-selected" : "table-row toml-table-row"}
                onClick={() => setSelectedTomlPath(document.path)}
              >
                <span title={document.path}>{document.target}</span>
                <span className="toggle-cell" title={document.active}>
                  <span className={isDocumentActive(document) ? "toggle is-on" : "toggle"} aria-hidden="true">
                    <span className="toggle-knob" />
                  </span>
                </span>
                <span title={document.language_context}>{document.language_context}</span>
              </button>
            ))}
          </div>
          <div className="editor-panel">
            {selectedTomlDocument ? (
              <>
                <TomlCodeEditor
                  value={tomlDrafts[selectedTomlDocument.path] ?? selectedTomlDocument.content}
                  onChange={(v) => setTomlDraft(selectedTomlDocument.path, v)}
                />
                <button className="action-button" type="button" disabled={!selectedTomlDocument.editable || savingTomlPath === selectedTomlDocument.path} onClick={handleTomlSave}>
                  {savingTomlPath === selectedTomlDocument.path ? "Saving..." : "Save TOML"}
                </button>
              </>
            ) : <div className="empty-panel">No TOML documents found for this category.</div>}
          </div>
        </div>
      </div>
    </section>
  );
}
