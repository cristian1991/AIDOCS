import type { TomlConfigsPageProps } from "./dashboardTypes";
import { isDocumentActive } from "./dashboardUtils";
import { TomlCodeEditor } from "./TomlCodeEditor";
import { GateMessagesPage, VocabPage } from "./VocabPage";

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
  // Phase 4d: intent_tokens + gate_messages moved from file-shaped TOML
  // editor to schema-aware sqlite editors (VocabPage / GateMessagesPage).
  // The legacy TomlCodeEditor only renders for language_descriptors now.
  if (tomlCategory === "intent_tokens") {
    return (
      <section className="page page-config">
        <div className="page-fixed-header">
          <div className="config-tabs">
            <button type="button" className="config-tab is-active" onClick={() => setTomlCategory("intent_tokens")}>Action Tokens</button>
            <button type="button" className="config-tab" onClick={() => setTomlCategory("gate_messages")}>Action Hooks</button>
            <button type="button" className="config-tab" onClick={() => setTomlCategory("language_descriptors")}>Language Descriptors</button>
          </div>
        </div>
        <VocabPage />
      </section>
    );
  }
  if (tomlCategory === "gate_messages") {
    return (
      <section className="page page-config">
        <div className="page-fixed-header">
          <div className="config-tabs">
            <button type="button" className="config-tab" onClick={() => setTomlCategory("intent_tokens")}>Action Tokens</button>
            <button type="button" className="config-tab is-active" onClick={() => setTomlCategory("gate_messages")}>Action Hooks</button>
            <button type="button" className="config-tab" onClick={() => setTomlCategory("language_descriptors")}>Language Descriptors</button>
          </div>
        </div>
        <GateMessagesPage />
      </section>
    );
  }
  const categoryPrefix = "mcp/server/aidocs_mcp/index_languages/";
  const filteredDocuments = tomlDocuments.filter((d) => d.path.startsWith(categoryPrefix));
  const isLoading = tomlDocuments.length === 0;

  return (
    <section className="page page-config">
      <div className="page-fixed-header">
        <div className="config-tabs">
          <button type="button" className="config-tab" onClick={() => setTomlCategory("intent_tokens")}>Action Tokens</button>
          <button type="button" className="config-tab" onClick={() => setTomlCategory("gate_messages")}>Action Hooks</button>
          <button type="button" className="config-tab is-active" onClick={() => setTomlCategory("language_descriptors")}>Language Descriptors</button>
        </div>
      </div>
      {isLoading ? (
        <div style={{ padding: "32px 0", textAlign: "center", color: "var(--text-faint)" }}>Loading TOML documents...</div>
      ) : (
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
                {!selectedTomlDocument.editable && (
                  <div className="toml-readonly-note" role="note">
                    {selectedTomlDocument.deprecated
                      || "Read-only: this document's authority is not the TOML file."}
                  </div>
                )}
                <TomlCodeEditor
                  value={tomlDrafts[selectedTomlDocument.path] ?? selectedTomlDocument.content}
                  onChange={(v) => setTomlDraft(selectedTomlDocument.path, v)}
                  readOnly={!selectedTomlDocument.editable}
                />
                <button className="action-button" type="button" disabled={!selectedTomlDocument.editable || savingTomlPath === selectedTomlDocument.path} onClick={handleTomlSave}>
                  {savingTomlPath === selectedTomlDocument.path ? "Saving..." : "Save TOML"}
                </button>
              </>
            ) : <div className="empty-panel">No TOML documents found for this category.</div>}
          </div>
        </div>
      </div>
      )}
    </section>
  );
}
