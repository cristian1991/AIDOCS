import { useState } from "react";
import type { DashboardConfigEntry } from "./dashboardApi";
import { isDashboardEditable } from "./dashboardUtils";

export function SettingDropdown({
  value,
  options,
  disabled,
  onChange,
  openUpward,
}: {
  value: string;
  options: Array<{ value: string; label?: string }>;
  disabled: boolean;
  onChange: (value: string) => void;
  openUpward?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const selected = options.find((o) => o.value === value);
  return (
    <div className={open ? "setting-dropdown is-open" : "setting-dropdown"}>
      <button type="button" className="dropdown-trigger" disabled={disabled} onClick={() => setOpen(!open)}>
        <span className="dropdown-trigger-label">{selected?.label ?? value}</span>
        <span className="dropdown-trigger-icon" aria-hidden="true">{"\u25be"}</span>
      </button>
      {open ? (
        <div className={openUpward ? "dropdown-menu dropdown-menu-upward" : "dropdown-menu"} role="listbox">
          {options.map((option) => (
            <button
              key={option.value}
              type="button"
              className={option.value === value ? "dropdown-option is-selected" : "dropdown-option"}
              onClick={() => { onChange(option.value); setOpen(false); }}
            >
              <span>{option.label ?? option.value}</span>
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

const KNOWN_LANGUAGES = [
  "csharp", "css", "dart", "elixir", "go", "html", "java", "javascript", "json",
  "jsx", "kotlin", "less", "lua", "php", "powershell", "prisma", "python", "razor",
  "ruby", "rust", "sass", "scss", "shell", "sql", "svelte", "swift", "toml", "tsx",
  "typescript", "vue", "yaml",
];

export function SettingMultiSelect({ value, disabled, onChange }: { value: string; disabled: boolean; onChange: (value: string) => void }) {
  const [open, setOpen] = useState(false);
  const isAll = value.trim().toLowerCase() === "all";
  const selected = isAll ? ["all"] : value.split(",").map((s) => s.trim()).filter(Boolean);
  const label = isAll || selected.length === 0 ? "all" : selected.join(", ");

  function toggle(language: string) {
    const next = new Set(selected);
    if (language === "all") {
      onChange("all");
      return;
    }
    next.delete("all");
    if (next.has(language)) next.delete(language); else next.add(language);
    onChange(next.size ? Array.from(next).sort().join(", ") : "all");
  }

  function toggleAll() {
    onChange("all");
  }

  return (
    <div className={open ? "setting-dropdown is-open" : "setting-dropdown"}>
      <button type="button" className="dropdown-trigger" disabled={disabled} onClick={() => setOpen(!open)}>
        <span className="dropdown-trigger-label">{label}</span>
        <span className="dropdown-trigger-icon" aria-hidden="true">{"\u25be"}</span>
      </button>
      {open ? (
        <div className="dropdown-menu multiselect-menu" role="listbox">
          <label className="multiselect-option">
            <input type="checkbox" checked={isAll} onChange={toggleAll} />
            <span>all</span>
          </label>
          {KNOWN_LANGUAGES.map((language) => (
            <label key={language} className="multiselect-option">
              <input
                type="checkbox"
                checked={!isAll && selected.includes(language)}
                onChange={() => toggle(language)}
              />
              <span>{language}</span>
            </label>
          ))}
        </div>
      ) : null}
    </div>
  );
}

export function SettingInput({
  entry,
  currentValue,
  savingSetting,
  onChange,
}: {
  entry: DashboardConfigEntry;
  currentValue: string;
  savingSetting: string | null;
  onChange: (value: string) => void;
}) {
  const disabled = !isDashboardEditable(entry) || savingSetting === entry.path;
  if (entry.allowed_values?.length) {
    return (
      <SettingDropdown
        value={currentValue}
        options={entry.allowed_values.map((v) => ({ value: v }))}
        disabled={disabled}
        onChange={onChange}
      />
    );
  }
  if (entry.type === "boolean") {
    return (
      <SettingDropdown
        value={currentValue}
        options={[{ value: "true", label: "true" }, { value: "false", label: "false" }]}
        disabled={disabled}
        onChange={onChange}
      />
    );
  }
  if (entry.path === "languages.enabled" || entry.path === "index.enabled_languages") {
    return <SettingMultiSelect value={currentValue} disabled={disabled} onChange={onChange} />;
  }
  if (entry.type === "string_list") {
    const lines = currentValue.split(",").map((s) => s.trim()).filter(Boolean).join("\n");
    return (
      <textarea
        className="setting-list-textarea"
        value={lines}
        disabled={disabled}
        placeholder="One item per line"
        onChange={(event) => onChange(event.target.value.split("\n").map((s) => s.trim()).filter(Boolean).join(", "))}
      />
    );
  }
  return <input value={currentValue} disabled={disabled} onChange={(event) => onChange(event.target.value)} />;
}
