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
              // WebView2 (Tauri 2) silently drops React's synthetic
              // onClick for buttons inside an absolute-positioned
              // popover — the option click was lost, so boolean/enum
              // settings never changed. mousedown is the reliable
              // channel; preventDefault stops the focus shuffle.
              // (Mirrors the CastleDropdown fix.)
              onMouseDown={(e) => { e.preventDefault(); onChange(option.value); setOpen(false); }}
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
          {/* WebView2 drops onClick/onChange for controls inside this
              absolute popover — drive the toggle from the label's
              onMouseDown instead (preventDefault stops the lost native
              toggle); the checkbox is controlled + readOnly. */}
          <label
            className="multiselect-option"
            onMouseDown={(e) => { e.preventDefault(); toggleAll(); }}
          >
            <input type="checkbox" checked={isAll} readOnly />
            <span>all</span>
          </label>
          {KNOWN_LANGUAGES.map((language) => (
            <label
              key={language}
              className="multiselect-option"
              onMouseDown={(e) => { e.preventDefault(); toggle(language); }}
            >
              <input
                type="checkbox"
                checked={!isAll && selected.includes(language)}
                readOnly
              />
              <span>{language}</span>
            </label>
          ))}
        </div>
      ) : null}
    </div>
  );
}

/** Special value that means "clear this scope's override, inherit from parent". */
export const INHERIT_VALUE = "__inherit__";

export function SettingInput({
  entry,
  currentValue,
  savingSetting,
  onChange,
  openUpward,
  inheritLabel,
  isInherited,
}: {
  entry: DashboardConfigEntry;
  currentValue: string;
  savingSetting: string | null;
  onChange: (value: string) => void;
  openUpward?: boolean;
  inheritLabel?: string | null;
  isInherited?: boolean;
}) {
  const disabled = !isDashboardEditable(entry) || savingSetting === entry.path;
  const inheritOption = inheritLabel ? [{ value: INHERIT_VALUE, label: `Use ${inheritLabel} default` }] : [];
  // When inherited, show the inherited value but mark dropdown as displaying an inherited state
  const displayValue = isInherited ? INHERIT_VALUE : currentValue;

  if (entry.allowed_values?.length) {
    return (
      <SettingDropdown
        value={displayValue}
        options={[...inheritOption, ...entry.allowed_values.map((v) => ({ value: v }))]}
        disabled={disabled}
        onChange={onChange}
        openUpward={openUpward}
      />
    );
  }
  if (entry.type === "boolean") {
    return (
      <SettingDropdown
        value={displayValue}
        options={[...inheritOption, { value: "true", label: "true" }, { value: "false", label: "false" }]}
        disabled={disabled}
        onChange={onChange}
        openUpward={openUpward}
      />
    );
  }
  if (entry.path === "languages.enabled" || entry.path === "index.enabled_languages") {
    return <SettingMultiSelect value={currentValue} disabled={disabled} onChange={onChange} />;
  }
  if (entry.type === "string_list") {
    return (
      <StringListTextarea
        currentValue={currentValue}
        disabled={disabled}
        onChange={onChange}
      />
    );
  }
  return <input value={currentValue} disabled={disabled} onChange={(event) => onChange(event.target.value)} />;
}

/**
 * Editable textarea for `string_list` settings.
 *
 * The previous implementation derived the textarea's `value` prop from
 * `currentValue` via
 *   currentValue.split(",").map(trim).filter(Boolean).join("\n")
 * and sent edits back through the same pipeline on every keystroke.
 *
 * That round-trip silently deleted empty lines. When the user pressed
 * Enter at end-of-line the trailing empty string got `.filter(Boolean)`-ed
 * out, the textarea re-rendered with the same content, and the caret
 * never moved — Enter appeared dead. Same problem typing a blank line
 * between two entries. Paste worked because pasted text rarely has
 * dangling empty lines.
 *
 * Fix: hold the raw textarea buffer in local state. Only collapse empty
 * lines when we hand the value back to the parent, and only on blur so
 * in-progress edits aren't mangled. If `currentValue` changes from the
 * outside (scope switch, reset-to-default) we resync.
 */
function StringListTextarea({
  currentValue,
  disabled,
  onChange,
}: {
  currentValue: string;
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  const externalLines = currentValue
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
    .join("\n");
  const [buffer, setBuffer] = useState(externalLines);
  const [lastExternal, setLastExternal] = useState(externalLines);

  // Resync local buffer when the upstream value changes outside of our own edits
  // (e.g. user switches settings scope, clicks reset-to-default, or another
  // pane writes to the same setting).
  if (externalLines !== lastExternal) {
    setBuffer(externalLines);
    setLastExternal(externalLines);
  }

  function commit(nextBuffer: string) {
    const serialized = nextBuffer
      .split("\n")
      .map((s) => s.trim())
      .filter(Boolean)
      .join(", ");
    onChange(serialized);
  }

  return (
    <textarea
      className="setting-list-textarea"
      value={buffer}
      disabled={disabled}
      placeholder="One item per line"
      onChange={(event) => setBuffer(event.target.value)}
      onBlur={() => commit(buffer)}
      onKeyDown={(event) => {
        // Ctrl+Enter / Cmd+Enter also inserts a newline — some hosts swallow
        // the modifier-free Enter inside Tauri WebView2. We preventDefault
        // on both so nothing higher in the tree can consume the key, then
        // we mutate the buffer explicitly.
        if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
          event.preventDefault();
          event.stopPropagation();
          const ta = event.currentTarget;
          const start = ta.selectionStart ?? ta.value.length;
          const end = ta.selectionEnd ?? ta.value.length;
          const next = ta.value.slice(0, start) + "\n" + ta.value.slice(end);
          setBuffer(next);
          requestAnimationFrame(() => {
            ta.selectionStart = ta.selectionEnd = start + 1;
          });
        }
      }}
    />
  );
}
