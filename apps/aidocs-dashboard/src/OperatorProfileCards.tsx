/**
 * OperatorProfileCards — operator profile cards for the two dangerous
 * doctrine areas (Breakglass & Flavor, Authority Exceptions / Border Law).
 * A change is applied as one confirmed action through operatorSurfaceApply,
 * which the backend requires to carry the exact confirm token AND a
 * non-empty reason. A confirmed action may write the profile's own
 * dashboard-only/security-sensitive member keys, but can never reach a
 * service-managed or deprecated key.
 *
 * Each member key gets the same typed control as a normal setting
 * (checkbox, choice list, number, list, text), prefilled from its current
 * value; only the members the operator changed are sent.
 */
import { useEffect, useState } from "react";
import { ShieldAlert } from "lucide-react";
import type {
  DashboardConfigEntry,
  OperatorProfileSummary,
  OperatorSurfaceResult,
} from "./dashboardApi";
import { operatorSurfaceApply, operatorSurfaceList } from "./dashboardApi";
import { validateSettingDraft } from "./dashboardUtils";
import { profileConfirmToken } from "./settingsRouting";
import { matchesTerm, profileFormValues, profileValuesJson } from "./settingsModel";

const DANGEROUS_IDS = ["breakglass_flavor", "authority_border"] as const;

interface CardState {
  open: boolean;
  form: Record<string, string>;
  start: Record<string, string>;
  reason: string;
  confirm: string;
  busy: boolean;
  result: OperatorSurfaceResult | null;
}

const EMPTY: CardState = {
  open: false,
  form: {},
  start: {},
  reason: "",
  confirm: "",
  busy: false,
  result: null,
};

const INPUT =
  "w-full rounded-lg border border-castle-line bg-black/30 px-2 py-1.5 font-mono text-xs text-slate-100";

function MemberControl({
  entry,
  value,
  label,
  onChange,
}: {
  entry: DashboardConfigEntry | undefined;
  value: string;
  label: string;
  onChange: (next: string) => void;
}) {
  if (entry?.type === "boolean") {
    return (
      <label className="flex items-center gap-2 font-mono text-xs text-slate-100">
        <input
          type="checkbox"
          aria-label={label}
          checked={value === "true"}
          onChange={(e) => onChange(e.target.checked ? "true" : "false")}
        />
        <span>{value === "true" ? "on" : "off"}</span>
      </label>
    );
  }
  if (entry?.allowed_values && entry.allowed_values.length > 0) {
    return (
      <select
        aria-label={label}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className={INPUT}
      >
        {!entry.allowed_values.includes(value) && (
          <option value={value}>{value || "(unset)"}</option>
        )}
        {entry.allowed_values.map((v) => (
          <option key={v} value={v}>
            {entry.value_descriptions?.[v] ? `${v} — ${entry.value_descriptions[v]}` : v}
          </option>
        ))}
      </select>
    );
  }
  if (entry?.type === "string_list") {
    return (
      <textarea
        aria-label={label}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        rows={2}
        placeholder="one per line, or comma-separated"
        className={INPUT}
      />
    );
  }
  return (
    <input
      aria-label={label}
      type={entry?.type === "integer" ? "number" : "text"}
      min={entry?.minimum ?? undefined}
      max={entry?.maximum ?? undefined}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className={INPUT}
    />
  );
}

