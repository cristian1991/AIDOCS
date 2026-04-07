import type { DangerConfirmModalProps, ImportExportModalProps, ToastStackProps } from "./dashboardTypes";


export function ImportExportModal({
  selectedConfigTextDocument,
  configTextDocuments,
  selectedConfigTextPath,
  setConfigTextPath,
  close,
  savingTomlPath,
  tomlDrafts,
  setTomlDraft,
  handleConfigTextSave,
  scopeLabel,
}: ImportExportModalProps) {
  if (!selectedConfigTextDocument) {
    return null;
  }

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Import or export AIDOCS config" onClick={close}>
      <div className="modal-panel">
        <div className="page-header modal-header" onClick={(event) => event.stopPropagation()}>
          <div>
            <div className="section-label">Import / Export</div>
            <h3>{scopeLabel}</h3>
          </div>
          <button className="action-button action-button-small modal-close" type="button" onClick={close}>
            Close
          </button>
        </div>
        <div className="config-text-toolbar" onClick={(event) => event.stopPropagation()}>
          <button
            className="action-button action-button-small"
            type="button"
            onClick={() => navigator.clipboard.writeText(tomlDrafts[selectedConfigTextDocument.path] ?? selectedConfigTextDocument.content)}
          >
            Copy
          </button>
          <button
            className="action-button action-button-small"
            type="button"
            disabled={!selectedConfigTextDocument.editable || savingTomlPath === selectedConfigTextDocument.path}
            onClick={handleConfigTextSave}
          >
            {savingTomlPath === selectedConfigTextDocument.path ? "Saving..." : "Save"}
          </button>
        </div>
        <div className="modal-editor modal-editor-large" onClick={(event) => event.stopPropagation()}>
          <textarea
            className="toml-editor"
            value={tomlDrafts[selectedConfigTextDocument.path] ?? selectedConfigTextDocument.content}
            onChange={(event) => setTomlDraft(selectedConfigTextDocument.path, event.target.value)}
            spellCheck={false}
          />
        </div>
      </div>
    </div>
  );
}

export function DangerConfirmModal({ settingPath, close, confirm }: DangerConfirmModalProps) {
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Confirm security setting change" onClick={close}>
      <div className="modal-panel danger-modal" onClick={(event) => event.stopPropagation()}>
        <div className="page-header modal-header">
          <div>
            <div className="section-label">Confirm Security Change</div>
            <h3>{settingPath}</h3>
          </div>
          <button className="action-button action-button-small modal-close" type="button" onClick={close}>
            Cancel
          </button>
        </div>
        <p className="warning-text modal-warning-copy">
          {settingPath === "dev.dev_mode" && "Enables editing AIDOCS MCP server source files. Only use when actively developing AIDOCS."}
          {settingPath === "dev.allow_config_edit" && "Allows agents to modify aidocs.toml via tool calls. The agent can change gate settings, conductor config, and other project settings."}
          {settingPath === "gate.enforce" && "Disabling tool gates removes bash allowlist, raw tool blocking, and destructive command protection. Agents can use any tool freely."}
        </p>
        <div className="danger-modal-actions">
          <button className="action-button action-button-small" type="button" onClick={close}>
            Keep current setting
          </button>
          <button className="action-button action-button-small action-button-danger" type="button" onClick={confirm}>
            Confirm change
          </button>
        </div>
      </div>
    </div>
  );
}

export function LoadingOverlay() {
  return (
    <div className="app-overlay" role="status" aria-live="polite">
      <div className="app-overlay-panel">
        <div className="overlay-spinner" aria-hidden="true" />
        <strong>Loading runtime snapshot</strong>
        <span>Refreshing project, session, and settings state.</span>
      </div>
    </div>
  );
}

export function ToastStack({ notice, error, clearNotice, clearError }: ToastStackProps) {
  if (!notice && !error) {
    return null;
  }

  return (
    <div className="toast-stack" aria-live="polite">
      {notice ? (
        <div className="toast toast-success">
          <span>{notice}</span>
          <button type="button" className="toast-close" onClick={clearNotice}>
            Close
          </button>
        </div>
      ) : null}
      {error ? (
        <div className="toast toast-error" role="alert">
          <span>{error}</span>
          <button type="button" className="toast-close" onClick={clearError}>
            Close
          </button>
        </div>
      ) : null}
    </div>
  );
}
