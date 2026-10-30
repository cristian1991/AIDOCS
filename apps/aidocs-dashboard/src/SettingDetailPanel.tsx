/**
 * SettingDetailPanel — Phase 6g (2026-05-02).
 *
 * Right-rail detail surface for a selected setting on the Settings
 * page. Renders the full 4-layer cascade (factory / global / project
 * / session) with origin badges, the entry's effective value, hot-
 * reload pill, T0 / dashboard-only flag, and an actions footer.
 *
 * Presentational. The parent owns save/reset handlers; this panel
 * fires onEdit / onReset / onClose callbacks.
 */
import { useState } from "react";
import {
  AlertTriangle,
  CircleDot,
  Layers,
  Lock,
  RotateCcw,
  ShieldCheck,
  X,
} from "lucide-react";
import type { DashboardConfigEntry } from "./dashboardApi";
import { CastlePill } from "./CastleShell";
import { asText, canonicalSettingText, validateSettingDraft } from "./dashboardUtils";

export type SettingDetailPanelProps = {
  entry: DashboardConfigEntry;
  activeScope: "global" | "project" | "session";
  /** Resolve the pending draft for a given scope. Driven by the panel's
   * OWN activeScope (the in-panel G/P/S toggle) — a single `draft` keyed
   * to the page scope went stale the moment the operator switched scope
   * inside the panel, so the control stopped reflecting clicks. */
  draftForScope?: (scope: "global" | "project" | "session") => string | undefined;
  onClose: () => void;
  onEdit?: (path: string) => void;
  onReset?: (path: string, scope: string) => void;
  onSave?: (entry: DashboardConfigEntry, scope: string, value: string) => void;
  onDraftChange?: (path: string, scope: string, value: string) => void;
  saving?: boolean;
};

const LAYER_LABEL: Record<"factory" | "global" | "project" | "session", string> = {
  factory: "Factory",
  global: "Global",
  project: "Project",
  session: "Session",
};

const LAYER_TONE: Record<"factory" | "global" | "project" | "session",
  "muted" | "info" | "flow" | "ok"> = {
  factory: "muted",
  global: "info",
  project: "flow",
  session: "ok",
};

