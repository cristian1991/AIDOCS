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
import { useEffect, useMemo, useState } from "react";
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
import { asText } from "./dashboardUtils";
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
  if (!term) return true;
  const t = term.trim().toLowerCase();
  if (!t) return true;
  return (
    entry.path.toLowerCase().includes(t) ||
    entry.key.toLowerCase().includes(t) ||
    (entry.description || "").toLowerCase().includes(t) ||
    entry.section.toLowerCase().includes(t)
  );
}

export function SettingsPage({
  settingsScope,
  setSettingsScope,
  hasProject,
  hasSession,
  configSections,
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

  useEffect(() => {
    let live = true;
    operatorSurfaceRows()
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
        setRowsLoaded(false);
      });
  }, []);

  const draftKey = (path: string) => `${settingsScope}:${path}`;

  const allEntries = useMemo(
    () => configSections.flatMap(({ entries }) => entries),
    [configSections],
  );

  const categories = useMemo(() => {
    const counts = new Map<string, number>();
    for (const e of allEntries) {
      counts.set(e.section, (counts.get(e.section) ?? 0) + 1);
    }
    const dangerSet = new Set(["dev", "security", "rbac", "audit", "policies"]);
    return Array.from(counts.entries())
      .sort(([a], [b]) => {
        const aD = dangerSet.has(a) ? 1 : 0;
        const bD = dangerSet.has(b) ? 1 : 0;
        return aD - bD || a.localeCompare(b);
      })
      .map(([name, count]) => ({
        name,
        count,
        isDanger: dangerSet.has(name),
      }));
  }, [allEntries]);

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

  const visibleEntries = useMemo(() => {
    return allEntries.filter((e) => {
      if (isGuardrail(e)) return false; // never a normal row
      if (activeCategory !== "__all__" && e.section !== activeCategory) {
        return false;
      }
      if (!matchesSearch(e, searchTerm)) return false;
      if (activeFilters.has("modified") && !isOwn(e)) return false;
      if (activeFilters.has("security") && !e.security_sensitive) return false;
      if (activeFilters.has("danger") && !isDangerEntry(e)) return false;
      return true;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [allEntries, searchTerm, activeFilters, draftValues, settingsScope, activeCategory, advancedHidden, rowsLoaded]);

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
      return draft !== baseline;
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
    if (!rowsLoaded) return;
    const allDirty = dirtyEntries.filter((entry) =>
      isNormalRowAllowed(
        {
          path: entry.path,
          dashboard_only: entry.dashboard_only,
          is_t0: entry.is_t0,
          security_sensitive: entry.security_sensitive,
        },
        { rowsLoaded, advancedKeys: advancedHidden },
      ),
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
    if (!expertKey) return;
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
              onChange={(e) => setSearchTerm(e.target.value)}
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
              {allEntries.length}
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
                disabled={!hasDirty || !!savingSetting || !rowsLoaded || !configEditingAvailable()}
                title={
                  !configEditingAvailable()
                    ? "Config editing runs in the local / desktop app - read-only over WebAgent."
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
          {/* Dangerous doctrine profiles — confirmed actions, never raw rows */}
          <OperatorProfileCards scope={settingsScope} />

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
              No settings match the current filters.
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
                      (own && !danger ? " border-l-2 border-l-castle-flow/40" : "")
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
                      </div>
                      {entry.description && (
                        <div className="mt-0.5 truncate text-[11px] text-castle-mute">
                          {entry.description.replace(/\s+/g, " ").slice(0, 120)}
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
          {advancedRows.length > 0 && (
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
              <div>
                {(() => {
                  // #99 (operator 2026-07-15): the panel was cluttered with
                  // read-only rows that only said "use the profile card"
                  // (route 'profile') and deprecated no-ops (route 'blocked').
                  // Both duplicate the profile CARDS above / carry no action.
                  // Render ONLY operator-actionable rows (route 'expert');
                  // collapse the rest into ONE honest summary line so nothing
                  // vanishes silently. The fail-closed normal-settings hider
                  // (advancedHidden) still uses the FULL advanced_raw set, so
                  // this is a display-only trim with no security change.
                  const routed = advancedRows.map((row) => {
                    const key = rowField(row, "key");
                    return {
                      row,
                      key,
                      owner: rowField(row, "owning_profile"),
                      route: saveRouteFor({
                        key,
                        service_managed: rowField(row, "service_managed") || null,
                        deprecated: rowField(row, "deprecated") || null,
                        dashboard_only: Boolean(
                          (row as Record<string, unknown>).dashboard_only,
                        ),
                        security_sensitive: Boolean(
                          (row as Record<string, unknown>).security_sensitive,
                        ),
                      }),
                    };
                  });
                  const actionable = routed.filter((r) => r.route === "expert");
                  const managedCount = routed.filter(
                    (r) => r.route === "profile",
                  ).length;
                  const deprecatedCount = routed.filter(
                    (r) => r.route === "blocked",
                  ).length;
                  return (
                    <>
                      {actionable.map(({ key, owner }) => (
                        <div
                          key={key}
                          className="flex items-center gap-3 border-b border-castle-deny/10 px-4 py-2 last:border-b-0"
                        >
                          <code className="min-w-0 flex-1 truncate font-mono text-xs text-slate-200">
                            {key}
                          </code>
                          <CastlePill tone="muted">{owner}</CastlePill>
                          <span className="hidden text-[11px] text-castle-mute md:inline">
                            Editable here — applying asks for a one-click
                            confirmation.
                          </span>
                          <button
                            type="button"
                            onClick={() => {
                              setExpertKey(key);
                              setExpertValue("");
                              setExpertConfirm("");
                              setExpertResult(null);
                            }}
                            className="rounded-lg border border-castle-deny/40 bg-castle-deny/10 px-2.5 py-1 text-[11px] font-bold text-castle-deny hover:bg-castle-deny/20"
                          >
                            Edit
                          </button>
                        </div>
                      ))}
                      {(managedCount > 0 || deprecatedCount > 0) && (
                        <div className="flex items-center gap-2 px-4 py-2 text-[11px] text-castle-mute">
                          <Info className="h-3.5 w-3.5 shrink-0" />
                          <span>
                            {managedCount > 0 &&
                              `${managedCount} setting${managedCount === 1 ? "" : "s"} managed by the profile cards above`}
                            {managedCount > 0 && deprecatedCount > 0 && " · "}
                            {deprecatedCount > 0 &&
                              `${deprecatedCount} deprecated (hidden)`}
                            {" — edit via the owning profile card; nothing is lost."}
                          </span>
                        </div>
                      )}
                    </>
                  );
                })()}
              </div>
              {expertKey && (
                <div className="border-t border-castle-deny/20 bg-black/20 p-4">
                  <div className="mb-2 text-[11px] font-bold text-slate-200">
                    Change protected setting:{" "}
                    <code className="font-mono">{expertKey}</code>{" "}
                    <span className="text-castle-mute">(scope {settingsScope})</span>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <input
                      value={expertValue}
                      onChange={(e) => setExpertValue(e.target.value)}
                      placeholder="value (JSON or raw)"
                      className="min-w-[180px] flex-1 rounded-lg border border-castle-line bg-black/30 px-2 py-1.5 font-mono text-xs text-slate-100"
                    />
                    <input
                      value={expertConfirm}
                      onChange={(e) => setExpertConfirm(e.target.value)}
                      placeholder={`confirm-set ${expertKey}`}
                      className="min-w-[200px] rounded-lg border border-castle-deny/40 bg-black/30 px-2 py-1.5 font-mono text-xs text-castle-deny"
                    />
                    {expertConfirm.trim() !== `confirm-set ${expertKey}` && (
                      <button
                        type="button"
                        onClick={() => setExpertConfirm(`confirm-set ${expertKey}`)}
                        title="Fill in the confirmation phrase — clicking this IS your deliberate confirmation"
                        className="rounded-lg border border-castle-warn/40 bg-castle-warn/10 px-2.5 py-1.5 text-[11px] font-bold text-castle-warn hover:bg-castle-warn/20"
                      >
                        I understand — confirm
                      </button>
                    )}
                    <button
                      type="button"
                      disabled={expertBusy}
                      onClick={applyExpert}
                      className="rounded-lg border border-castle-deny/50 bg-castle-deny/15 px-3 py-1.5 text-xs font-bold text-castle-deny hover:bg-castle-deny/25 disabled:opacity-40"
                    >
                      {expertBusy ? "Applying..." : "Apply"}
                    </button>
                    <button
                      type="button"
                      onClick={() => setExpertKey("")}
                      className="rounded-lg border border-castle-line px-3 py-1.5 text-xs text-castle-mute hover:text-slate-200"
                    >
                      Cancel
                    </button>
                  </div>
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
            </div>
          )}
        </div>
      </main>
    </div>
  );
}
