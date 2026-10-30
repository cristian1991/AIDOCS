/**
 * SettingsPage — Phase 6g (2026-05-02), Royal Ledger rebuild.
 *
 * Master/detail layout in the new castle design language:
 *
 *   ┌────────────┬─────────────────────────────────────┐
 *   │ Categories │ Entries (search + filter + table)   │
 *   └────────────┴─────────────────────────────────────┘
 *
 * The selected entry's full layer-cascade detail renders in the
 * shell's right context rail (App.tsx wires SettingDetailPanel
 * into contextRail when activeNav === "settings" and a path is
 * selected). The page itself stays dense and scannable.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { configEditingAvailable } from "./entitlements";
import {
  Filter,
  Info,
  Lock,
  Search,
  Settings as SettingsIcon,
  ShieldAlert,
  ShieldCheck,
} from "lucide-react";
import type {
  DashboardConfigEntry,
  OperatorSurfaceResult,
} from "./dashboardApi";
import {
  operatorSurfaceExpertSet,
  operatorSurfaceRows,
} from "./dashboardApi";
import { asText, canonicalSettingText, validateSettingDraft } from "./dashboardUtils";
import {
  refusalBanner,
  broadeningWarning,
  type AuthorityResult,
} from "./authorityPresentation";
import type { SettingsPageProps } from "./dashboardTypes";
import { CastlePill } from "./CastleShell";
import { INHERIT_VALUE } from "./dashboardSettingsComponents";
import { OperatorProfileCards } from "./OperatorProfileCards";
import {
  advancedKeySet,
  isNormalRowAllowed,
  partitionDirtyForSave,
  saveRouteFor,
} from "./settingsRouting";
import {
  categoryCounts,
  isSettableAt,
  matchesTerm,
  sameProjectRoot,
  sectionName,
} from "./settingsModel";

type FilterId = "modified" | "security" | "danger";
type Layer = "factory" | "global" | "project" | "session";

const LAYER_TONE: Record<Layer, "muted" | "info" | "flow" | "ok"> = {
  factory: "muted",
  global: "info",
  project: "flow",
  session: "ok",
};

const LAYER_LABEL: Record<Layer, string> = {
  factory: "factory",
  global: "global",
  project: "project",
  session: "session",
};

function isDangerEntry(entry: DashboardConfigEntry): boolean {
  return Boolean(entry.is_t0 || entry.dashboard_only);
}

function matchesSearch(entry: DashboardConfigEntry, term: string): boolean {
  return matchesTerm([entry.path, entry.key, entry.description, sectionName(entry)], term);
}

export function SettingsPage({
  settingsScope,
  setSettingsScope,
  hasProject,
  hasSession,
  configSections,
  projectRoot,
  loadedProjectRoot,
  bashPolicy,
  saveBashCommandState,
  draftValues,
  savingSetting,
  requestConfigSave,
  requestConfigBatchSave,
  setDraftValue,
  openImportExport,
  selectedPath,
  onEntrySelect,
}: SettingsPageProps) {
  const [searchTerm, setSearchTerm] = useState("");
  const [activeFilters, setActiveFilters] = useState<Set<FilterId>>(new Set());
  const [activeCategory, setActiveCategory] = useState<string>("__all__");

  // Operator Surface is the source of truth for which keys are NORMAL vs
  // Advanced Raw. Normal Settings rows must never show service-managed,
  // deprecated, dashboard-only, or hidden-owned keys — those live only in
  // the Advanced Raw diagnostics section and are written through the
  // expert/profile paths, never a normal save.
  const [advancedRows, setAdvancedRows] = useState<OperatorSurfaceResult[]>([]);
  const [advancedHidden, setAdvancedHidden] = useState<Set<string>>(new Set());
  // Fail-closed gate: until the operator surface rows load successfully we
  // hide security-sensitive keys and disable normal saves, so a guardrail
  // key can never slip through on a failed/slow fetch.
  const [rowsLoaded, setRowsLoaded] = useState(false);
  const [expertKey, setExpertKey] = useState<string>("");
  const [expertValue, setExpertValue] = useState<string>("");
  const [expertConfirm, setExpertConfirm] = useState<string>("");
  const [expertBusy, setExpertBusy] = useState(false);
  const [expertResult, setExpertResult] = useState<OperatorSurfaceResult | null>(
    null,
  );

  // ── TYPED EXPERT EDITOR (2026-08-02) ────────────────────────────────
  // The protected-setting panel was a single `value (JSON or raw)` text box, so
  // flipping a BOOLEAN guardrail meant hand-typing `true` — for a key whose
  // catalog entry has declared type="boolean" all along. Operator, verbatim:
  // "i said it shouldnt be strings, change it to the same setting style as the
  // 'normal' settings". The catalog is authoritative and the normal rows already
  // render from it (SettingDetailPanel: boolean -> checkbox, allowed_values ->
  // select, string_list -> textarea, integer -> number); this panel now uses the
  // same controls instead of asking the operator to be a JSON encoder.
  //
  // SOURCE ORDER MATTERS: dashboard_only / T0 keys are deliberately EXCLUDED
  // from configSections (they are Advanced-Raw only), so the advanced row is
  // consulted FIRST — it carries type/allowed_values/default from
  // operator_surface.inspect_key. Falling back to configSections alone would
  // have left exactly the protected settings untyped, i.e. fixed everything
  // except the case that prompted the fix.
  const expertMeta = useMemo(() => {
    if (!expertKey) return null;
    const adv = advancedRows.find(
      (r) => String((r as { key?: string }).key ?? "") === expertKey,
    ) as Record<string, unknown> | undefined;
    if (adv && adv.type) {
      return {
        type: String(adv.type),
        allowedValues: (adv.allowed_values as string[] | null) ?? null,
        valueFormat: String(adv.value_format ?? ""),
        minimum: (adv.minimum as number | null | undefined) ?? null,
        maximum: (adv.maximum as number | null | undefined) ?? null,
        current: adv.effective_value,
      };
    }
    const entry = configSections
      .flatMap((s) => s.entries)
      .find((x) => x.path === expertKey);
    return entry
      ? {
          type: String(entry.type),
          allowedValues: entry.allowed_values,
          valueFormat: String(entry.value_format ?? ""),
          minimum: entry.minimum ?? null,
          maximum: entry.maximum ?? null,
          current: entry.current_value,
        }
      : null;
  }, [advancedRows, configSections, expertKey]);

  // Prefill from the CURRENT value when the panel opens for a new key, so Apply
  // never submits an empty box and the operator edits from truth. Keyed on a ref
  // rather than the effect deps so a re-render (or the rows arriving late) can
  // never wipe an edit already in progress.
  const expertPrefilledFor = useRef<string>("");
  useEffect(() => {
    if (!expertKey) {
      expertPrefilledFor.current = "";
      return;
    }
    if (expertPrefilledFor.current === expertKey) return;
    expertPrefilledFor.current = expertKey;
    const cur = expertMeta?.current;
    setExpertValue(cur === undefined || cur === null ? "" : JSON.stringify(cur));
    setExpertConfirm("");
    setExpertResult(null);
  }, [expertKey, expertMeta]);

  // The exact phrase the backend verifies (operator_surface.expert_confirm_token).
  // "Armed" is DERIVED from it, never a separate flag — so typing the phrase by
  // hand and clicking the button are the same state, and there is no way for the
  // UI to believe it is confirmed while the backend disagrees.
  const expertConfirmPhrase = `confirm-set ${expertKey}`;
  const expertArmed = expertConfirm.trim() === expertConfirmPhrase;

  // expertValue stays the JSON-encoded wire value (applyExpert JSON.parses it),
  // so these two adapt between that and what a human should see in the control.
  function expertShown(raw: string): string {
    try {
      const v = JSON.parse(raw);
      return typeof v === "string" ? v : raw;
    } catch {
      return raw;
    }
  }
  // Same catalog-typed rule as the normal rows (validateSettingDraft). The
  // backend's expert_set confirmation + guards still decide the write; this
  // only refuses to send a value of the wrong shape.
  const expertError = useMemo(() => {
    if (!expertMeta || expertValue === "") return null;
    if (expertMeta.type === "string_list") {
      try {
        const v = JSON.parse(expertValue);
        return Array.isArray(v) && v.every((x) => typeof x === "string")
          ? null
          : 'Must be a JSON list of strings, e.g. ["*"].';
      } catch {
        return 'Must be a JSON list of strings, e.g. ["*"].';
      }
    }
    return validateSettingDraft(
      {
        type: expertMeta.type,
        allowed_values: expertMeta.allowedValues,
        value_format: expertMeta.valueFormat,
        minimum: expertMeta.minimum,
        maximum: expertMeta.maximum,
      },
      expertShown(expertValue),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [expertMeta, expertValue]);

  function setExpertTyped(next: string) {
    // A string-typed setting must round-trip as a JSON string; anything else
    // (integer, list, raw JSON) is already in wire form.
    setExpertValue(
      expertMeta?.type === "string" ? JSON.stringify(next) : next,
    );
  }

  // Protected rows and profile cards load for the project the on-screen values
  // belong to (the loaded snapshot), never for a header pick still loading, so
  // what is shown and where it would be written always agree.
  const surfaceRoot = loadedProjectRoot ?? projectRoot;

  useEffect(() => {
    let live = true;
    setRowsLoaded(false);
    setExpertKey("");
    operatorSurfaceRows(surfaceRoot)
      .then((res) => {
        if (!live) return;
        const adv = (res.advanced_raw ?? []) as OperatorSurfaceResult[];
        setAdvancedRows(adv);
        setAdvancedHidden(
          advancedKeySet(
            adv.map((r) => ({ key: String((r as { key?: string }).key ?? "") })),
          ),
        );
        setRowsLoaded(true);
      })
      .catch(() => {
        // Operator surface unavailable → stay fail-closed: rowsLoaded
        // remains false, so isNormalRowAllowed additionally hides every
        // security-sensitive key and the known guardrail set, and saves
        // are disabled until rows load.
        if (live) setRowsLoaded(false);
      });
    return () => {
      live = false;
    };
  }, [surfaceRoot]);

  // While a header project switch is loading, the values on screen are still
  // the previous project's: say so, and allow no write of any kind (normal
  // save, protected setting, profile card) until the new project has loaded.
  const switchingProject =
    !!projectRoot && !!loadedProjectRoot && !sameProjectRoot(projectRoot, loadedProjectRoot);
  // Writes need positive proof: a loaded snapshot that IS the picked project.
  // Before the first load (or after a failed one) there is none, so nothing
  // may be written either.
  const readyForWrites =
    !!loadedProjectRoot && (!projectRoot || sameProjectRoot(projectRoot, loadedProjectRoot));

  const draftKey = (path: string) => `${settingsScope}:${path}`;

  const allEntries = useMemo(
    () => configSections.flatMap(({ entries }) => entries),
    [configSections],
  );


  function isOwn(entry: DashboardConfigEntry): boolean {
    if (draftValues[draftKey(entry.path)] === INHERIT_VALUE) return false;
    if (draftValues[draftKey(entry.path)] !== undefined) return true;
    const raw = entry.scope_values?.[settingsScope];
    if (raw === undefined || raw === null) return false;
    return asText(raw) !== asText(entry.default);
  }

  function effectiveText(entry: DashboardConfigEntry): string {
    const draft = draftValues[draftKey(entry.path)];
    if (draft !== undefined && draft !== INHERIT_VALUE) return draft;
    if (entry.current_value !== undefined && entry.current_value !== null)
      return asText(entry.current_value);
    return asText(entry.default);
  }

  // A key that must NOT appear as a normal editable row. The authoritative
  // advanced set comes from the Operator Surface; isNormalRowAllowed also
  // applies the static known-guardrail list + a fail-closed rule (hide
  // security-sensitive until rows load) so nothing leaks on a failed fetch.
  function isGuardrail(e: DashboardConfigEntry): boolean {
    return !isNormalRowAllowed(
      {
        path: e.path,
        dashboard_only: e.dashboard_only,
        is_t0: e.is_t0,
        security_sensitive: e.security_sensitive,
      },
      { rowsLoaded, advancedKeys: advancedHidden },
    );
  }

  // What the table can show before the namespace pick; the namespace counts
  // come from this, so a count never promises rows that are hidden.
  const shownEntries = useMemo(() => {
    return allEntries.filter((e) => {
      if (isGuardrail(e)) return false; // never a normal row
      if (!matchesSearch(e, searchTerm)) return false;
      if (activeFilters.has("modified") && !isOwn(e)) return false;
      if (activeFilters.has("security") && !e.security_sensitive) return false;
      if (activeFilters.has("danger") && !isDangerEntry(e)) return false;
      return true;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [allEntries, searchTerm, activeFilters, draftValues, settingsScope, advancedHidden, rowsLoaded]);

  const categories = useMemo(() => categoryCounts(shownEntries), [shownEntries]);

  const visibleEntries = useMemo(
    () =>
      activeCategory === "__all__"
        ? shownEntries
        : shownEntries.filter((e) => sectionName(e) === activeCategory),
    [shownEntries, activeCategory],
  );

  // EVERY entry with a meaningful draft, BEFORE the guardrail split. This
  // includes the dev-mode quick toggle. saveAll must never iterate this
  // raw list directly — see the partition below.
  const rawDirtyEntries = useMemo(() => {
    const isDirty = (e: DashboardConfigEntry) => {
      const draft = draftValues[draftKey(e.path)];
      if (draft === undefined) return false;
      if (draft === INHERIT_VALUE) {
        const raw = e.scope_values?.[settingsScope];
        return raw !== undefined && raw !== null;
      }
      const raw = e.scope_values?.[settingsScope];
      const baseline =
        raw !== undefined && raw !== null ? asText(raw) : asText(e.default);
      // Canonical compare: a list edited one-per-line equals "a, b".
      return canonicalSettingText(e, draft) !== canonicalSettingText(e, baseline);
    };
    const set = new Map<string, DashboardConfigEntry>();
    for (const e of allEntries) if (isDirty(e)) set.set(e.path, e);
    return Array.from(set.values());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [allEntries, draftValues, settingsScope]);

  // Split into what a NORMAL Save All may persist vs. what must be
  // quarantined (guardrail keys whose draft must go through the expert /
  // profile path, never the normal save). Save All operates only on
  // `dirtyEntries` (savable).
  const { savable: dirtyEntries, quarantined: quarantinedDirty } = useMemo(
    () =>
      partitionDirtyForSave(rawDirtyEntries, {
        rowsLoaded,
        advancedKeys: advancedHidden,
      }),
    [rawDirtyEntries, rowsLoaded, advancedHidden],
  );

  const hasDirty = dirtyEntries.length > 0;
  const dangerDirtyCount = dirtyEntries.filter(isDangerEntry).length;

  function saveAll() {
    // Defense-in-depth at the HANDLER (not just disabled-button state):
    //   1) refuse entirely until the operator surface has loaded, and
    //   2) re-assert isNormalRowAllowed on every entry here, so even a
    //      mis-derived dirtyEntries can never push a guardrail key into
    //      requestConfigSave. The backend positive-proof gate is the real
    //      enforcement; this keeps the UI from ever attempting it.
    if (!rowsLoaded || !readyForWrites) return;
    const allDirty = dirtyEntries.filter((entry) =>
      isNormalRowAllowed(
        {
          path: entry.path,
          dashboard_only: entry.dashboard_only,
          is_t0: entry.is_t0,
          security_sensitive: entry.security_sensitive,
        },
        { rowsLoaded, advancedKeys: advancedHidden },
      ) && isSettableAt(entry, settingsScope),
    );
    if (allDirty.length === 0) return;
    const items = allDirty.map((entry) => ({
      entry,
      value: draftValues[draftKey(entry.path)] ?? asText(entry.current_value),
    }));
    if (items.length === 1) {
      requestConfigSave(items[0].entry, settingsScope, items[0].value);
    } else if (items.length > 1) {
      requestConfigBatchSave(items, settingsScope);
    }
  }

  async function applyExpert() {
    if (!expertKey || !readyForWrites) return;
    setExpertBusy(true);
    setExpertResult(null);
    try {
      let value: unknown = expertValue;
      try {
        value = JSON.parse(expertValue);
      } catch {
        /* not JSON → send the raw string */
      }
      const res = await operatorSurfaceExpertSet({
        key: expertKey,
        valueJson: JSON.stringify(value),
        projectRoot: surfaceRoot,
        confirm: expertConfirm || undefined,
        scope: settingsScope,
      });
      setExpertResult(res);
    } catch (e) {
      setExpertResult({ ok: false, message: String(e) });
    } finally {
      setExpertBusy(false);
    }
  }

  function rowField(row: OperatorSurfaceResult, name: string): string {
    const v = (row as Record<string, unknown>)[name];
    return v == null ? "" : String(v);
  }

  // #99 (operator 2026-07-15): only operator-actionable rows (route 'expert')
  // are listed; profile-owned (route 'profile') and deprecated (route
  // 'blocked') rows collapse into one summary line so nothing vanishes
  // silently. The fail-closed normal-settings hider (advancedHidden) still
  // uses the FULL advanced_raw set, so this is a display-only trim.
  const protectedRouted = advancedRows.map((row) => {
    const key = rowField(row, "key");
    return {
      row,
      key,
      owner: rowField(row, "owning_profile"),
      route: saveRouteFor({
        key,
        service_managed: rowField(row, "service_managed") || null,
        deprecated: rowField(row, "deprecated") || null,
        dashboard_only: Boolean((row as Record<string, unknown>).dashboard_only),
        security_sensitive: Boolean((row as Record<string, unknown>).security_sensitive),
      }),
    };
  });
  const protectedActionable = protectedRouted.filter(
    (r) =>
      r.route === "expert" &&
      matchesTerm([r.key, r.owner, rowField(r.row, "description")], searchTerm),
  );
  const managedCount = protectedRouted.filter((r) => r.route === "profile").length;
  const deprecatedCount = protectedRouted.filter((r) => r.route === "blocked").length;
  // The editor opens directly under the row being edited; a key with no row
  // here (a quarantined profile key) gets it at the top of the panel.
  const expertIndex = protectedActionable.findIndex((r) => r.key === expertKey);
  const protectedBefore = expertKey
    ? protectedActionable.slice(0, expertIndex + 1)
    : protectedActionable;
  const protectedAfter = expertKey ? protectedActionable.slice(expertIndex + 1) : [];
  const showProtected =
    advancedRows.length > 0 &&
    (protectedActionable.length > 0 || !searchTerm.trim() || !!expertKey);

  const expertEditorRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (expertKey) expertEditorRef.current?.scrollIntoView?.({ block: "nearest" });
  }, [expertKey]);

  function renderProtectedRow({ key, owner }: { key: string; owner: string }) {
    return (
      <div
        key={key}
        className={
          "flex items-center gap-3 border-b border-castle-deny/10 px-4 py-2 last:border-b-0" +
          (key === expertKey ? " bg-castle-deny/10" : "")
        }
      >
        <code className="min-w-0 flex-1 truncate font-mono text-xs text-slate-200">{key}</code>
        <CastlePill tone="muted">{owner}</CastlePill>
        <button
          type="button"
          onClick={() => setExpertKey(key === expertKey ? "" : key)}
          className="rounded-lg border border-castle-deny/40 bg-castle-deny/10 px-2.5 py-1 text-[11px] font-bold text-castle-deny hover:bg-castle-deny/20"
        >
          {key === expertKey ? "Close" : "Edit"}
        </button>
      </div>
    );
  }

  function toggleFilter(id: FilterId) {
    setActiveFilters((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  // Hide unused setDraftValue warning — wired to inputs via SettingDetailPanel.
  void setDraftValue;

  return (
    <div className="flex min-h-0 flex-1 overflow-hidden">
      {/* ── Category rail (left) ─────────────────────────────── */}
      <aside className="flex w-[240px] shrink-0 flex-col border-r border-castle-line bg-black/15">
        <div className="border-b border-castle-line p-3">
          <div className="relative">
            <Search className="absolute left-2.5 top-2.5 h-3.5 w-3.5 text-castle-mute" />
            <input
              type="search"
              placeholder="Search settings"
              value={searchTerm}
              onChange={(e) => {
                setSearchTerm(e.target.value);
                setActiveCategory("__all__");
              }}
              className="w-full rounded-lg border border-castle-line bg-black/30 py-1.5 pl-8 pr-2 text-sm text-slate-100 placeholder:text-castle-mute focus:border-castle-allow/50 focus:outline-none"
            />
          </div>
        </div>
        <nav className="flex-1 overflow-y-auto p-2">
          <button
            type="button"
            onClick={() => setActiveCategory("__all__")}
            className={
              "flex w-full items-center justify-between rounded-lg px-3 py-2 text-sm transition " +
              (activeCategory === "__all__"
                ? "bg-castle-allow/10 text-white"
                : "text-castle-mute hover:bg-white/[0.03] hover:text-slate-200")
            }
          >
            <span className="flex items-center gap-2">
              <SettingsIcon className="h-3.5 w-3.5" />
              All settings
            </span>
            <span className="text-[10px] text-castle-mute">
              {shownEntries.length}
            </span>
          </button>
          <div className="mb-1 mt-2 px-2 text-[10px] font-black uppercase tracking-widest text-castle-mute">
            Namespaces
          </div>
          {categories.map((cat) => {
            const isActive = activeCategory === cat.name;
            return (
              <button
                key={cat.name}
                type="button"
                onClick={() => setActiveCategory(cat.name)}
                className={
                  "mb-0.5 flex w-full items-center justify-between rounded-lg px-3 py-1.5 text-sm transition " +
                  (isActive
                    ? cat.isDanger
                      ? "bg-castle-deny/10 text-castle-deny"
                      : "bg-castle-allow/10 text-white"
                    : "text-castle-mute hover:bg-white/[0.03] hover:text-slate-200")
                }
              >
                <span className="flex items-center gap-2">
                  {cat.isDanger && <Lock className="h-3 w-3" />}
                  {cat.name}
                </span>
                <span className="font-mono text-[10px]">{cat.count}</span>
              </button>
            );
          })}
        </nav>
      </aside>

      {/* ── Entries (right) ──────────────────────────────────── */}
      <main className="flex min-h-0 flex-1 flex-col">
        {/* Top strip — scope tabs + filters + save */}
        <div className="flex flex-nowrap items-center gap-3 border-b border-castle-line bg-castle-panel/40 px-pad-3 py-pad-2">
          <div className="flex rounded-xl border border-castle-line bg-black/30 p-1">
            {(["global", "project", "session"] as const).map((scope) => {
              const enabled =
                scope === "global"
                  ? true
                  : scope === "project"
                  ? hasProject
                  : hasSession;
              const isActive = settingsScope === scope;
              return (
                <button
                  key={scope}
                  type="button"
                  disabled={!enabled}
                  onClick={() => setSettingsScope(scope)}
                  className={
                    "rounded-lg px-3 py-1 text-xs font-bold uppercase tracking-widest transition " +
                    (isActive
                      ? "bg-castle-allow text-castle-bg"
                      : enabled
                      ? "text-castle-mute hover:text-slate-200"
                      : "cursor-not-allowed text-castle-mute/40")
                  }
                >
                  {scope}
                </button>
              );
            })}
          </div>
          <div className="flex min-w-0 flex-nowrap items-center gap-2">
            <Filter className="h-3.5 w-3.5 shrink-0 text-castle-mute" />
            {(
              [
                ["modified", "Modified"],
                ["security", "Security"],
                ["danger", "Danger zone"],
              ] as Array<[FilterId, string]>
            ).map(([id, label]) => {
              const isActive = activeFilters.has(id);
              const isDangerChip = id === "danger";
              return (
                <button
                  key={id}
                  type="button"
                  onClick={() => toggleFilter(id)}
                  className={
                    "rounded-full border px-3 py-0.5 text-[10px] font-bold uppercase tracking-widest transition " +
                    (isActive
                      ? isDangerChip
                        ? "border-castle-deny bg-castle-deny text-white"
                        : "border-castle-allow bg-castle-allow text-castle-bg"
                      : "border-castle-line text-castle-mute hover:border-castle-allow/40 hover:text-slate-200")
                  }
                >
                  {label}
                </button>
              );
            })}
          </div>
          <div className="ml-auto flex shrink-0 items-center gap-3">
            <button
              type="button"
              onClick={openImportExport}
              className="rounded-xl border border-castle-line bg-white/[0.035] px-3 py-2 text-sm font-bold text-slate-300 hover:bg-white/[0.07]"
            >
              Import / Export
            </button>
            <button
              type="button"
              onClick={saveAll}
                disabled={!hasDirty || !!savingSetting || !rowsLoaded || !readyForWrites || !configEditingAvailable()}
                title={
                  !configEditingAvailable()
                    ? "Config editing runs in the local / desktop app - read-only over WebAgent."
                    : !readyForWrites
                    ? "Saving is disabled until the selected project has loaded."
                    : !rowsLoaded
                    ? "Saving is disabled until the operator surface loads (fail-closed)."
                    : undefined
                }
              className={
                "rounded-xl border px-3 py-2 text-sm font-bold transition disabled:cursor-not-allowed disabled:opacity-40 " +
                (dangerDirtyCount > 0
                  ? "border-castle-deny/40 bg-castle-deny/15 text-castle-deny hover:bg-castle-deny/25"
                  : "border-castle-allow/40 bg-castle-allow/15 text-castle-allow hover:bg-castle-allow/25")
              }
            >
              {savingSetting
                ? "Saving..."
                : dangerDirtyCount > 0
                ? `Save (${dangerDirtyCount} dangerous)`
                : `Save${hasDirty ? ` (${dirtyEntries.length})` : ""}`}
            </button>
          </div>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto p-4">
          {switchingProject && (
            <div className="mb-4 rounded-2xl border border-castle-warn/40 bg-castle-warn/[0.06] px-3 py-2 text-[11px] text-castle-warn">
              Loading settings for <code className="font-mono">{projectRoot}</code>. Until
              it finishes, the values below are still from{" "}
              <code className="font-mono">{loadedProjectRoot}</code>.
            </div>
          )}
          {/* Dangerous doctrine profiles — confirmed actions, never raw rows */}
          <OperatorProfileCards
            scope={settingsScope}
            projectRoot={surfaceRoot}
            locked={!readyForWrites || allEntries.length === 0}
            entries={allEntries}
            searchTerm={searchTerm}
          />

          {/* Quarantined drafts — guardrail keys that picked up a draft
              (e.g. before the operator surface loaded, or via a stale
              selection). They are EXCLUDED from normal Save All; the
              operator must route them through the expert / profile path. */}
          {quarantinedDirty.length > 0 && (
            <div className="mb-4 rounded-2xl border border-castle-deny/40 bg-castle-deny/[0.06] p-3">
              <div className="mb-1 text-[11px] font-black uppercase tracking-widest text-castle-deny">
                {quarantinedDirty.length} guardrail draft
                {quarantinedDirty.length === 1 ? "" : "s"} blocked from Save All
              </div>
              <div className="mb-2 text-[11px] text-castle-mute">
                These are protected settings, so the bulk Save skips them.
                Click one to edit it below with a one-click confirmation
                (profile-owned keys apply through their profile card).
              </div>
              <div className="flex flex-wrap gap-1.5">
                {quarantinedDirty.map((e) => (
                  <button
                    key={e.path}
                    type="button"
                    onClick={() => {
                      // Edit the quarantined draft itself, not a fresh prefill.
                      expertPrefilledFor.current = e.path;
                      setExpertKey(e.path);
                      setExpertValue(draftValues[draftKey(e.path)] ?? "");
                      setExpertConfirm("");
                      setExpertResult(null);
                    }}
                    className="rounded-md border border-castle-deny/40 bg-black/30 px-2 py-1 font-mono text-[10px] text-castle-deny hover:bg-castle-deny/15"
                    title="Edit this protected setting (confirmed apply)"
                  >
                    {e.path}
                  </button>
                ))}
              </div>
            </div>
          )}
          {visibleEntries.length === 0 ? (
            <div className="rounded-2xl border border-dashed border-castle-line bg-white/[0.02] p-8 text-center text-sm text-castle-mute">
              {searchTerm.trim()
                ? `No settings match "${searchTerm.trim()}".`
                : "No settings match the current filters."}
            </div>
          ) : (
            <div className="overflow-hidden rounded-2xl border border-castle-line bg-castle-card">
              <div className="sticky top-0 z-10 grid grid-cols-[1fr_auto_auto] items-center gap-3 border-b border-castle-line bg-castle-panel/95 px-3 py-2 text-[9px] font-black uppercase tracking-widest text-castle-mute backdrop-blur">
                <span>setting</span>
                <span className="grid grid-cols-4 gap-1 text-center">
                  <span className="min-w-[58px]">factory</span>
                  <span className="min-w-[58px]">global</span>
                  <span className="min-w-[58px]">project</span>
                  <span className="min-w-[58px]">session</span>
                </span>
                <span className="pr-1">live</span>
              </div>
              {visibleEntries.map((entry) => {
                const own = isOwn(entry);
                const danger = isDangerEntry(entry);
                const layer = (entry.effective_layer ?? "factory") as Layer;
                const isSelected = selectedPath === entry.path;
                const settable = isSettableAt(entry, settingsScope);
                return (
                  <div
                    key={entry.path}
                    onClick={() => onEntrySelect?.(entry.path)}
                    role="button"
                    tabIndex={0}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        onEntrySelect?.(entry.path);
                      }
                    }}
                    className={
                      "grid w-full cursor-pointer select-none grid-cols-[1fr_auto_auto_auto] items-center gap-3 border-b border-castle-line/50 px-3 py-2.5 text-left transition last:border-b-0 " +
                      (isSelected
                        ? "bg-castle-allow/10"
                        : "hover:bg-white/[0.02]") +
                      (danger ? " border-l-2 border-l-castle-deny/40" : "") +
                      (own && !danger ? " border-l-2 border-l-castle-flow/40" : "") +
                      (settable ? "" : " opacity-50")
                    }
                  >
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <code
                          className={
                            "truncate font-mono text-sm " +
                            (danger ? "text-castle-deny" : "text-slate-100")
                          }
                        >
                          {entry.path}
                        </code>
                        {danger && (
                          <CastlePill tone="danger">
                            <Lock className="mr-0.5 inline h-2.5 w-2.5" />
                            T0
                          </CastlePill>
                        )}
                        {entry.security_sensitive && !danger && (
                          <CastlePill tone="warn">
                            <ShieldCheck className="mr-0.5 inline h-2.5 w-2.5" />
                            sensitive
                          </CastlePill>
                        )}
                        {own && (
                          <CastlePill tone="flow">override</CastlePill>
                        )}
                        {!settable && (
                          <CastlePill tone="muted">
                            {`not set per ${settingsScope}`}
                          </CastlePill>
                        )}
                      </div>
                      {entry.description && (
                        <div
                          className="mt-0.5 truncate text-[11px] text-castle-mute"
                          title={entry.description}
                        >
                          {entry.description.replace(/\s+/g, " ")}
                        </div>
                      )}
                    </div>
                    <div className="flex items-center gap-1">
                      {(["factory", "global", "project", "session"] as const).map((sc) => {
                        const value =
                          sc === "factory"
                            ? entry.default
                            : entry.scope_values?.[sc];
                        const has = value !== undefined && value !== null;
                        const allowed = sc === "factory" || entry.allowed_scopes.includes(sc);
                        const isActive = (sc as string) === settingsScope;
                        const isEffective = layer === sc;
                        const text = has ? asText(value as unknown) : "—";
                        const truncated = text.length > 14 ? text.slice(0, 14) + "…" : text;
                        return (
                          <span
                            key={sc}
                            title={`${LAYER_LABEL[sc]}: ${has ? text : "unset"}`}
                            className={
                              "inline-flex h-7 min-w-[58px] items-center justify-center rounded-md border px-1.5 font-mono text-[10px] transition " +
                              (allowed ? "" : " opacity-30") +
                              (isActive ? " ring-2 ring-castle-allow/60 ring-offset-1 ring-offset-castle-card " : " ") +
                              (isEffective
                                ? sc === "factory"
                                  ? "border-castle-mute/50 bg-castle-mute/10 text-slate-200"
                                  : sc === "global"
                                  ? "border-castle-info/40 bg-castle-info/10 text-castle-info"
                                  : sc === "project"
                                  ? "border-castle-flow/40 bg-castle-flow/10 text-castle-flow"
                                  : "border-castle-allow/40 bg-castle-allow/10 text-castle-allow"
                                : "border-castle-line bg-black/30 " + (has ? "text-slate-300" : "text-castle-mute italic"))
                            }
                          >
                            {truncated}
                          </span>
                        );
                      })}
                    </div>
                    <div className="flex items-center gap-1.5">
                      {entry.requires_restart ? (
                        <CastlePill tone="warn">⟳</CastlePill>
                      ) : (
                        <CastlePill tone="ok">●</CastlePill>
                      )}
                    </div>
                    <button
                      type="button"
                      onClick={() => onEntrySelect?.(entry.path)}
                      title="Open detail panel to edit"
                      aria-label={`Open detail panel for ${entry.path}`}
                      className={
                        "flex h-7 w-7 items-center justify-center rounded-full border border-castle-line bg-black/30 text-castle-mute transition hover:border-castle-info/60 hover:bg-castle-info/10 hover:text-castle-info " +
                        (isSelected ? "border-castle-allow/60 bg-castle-allow/10 text-castle-allow" : "")
                      }
                    >
                      <Info className="h-3.5 w-3.5" />
                    </button>
                  </div>
                );
              })}
            </div>
          )}

          {/* ── Advanced Raw diagnostics ─────────────────────────── */}
          {showProtected && (
            <div className="mt-6 overflow-hidden rounded-2xl border border-castle-deny/30 bg-castle-deny/[0.04]">
              <div className="flex items-center gap-2 border-b border-castle-deny/20 px-4 py-2.5">
                <ShieldAlert className="h-4 w-4 text-castle-deny" />
                <span className="text-sm font-black uppercase tracking-widest text-castle-deny">
                  Protected settings
                </span>
                <span className="text-[11px] text-castle-mute">
                  These change how AIDOCS enforces security, so they take one
                  extra step: profile-owned keys are applied through their
                  profile card above; the rest are edited right here with a
                  one-click confirmation.
                </span>
              </div>
              <div>{protectedBefore.map(renderProtectedRow)}</div>
              {expertKey && (
                <div
                  ref={expertEditorRef}
                  className="border-y border-castle-deny/20 bg-black/20 p-4"
                >
                  <div className="mb-2 text-[11px] font-bold text-slate-200">
                    Change protected setting:{" "}
                    <code className="font-mono">{expertKey}</code>{" "}
                    <span className="text-castle-mute">(scope {settingsScope})</span>
                  </div>
                  {/* Operator ruling 2026-08-02: this surface had controls but no
                      instructions. Say where the operator is and what the ritual
                      means — settings are changed by humans, often mid-incident. */}
                  <div className="mb-3 space-y-1 text-[11px] leading-relaxed text-castle-mute">
                    <p>
                      This is the{" "}
                      <span className="font-bold text-castle-deny">
                        protected-settings surface
                      </span>
                      . Keys here control AIDOCS guardrails, so they are kept out
                      of normal Settings and can only be changed from this panel
                      by a signed-in operator — never by an agent.
                    </p>
                    <p>
                      Applying takes two deliberate clicks: the first fills in the
                      confirmation phrase so you can see exactly what you are
                      approving; the second performs the write. Typing the phrase
                      yourself works too. Every change made here is recorded in
                      the audit log.
                    </p>
                    <p>
                      The scope shown above is where the change lands:{" "}
                      <span className="text-slate-200">global</span> affects every
                      project on this machine,{" "}
                      <span className="text-slate-200">project</span> only this
                      project, <span className="text-slate-200">session</span>{" "}
                      only the current session until it ends.
                    </p>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    {expertMeta?.type === "boolean" ? (
                      <label className="flex min-w-[180px] flex-1 items-center gap-2 rounded-lg border border-castle-line bg-black/30 px-3 py-1.5 font-mono text-xs text-slate-100">
                        <input
                          type="checkbox"
                          checked={expertValue === "true"}
                          onChange={(e) =>
                            setExpertValue(e.target.checked ? "true" : "false")
                          }
                        />
                        <span>{expertValue === "true" ? "true" : "false"}</span>
                      </label>
                    ) : expertMeta?.allowedValues &&
                      expertMeta.allowedValues.length > 0 ? (
                      <select
                        value={expertShown(expertValue)}
                        onChange={(e) => setExpertTyped(e.target.value)}
                        className="min-w-[180px] flex-1 rounded-lg border border-castle-line bg-black/30 px-2 py-1.5 font-mono text-xs text-slate-100"
                      >
                        {expertMeta.allowedValues.map((v) => (
                          <option key={v} value={v}>
                            {v}
                          </option>
                        ))}
                      </select>
                    ) : expertMeta?.type === "string_list" ? (
                      <textarea
                        value={expertValue}
                        onChange={(e) => setExpertValue(e.target.value)}
                        rows={3}
                        placeholder='JSON list, e.g. ["*"]'
                        className="min-w-[180px] flex-1 rounded-lg border border-castle-line bg-black/30 px-2 py-1.5 font-mono text-xs text-slate-100"
                      />
                    ) : expertMeta?.valueFormat === "json_object" ||
                      expertMeta?.valueFormat === "json_array" ? (
                      <textarea
                        value={expertShown(expertValue)}
                        onChange={(e) => setExpertTyped(e.target.value)}
                        rows={5}
                        spellCheck={false}
                        placeholder={
                          expertMeta.valueFormat === "json_array"
                            ? "JSON array, e.g. []"
                            : "JSON object, e.g. {}"
                        }
                        className="min-w-[180px] flex-1 rounded-lg border border-castle-line bg-black/30 px-2 py-1.5 font-mono text-xs text-slate-100"
                      />
                    ) : (
                      <input
                        type={
                          expertMeta?.type === "integer" || expertMeta?.valueFormat === "number"
                            ? "number"
                            : "text"
                        }
                        step={expertMeta?.type === "integer" ? 1 : expertMeta?.valueFormat === "number" ? "any" : undefined}
                        min={expertMeta?.minimum ?? undefined}
                        max={expertMeta?.maximum ?? undefined}
                        value={expertShown(expertValue)}
                        onChange={(e) => setExpertTyped(e.target.value)}
                        placeholder={
                          expertMeta?.type
                            ? `${expertMeta.type} value`
                            : "value (JSON or raw)"
                        }
                        className="min-w-[180px] flex-1 rounded-lg border border-castle-line bg-black/30 px-2 py-1.5 font-mono text-xs text-slate-100"
                      />
                    )}
                    <input
                      value={expertConfirm}
                      onChange={(e) => setExpertConfirm(e.target.value)}
                      placeholder={`confirm-set ${expertKey}`}
                      className="min-w-[200px] rounded-lg border border-castle-deny/40 bg-black/30 px-2 py-1.5 font-mono text-xs text-castle-deny"
                    />
                    {/* ONE BUTTON, TWO CLICKS. It used to be two: a conditional
                        "I understand — confirm" that filled the phrase, plus an
                        always-enabled "Apply". Apply LOOKED ready when it was
                        not — pressing it first sent an empty confirmation, the
                        backend correctly refused with `confirmation_required`,
                        and the panel rendered that as a generic "the control
                        plane refused this action". The operator was one
                        unexplained click from the write and nothing said so.
                        The deliberateness is PRESERVED, not softened: still two
                        distinct clicks, still the exact phrase the backend
                        verifies. What changes is that both clicks live on the
                        SAME control, so the sequence is discoverable instead of
                        inferred. Typing the phrase by hand still arms it. */}
                    <button
                      type="button"
                      disabled={expertBusy || !!expertError || !readyForWrites}
                      onClick={() =>
                        expertArmed
                          ? void applyExpert()
                          : setExpertConfirm(expertConfirmPhrase)
                      }
                      title={
                        expertArmed
                          ? `Apply ${expertKey} — this click performs the write`
                          : "Click to confirm you understand; click again to apply"
                      }
                      className={
                        "rounded-lg border px-3 py-1.5 text-xs font-bold disabled:opacity-40 " +
                        (expertArmed
                          ? "border-castle-deny/50 bg-castle-deny/15 text-castle-deny hover:bg-castle-deny/25"
                          : "border-castle-warn/40 bg-castle-warn/10 text-castle-warn hover:bg-castle-warn/20")
                      }
                    >
                      {expertBusy
                        ? "Applying..."
                        : expertArmed
                          ? "Apply — click to write"
                          : "I understand — click to confirm"}
                    </button>
                    <button
                      type="button"
                      onClick={() => setExpertKey("")}
                      className="rounded-lg border border-castle-line px-3 py-1.5 text-xs text-castle-mute hover:text-slate-200"
                    >
                      Cancel
                    </button>
                  </div>
                  {expertError && (
                    <div role="alert" className="mt-2 text-[11px] text-castle-deny">
                      {expertError}
                    </div>
                  )}
                  {expertResult && (() => {
                    const ar = expertResult as AuthorityResult;
                    const banner = refusalBanner(ar);
                    const broad = broadeningWarning(ar);
                    return (
                      <div
                        className={
                          "mt-2 text-[11px] " +
                          (expertResult.ok
                            ? "text-castle-allow"
                            : "text-castle-deny")
                        }
                      >
                        {expertResult.ok ? (
                          <>
                            Applied.
                            {broad && (
                              <div className="mt-1" style={{ color: "#f59e0b" }}>
                                ⚠ {broad}
                              </div>
                            )}
                          </>
                        ) : banner ? (
                          <div>
                            <div className="font-semibold">{banner.title}</div>
                            <div>{banner.message}</div>
                            <div className="opacity-70">{banner.hint}</div>
                          </div>
                        ) : (
                          expertResult.message ||
                          expertResult.error ||
                          "Refused."
                        )}
                      </div>
                    );
                  })()}
                </div>
              )}
              <div>{protectedAfter.map(renderProtectedRow)}</div>
              {(managedCount > 0 || deprecatedCount > 0) && (
                <div className="flex items-center gap-2 px-4 py-2 text-[11px] text-castle-mute">
                  <Info className="h-3.5 w-3.5 shrink-0" />
                  <span>
                    {managedCount > 0 &&
                      `${managedCount} setting${managedCount === 1 ? "" : "s"} managed by the profile cards above`}
                    {managedCount > 0 && deprecatedCount > 0 && " · "}
                    {deprecatedCount > 0 && `${deprecatedCount} deprecated (hidden)`}
                    {" — edit via the owning profile card; nothing is lost."}
                  </span>
                </div>
              )}
            </div>
          )}
        </div>
      </main>
    </div>
  );
}