export function SettingDetailPanel({
  entry,
  activeScope: initialScope,
  draftForScope,
  onClose,
  onEdit,
  onReset,
  onSave,
  onDraftChange,
  saving,
}: SettingDetailPanelProps) {
  // Local activeScope state — operator can switch G/P/S inside the panel
  // without leaving the slide-out (Empire feature 2026-05-03).
  const [activeScope, setActiveScope] = useState<"global" | "project" | "session">(initialScope);
  // Draft for the panel's CURRENT scope — re-resolved on every render so
  // switching scope inside the panel reflects that scope's pending edit.
  const draft = draftForScope?.(activeScope);
  const layers = ["factory", "global", "project", "session"] as const;
  const isT0 = Boolean(entry.is_t0 || entry.dashboard_only);
  const effectiveLayer = entry.effective_layer ?? "factory";
  const ownAtActive =
    entry.scope_values?.[activeScope] !== undefined &&
    entry.scope_values?.[activeScope] !== null;
  // The stored value at this scope (or the factory default) as editor text.
  const baselineText = settingEditText(entry, entry.scope_values?.[activeScope] ?? entry.default);
  const editValue = draft ?? baselineText;
  const isDirty =
    draft !== undefined &&
    canonicalSettingText(entry, draft) !== canonicalSettingText(entry, baselineText);
  // Typed validation from the catalog (type / allowed_values / value_format /
  // minimum / maximum). A malformed draft is shown and cannot be saved; a
  // well-formed one still goes through the server's config_set guards.
  const draftError = draft !== undefined ? validateSettingDraft(entry, draft) : null;
  const canSave = !saving && isDirty && !draftError;

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="flex items-start justify-between gap-2 border-b border-castle-line pb-3">
        <div className="min-w-0">
          <div className="text-[10px] font-black uppercase tracking-widest text-castle-mute">
            Setting
          </div>
          <h3 className="mt-1 break-all font-mono text-base font-bold text-slate-100">
            {entry.path}
          </h3>
          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            <CastlePill tone={LAYER_TONE[effectiveLayer]}>
              from {LAYER_LABEL[effectiveLayer].toLowerCase()}
            </CastlePill>
            {entry.requires_restart ? (
              <CastlePill tone="warn">⟳ restart</CastlePill>
            ) : (
              <CastlePill tone="ok">● live</CastlePill>
            )}
            {isT0 && (
              <CastlePill tone="danger">
                <Lock className="mr-0.5 inline h-2.5 w-2.5" /> T0
              </CastlePill>
            )}
            {entry.security_sensitive && !isT0 && (
              <CastlePill tone="warn">
                <ShieldCheck className="mr-0.5 inline h-2.5 w-2.5" /> sensitive
              </CastlePill>
            )}
          </div>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="rounded-md border border-castle-line p-1 text-castle-mute hover:text-slate-200"
          title="Close"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </div>

      {/* Body */}
      <div className="flex-1 overflow-y-auto py-3">
        {/* Description */}
        <div className="mb-4">
          <div className="mb-1 flex items-center gap-1.5 text-[10px] font-black uppercase tracking-widest text-castle-mute">
            Description
          </div>
          <p className="text-xs leading-relaxed text-slate-300">
            {entry.description}
          </p>
        </div>

        {/* Allowed values */}
        {entry.allowed_values && entry.allowed_values.length > 0 && (
          <div className="mb-4">
            <div className="mb-1 flex items-center gap-1.5 text-[10px] font-black uppercase tracking-widest text-castle-mute">
              Allowed values
            </div>
            <div className="flex flex-wrap gap-1">
              {entry.allowed_values.map((v) => (
                <code
                  key={v}
                  className="rounded-md border border-castle-line bg-black/30 px-2 py-0.5 text-[11px] text-slate-200"
                >
                  {v}
                </code>
              ))}
            </div>
          </div>
        )}

        {/* Layer cascade */}
        <div className="mb-4">
          <div className="mb-1 flex items-center gap-1.5 text-[10px] font-black uppercase tracking-widest text-castle-mute">
            <Layers className="h-3 w-3" />
            Layer cascade
          </div>
          <div className="overflow-hidden rounded-xl border border-castle-line">
            {layers.map((layer) => {
              const value =
                layer === "factory"
                  ? entry.default
                  : entry.scope_values?.[layer];
              const has = value !== undefined && value !== null;
              const isEffective = effectiveLayer === layer;
              const allowed = entry.allowed_scopes.includes(layer) || layer === "factory";
              return (
                <div
                  key={layer}
                  className={
                    "grid grid-cols-[80px_1fr_auto] items-center gap-3 border-b border-castle-line/60 px-3 py-2 text-xs last:border-b-0 " +
                    (isEffective ? "bg-castle-allow/5" : "") +
                    (!allowed ? " opacity-50" : "")
                  }
                >
                  <CastlePill tone={LAYER_TONE[layer]}>
                    {LAYER_LABEL[layer]}
                  </CastlePill>
                  <div className="min-w-0 truncate font-mono text-slate-200">
                    {has ? (
                      asText(value as unknown)
                    ) : (
                      <span className="italic text-castle-mute">— unset —</span>
                    )}
                  </div>
                  {isEffective && (
                    <CircleDot className="h-3 w-3 text-castle-allow" />
                  )}
                  {!allowed && (
                    <span className="text-[10px] italic text-castle-mute">
                      n/a
                    </span>
                  )}
                </div>
              );
            })}
          </div>
        </div>

        {/* Per-leaf origin (dict-typed only) */}
        {entry.origin && Object.keys(entry.origin).length > 1 && (
          <details className="mb-4">
            <summary className="cursor-pointer text-[10px] font-black uppercase tracking-widest text-castle-mute hover:text-slate-300">
              Per-leaf origin ({Object.keys(entry.origin).length} leaves)
            </summary>
            <div className="mt-2 max-h-64 overflow-y-auto rounded-lg border border-castle-line">
              <table className="w-full font-mono text-[10px]">
                <tbody>
                  {Object.entries(entry.origin)
                    .sort(([a], [b]) => a.localeCompare(b))
                    .map(([leafPath, leafLayer]) => (
                      <tr
                        key={leafPath}
                        className="border-b border-castle-line/40 last:border-b-0"
                      >
                        <td className="px-2 py-1 text-castle-mute">
                          {leafPath}
                        </td>
                        <td className="px-2 py-1 text-right">
                          <CastlePill
                            tone={LAYER_TONE[leafLayer as keyof typeof LAYER_TONE]}
                          >
                            {leafLayer}
                          </CastlePill>
                        </td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>
          </details>
        )}

        {/* Override warnings */}
        {isT0 && (
          <div className="mb-4 flex items-start gap-2 rounded-lg border border-castle-deny/30 bg-castle-deny/5 p-2.5 text-[11px] text-castle-deny">
            <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
            <div>
              <strong>T0 dashboard-only.</strong> Agents categorically
              cannot write this regardless of NLP grant phrasing. The
              dashboard's direct-sqlite write is the only path. Saving
              from this panel requires type-the-key confirmation.
            </div>
          </div>
        )}
      </div>

      {/* Editor — actually wires to saveConfigEntry / setDraftValue.
       * 2026-05-03 fix: prior version had Reset + Edit buttons that
       * fired callbacks the parent never passed, so saving was a
       * dead end. Empire reported being unable to toggle dev.kill_switch.
       * Minimum viable editor: type-aware input, Save, Reset. */}
      {entry.editable && (
        <div className="flex flex-col gap-2 border-t border-castle-line pt-3">
          <div className="flex items-center justify-between gap-2">
            <div className="text-[10px] font-black uppercase tracking-widest text-castle-mute">
              Edit at {activeScope}
            </div>
            {isDirty && (
              <CastlePill tone="warn">unsaved</CastlePill>
            )}
          </div>
          {/* G/P/S scope toggle ABOVE the input (Empire directive 2026-05-03). */}
          <div className="flex items-center gap-1">
            {(["global", "project", "session"] as const).map((sc) => {
              const allowed = entry.allowed_scopes.includes(sc);
              const isActive = sc === activeScope;
              const label = sc === "global" ? "G" : sc === "project" ? "P" : "S";
              const fullLabel = sc === "global" ? "Global" : sc === "project" ? "Project" : "Session";
              return (
                <button
                  key={sc}
                  type="button"
                  onClick={() => allowed && setActiveScope(sc)}
                  disabled={!allowed}
                  title={`Edit at ${sc}${allowed ? "" : " — not allowed for this setting"}`}
                  className={
                    "flex items-center gap-1 rounded-md border px-2 py-1 font-mono text-[10px] font-bold transition " +
                    (isActive
                      ? "border-castle-allow/60 bg-castle-allow/15 text-castle-allow"
                      : allowed
                      ? "border-castle-line bg-black/30 text-castle-mute hover:border-castle-allow/40 hover:text-slate-200"
                      : "border-castle-line/30 bg-black/10 text-castle-mute/40")
                  }
                >
                  <span>{label}</span>
                  <span className="text-castle-mute/70">{fullLabel}</span>
                </button>
              );
            })}
          </div>
          <SettingValueEditor
            entry={entry}
            value={editValue}
            disabled={!onDraftChange}
            onChange={(next) => onDraftChange?.(entry.path, activeScope, next)}
          />
          {draftError && (
            <div role="alert" className="text-[11px] text-castle-deny">
              {draftError}
            </div>
          )}
          <div className="flex items-center gap-2">
            {ownAtActive && onReset && (
              <button
                type="button"
                onClick={() => onReset(entry.path, activeScope)}
                className="flex items-center gap-1 rounded-lg border border-castle-line px-3 py-1.5 text-xs font-bold text-castle-mute hover:bg-white/[0.04] hover:text-slate-200"
                title={`Clear override at ${activeScope} layer`}
                disabled={saving}
              >
                <RotateCcw className="h-3 w-3" />
                Reset {activeScope}
              </button>
            )}
            <div className="ml-auto">
              {onSave && (
                <button
                  type="button"
                  onClick={() => {
                    if (canSave) onSave(entry, activeScope, editValue);
                  }}
                  disabled={!canSave}
                  className="rounded-lg border border-castle-allow/35 bg-castle-allow/10 px-3 py-1.5 text-xs font-bold text-castle-allow hover:bg-castle-allow/20 disabled:opacity-40"
                >
                  {saving ? "Saving..." : `Save at ${activeScope}`}
                </button>
              )}
            </div>
          </div>
        </div>
      )}
      {!entry.editable && (
        <div className="border-t border-castle-line pt-3 text-[11px] italic text-castle-mute">
          This setting is not editable from the dashboard at the {activeScope} scope.
        </div>
      )}
    </div>
  );
}

/** A stored value as the text its typed control edits. Lists edit one item per
 * line; everything else uses the shared asText rendering. */
function settingEditText(entry: DashboardConfigEntry, value: unknown): string {
  if (entry.type === "string_list" && Array.isArray(value)) return value.join("\n");
  return asText(value);
}

const CONTROL_CLASS =
  "rounded-lg border border-castle-line bg-black/30 px-3 py-2 font-mono text-xs text-slate-200";

/**
 * The typed control for one setting, chosen from the catalog the snapshot
 * carries (config_schema via runtime_presentation_service) — never a
 * free-text box for a value that has a type:
 *   boolean                      -> checkbox
 *   allowed_values               -> select (an off-list stored value is shown,
 *                                   labelled, instead of silently becoming the
 *                                   first option)
 *   integer                      -> number input, step 1, catalog min/max
 *   string_list                  -> textarea, one item per line
 *   string + json_object/array   -> JSON textarea (validated by the parent)
 *   string + number              -> decimal number input
 *   string                       -> text input
 */
function SettingValueEditor({
  entry,
  value,
  disabled,
  onChange,
}: {
  entry: DashboardConfigEntry;
  value: string;
  disabled: boolean;
  onChange: (next: string) => void;
}) {
  if (entry.type === "boolean") {
    const on = value.trim() === "true";
    return (
      <label className="flex items-center gap-2 rounded-lg border border-castle-line bg-black/30 px-3 py-2 text-xs text-slate-200">
        <input
          type="checkbox"
          checked={on}
          onChange={(e) => onChange(e.target.checked ? "true" : "false")}
          disabled={disabled}
        />
        <span>{on ? "Enabled" : "Disabled"}</span>
      </label>
    );
  }
  if (entry.allowed_values && entry.allowed_values.length > 0) {
    const offList = !entry.allowed_values.includes(value);
    return (
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        className={CONTROL_CLASS}
      >
        {offList && (
          <option value={value}>
            {value || "(empty)"} (not an allowed value)
          </option>
        )}
        {entry.allowed_values.map((v) => (
          <option key={v} value={v} title={entry.value_descriptions[v] ?? undefined}>
            {v}
          </option>
        ))}
      </select>
    );
  }
  if (entry.type === "integer") {
    const bounds = [
      entry.minimum != null ? `min ${entry.minimum}` : null,
      entry.maximum != null ? `max ${entry.maximum}` : null,
    ].filter(Boolean);
    return (
      <div className="flex flex-col gap-1">
        <input
          type="number"
          inputMode="numeric"
          step={1}
          min={entry.minimum ?? undefined}
          max={entry.maximum ?? undefined}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          disabled={disabled}
          className={CONTROL_CLASS}
        />
        {bounds.length > 0 && (
          <span className="text-[10px] text-castle-mute">{bounds.join(" · ")}</span>
        )}
      </div>
    );
  }
  if (entry.type === "string_list") {
    return (
      <textarea
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        rows={4}
        placeholder="One value per line"
        className={CONTROL_CLASS}
      />
    );
  }
  if (entry.value_format === "json_object" || entry.value_format === "json_array") {
    return (
      <textarea
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        rows={6}
        spellCheck={false}
        placeholder={entry.value_format === "json_array" ? "JSON array, e.g. []" : "JSON object, e.g. {}"}
        className={CONTROL_CLASS}
      />
    );
  }
  if (entry.value_format === "number") {
    return (
      <input
        type="number"
        inputMode="decimal"
        step="any"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        className={CONTROL_CLASS}
      />
    );
  }
  return (
    <input
      type="text"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      disabled={disabled}
      className={CONTROL_CLASS}
    />
  );
}
