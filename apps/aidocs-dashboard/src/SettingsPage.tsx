import type { DashboardConfigEntry } from "./dashboardApi";
import { asText, buildSettingTooltip } from "./dashboardUtils";
import type { SettingsPageProps } from "./dashboardTypes";
import { SettingInput } from "./dashboardSettingsComponents";

export function SettingsPage({
  settingsScope,
  setSettingsScope,
  hasProject,
  hasSession,
  configSections,
  draftValues,
  savingSetting,
  requestConfigSave,
  setDraftValue,
  devModeEntry,
  openImportExport,
}: SettingsPageProps) {
  const saveScope = settingsScope === "global" ? "user" : settingsScope;

  function scopeValue(entry: DashboardConfigEntry): string {
      const draft = draftValues[entry.path];
      if (draft !== undefined) return draft;
      const raw = entry.scope_values?.[settingsScope === "global" ? "user" : settingsScope];
      if (raw !== undefined && raw !== null) return asText(raw);
      // Fall back to current_value (includes SQLite overlay) before default
      if (entry.current_value !== undefined && entry.current_value !== null) return asText(entry.current_value);
      return asText(entry.default);
    }

  const allEntries = configSections.flatMap(({ entries }) => entries);
  const dirtyEntries = allEntries.filter((e) => {
    const draft = draftValues[e.path];
    if (draft === undefined) return false;
    const raw = e.scope_values?.[settingsScope === "global" ? "user" : settingsScope];
    const baseline = raw !== undefined && raw !== null ? asText(raw) : asText(e.default);
    return draft !== baseline;
  });
  const devDirty = devModeEntry && draftValues[devModeEntry.path] !== undefined && draftValues[devModeEntry.path] !== asText(devModeEntry.scope_values?.[saveScope] ?? devModeEntry.default);
  const hasDirty = dirtyEntries.length > 0 || devDirty;

  function saveAll() {
    for (const entry of dirtyEntries) requestConfigSave(entry, saveScope);
    if (devDirty && devModeEntry) requestConfigSave(devModeEntry, saveScope);
  }

  return (
    <section className="page page-config">
      <div className="page-fixed-header config-header-row">
        <div className="config-tabs">
          <button type="button" className={settingsScope === "global" ? "config-tab is-active" : "config-tab"} onClick={() => setSettingsScope("global")}>Global</button>
          <button type="button" className={settingsScope === "project" ? "config-tab is-active" : "config-tab"} disabled={!hasProject} onClick={() => setSettingsScope("project")}>Project</button>
          <button type="button" className={settingsScope === "session" ? "config-tab is-active" : "config-tab"} disabled={!hasSession} onClick={() => setSettingsScope("session")}>Session</button>
        </div>
        <div className="config-header-actions">
          <button type="button" className="action-button config-save-button" style={{ whiteSpace: "nowrap" }} onClick={openImportExport}>Import / Export</button>
          <button type="button" className="action-button config-save-button" disabled={!hasDirty || !!savingSetting} onClick={saveAll}>
            {savingSetting ? "Saving..." : "Save"}
          </button>
        </div>
      </div>
      <div className="page-scroll-region">
        <div className="settings-flat-list">
          {configSections.map(({ section, entries }) => {
            const isSecuritySection = section === "dev" || section === "gate";
            return (
              <div key={section}>
                <div className={isSecuritySection ? "settings-section-header settings-section-danger" : "settings-section-header"}>
                  {isSecuritySection ? `Security · ${section.replace(/_/g, " ")}` : section.replace(/_/g, " ")}
                </div>
                {entries.map((entry) => {
                  const currentValue = scopeValue(entry);
                  const valueHelp = buildSettingTooltip(entry, currentValue);
                  const hasOptions = !!(entry.allowed_values?.length || entry.type === "boolean");
                  return (
                    <div key={entry.path} className={isSecuritySection ? "setting-row setting-row-danger" : "setting-row"}>
                      <div className="setting-copy">
                        <div className="setting-title-row">
                          <strong className={isSecuritySection ? "warning-text" : ""}>{entry.key}</strong>
                          {hasOptions ? <span className="setting-info" title={valueHelp} aria-label={valueHelp}>?</span> : null}
                        </div>
                        <p className={isSecuritySection ? "warning-text-soft" : ""}>{entry.description}</p>
                      </div>
                      <SettingInput entry={entry} currentValue={currentValue} savingSetting={savingSetting} onChange={(value) => setDraftValue(entry.path, value)} />
                    </div>
                  );
                })}
              </div>
            );
          })}
        </div>
      </div>
    </section>
  );
}