export function OperatorProfileCards({
  scope = "global",
  projectRoot,
  entries = [],
  searchTerm = "",
  locked = false,
}: {
  scope?: string;
  /** The project `entries` were loaded for; Apply writes there. */
  projectRoot?: string;
  entries?: DashboardConfigEntry[];
  searchTerm?: string;
  /** True while a project switch is loading: nothing may be applied. */
  locked?: boolean;
}) {
  const [profiles, setProfiles] = useState<OperatorProfileSummary[]>([]);
  const [cards, setCards] = useState<Record<string, CardState>>({});
  const byPath = new Map(entries.map((e) => [e.path, e]));

  useEffect(() => {
    let live = true;
    operatorSurfaceList(projectRoot)
      .then((res) => {
        if (!live) return;
        const dangerous = (res.profiles ?? []).filter((p) =>
          (DANGEROUS_IDS as readonly string[]).includes(p.id),
        );
        setProfiles(dangerous);
        setCards(Object.fromEntries(dangerous.map((p) => [p.id, { ...EMPTY }])));
      })
      .catch(() => {
        if (live) setProfiles([]);
      });
    return () => {
      live = false;
    };
  }, [projectRoot]);

  function patch(id: string, next: Partial<CardState>) {
    setCards((prev) => ({ ...prev, [id]: { ...(prev[id] ?? EMPTY), ...next } }));
  }

  function toggleOpen(p: OperatorProfileSummary) {
    const cur = cards[p.id] ?? EMPTY;
    if (cur.open) {
      patch(p.id, { open: false });
      return;
    }
    // Prefill from the values on screen now, so the form starts from truth.
    const start = profileFormValues(p.keys, entries);
    patch(p.id, { open: true, form: start, start, result: null });
  }

  async function apply(p: OperatorProfileSummary) {
    if (locked) return;
    const c = cards[p.id];
    patch(p.id, { busy: true, result: null });
    try {
      const res = await operatorSurfaceApply({
        profileId: p.id,
        projectRoot,
        valuesJson: profileValuesJson(c.form, c.start, entries),
        confirm: c.confirm,
        reason: c.reason,
        scope,
      });
      patch(p.id, res.ok ? { result: res, start: c.form, confirm: "" } : { result: res });
    } catch (e) {
      patch(p.id, { result: { ok: false, message: String(e) } });
    } finally {
      patch(p.id, { busy: false });
    }
  }

  const shown = profiles.filter((p) =>
    matchesTerm(
      [p.title, p.doctrine_area, ...p.keys, ...p.keys.map((k) => byPath.get(k)?.description)],
      searchTerm,
    ),
  );
  if (shown.length === 0) return null;

  return (
    <div className="mb-6 grid gap-3 md:grid-cols-2">
      {shown.map((p) => {
        const c = cards[p.id] ?? EMPTY;
        const expected = profileConfirmToken(p.id);
        const errors = p.keys
          .map((k) => {
            const e = byPath.get(k);
            if (!e || c.form[k] === c.start[k]) return null;
            const problem = validateSettingDraft(e, c.form[k] ?? "");
            return problem ? `${k}: ${problem}` : null;
          })
          .filter((x): x is string => !!x);
        const changed =
          c.open && errors.length === 0
            ? profileValuesJson(c.form, c.start, entries) !== "{}"
            : c.open && p.keys.some((k) => c.form[k] !== c.start[k]);
        const ready =
          !locked &&
          changed &&
          errors.length === 0 &&
          c.confirm.trim() === expected &&
          c.reason.trim().length > 0;
        return (
          <div
            key={p.id}
            className="overflow-hidden rounded-2xl border border-castle-deny/30 bg-castle-deny/[0.04]"
          >
            <button
              type="button"
              onClick={() => toggleOpen(p)}
              aria-expanded={c.open}
              className="flex w-full items-center gap-2 px-4 py-3 text-left"
            >
              <ShieldAlert className="h-4 w-4 shrink-0 text-castle-deny" />
              <span className="flex-1">
                <span className="block text-sm font-black text-slate-100">
                  {p.title}
                </span>
                <span className="block text-[11px] text-castle-mute">
                  {p.keys.length} setting{p.keys.length === 1 ? "" : "s"}, changed together
                  with a reason and a confirmation
                </span>
              </span>
              <span className="text-[11px] text-castle-mute">{c.open ? "Close" : "Open"}</span>
            </button>
            {c.open && (
              <div className="border-t border-castle-deny/20 bg-black/20 p-4">
                <div className="mb-3 space-y-3">
                  {p.keys.map((k) => {
                    const e = byPath.get(k);
                    return (
                      <div key={k}>
                        <div className="mb-1 flex items-baseline gap-2">
                          <code className="font-mono text-[11px] text-slate-200">{k}</code>
                          {c.form[k] !== c.start[k] && (
                            <span className="text-[10px] text-castle-warn">changed</span>
                          )}
                        </div>
                        {e?.description && (
                          <div className="mb-1 text-[11px] text-castle-mute">{e.description}</div>
                        )}
                        <MemberControl
                          entry={e}
                          label={k}
                          value={c.form[k] ?? ""}
                          onChange={(next) =>
                            patch(p.id, { form: { ...c.form, [k]: next }, result: null })
                          }
                        />
                      </div>
                    );
                  })}
                </div>
                {errors.length > 0 && (
                  <div role="alert" className="mb-2 text-[11px] text-castle-deny">
                    {errors.join(" ")}
                  </div>
                )}
                <input
                  value={c.reason}
                  onChange={(e) => patch(p.id, { reason: e.target.value })}
                  placeholder="Why are you changing this? (required)"
                  aria-label="reason"
                  className="mb-2 w-full rounded-lg border border-castle-line bg-black/30 px-2 py-1.5 text-xs text-slate-100"
                />
                <input
                  value={c.confirm}
                  onChange={(e) => patch(p.id, { confirm: e.target.value })}
                  placeholder={`type: ${expected}`}
                  aria-label="confirmation phrase"
                  className="mb-2 w-full rounded-lg border border-castle-deny/40 bg-black/30 px-2 py-1.5 font-mono text-xs text-castle-deny"
                />
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    disabled={!ready || c.busy}
                    onClick={() => apply(p)}
                    className="rounded-lg border border-castle-deny/50 bg-castle-deny/15 px-3 py-1.5 text-xs font-bold text-castle-deny hover:bg-castle-deny/25 disabled:opacity-40"
                  >
                    {c.busy ? "Applying..." : "Apply"}
                  </button>
                  <span className="text-[10px] text-castle-mute">
                    {locked
                      ? "Wait for the selected project to finish loading."
                      : !changed
                      ? "Change at least one setting above."
                      : `Applies at ${scope} scope. Type the phrase and a reason to enable Apply.`}
                  </span>
                </div>
                {c.result && (
                  <div
                    className={
                      "mt-2 text-[11px] " +
                      (c.result.ok ? "text-castle-allow" : "text-castle-deny")
                    }
                  >
                    {c.result.ok
                      ? "Applied."
                      : c.result.message || c.result.error || "Refused."}
                  </div>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
