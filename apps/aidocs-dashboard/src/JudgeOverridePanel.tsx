/**
 * JudgeOverridePanel — backlog #22 (+ renders the #19 family-split
 * data shape).
 *
 * Editor for `security.judge_override.<family>` (per-family judge-rule
 * opt-out lists). Operator-clarity guarantees from the #22 audit
 * residual:
 *
 *   1. Rules render grouped by verdict CLASS (judge_taxonomy.classify)
 *      with class label + per-rule risk, family and lock state.
 *   2. Class subtotals are always visible while editing — "2 of 3
 *      rules overridden" — so the operator sees they are one override
 *      away from disabling a class entirely.
 *   3. Overriding the LAST active rule in a class asks for explicit
 *      confirmation before saving.
 *   4. Help text explains within-class thinning vs class-shift.
 *
 * Locked rules (credential exfil, download-then-execute, catastrophic
 * destructive) render as LOCKED and cannot be toggled — the writer
 * refuses them and enforcement ignores them regardless.
 *
 * Audit: every save lands per-rule judge_rule_disabled / enabled
 * events server-side (judge_overrides.set_judge_override) plus the
 * config_write audit row from the config path itself.
 */
import { useMemo, useState } from "react";
import type { JudgeOverridesSnapshot, JudgeRuleRow } from "./dashboardApi";

export type JudgeOverridePanelProps = {
  data: JudgeOverridesSnapshot | null | undefined;
  activeLayer: "global" | "project" | "session";
  canEdit: boolean;
  saving?: boolean;
  /** Persist one family's full opt-out list (rule_ids). */
  onSaveFamily: (family: string, ruleIds: string[]) => void;
};

const CLASS_ORDER = [
  "malicious_forbidden",
  "confirmable_destructive",
  "safe_advisory",
] as const;

const CLASS_LABEL: Record<string, string> = {
  malicious_forbidden: "Malicious / forbidden — always hard-blocks",
  confirmable_destructive: "Confirmable destructive — freeze + operator confirm",
  safe_advisory: "Advisory — passes, recorded",
};

const CLASS_TONE: Record<string, string> = {
  malicious_forbidden: "text-castle-deny border-castle-deny/40",
  confirmable_destructive: "text-castle-warn border-castle-warn/40",
  safe_advisory: "text-castle-info border-castle-info/40",
};

export function JudgeOverridePanel({
  data,
  activeLayer,
  canEdit,
  saving,
  onSaveFamily,
}: JudgeOverridePanelProps) {
  const [collapsed, setCollapsed] = useState(true);
  const [searchTerm, setSearchTerm] = useState("");

  const rules = useMemo(() => data?.rules ?? [], [data]);

  const byClass = useMemo(() => {
    const grouped = new Map<string, JudgeRuleRow[]>();
    for (const rule of rules) {
      const cls = rule.verdict_class || "confirmable_destructive";
      const list = grouped.get(cls) ?? [];
      list.push(rule);
      grouped.set(cls, list);
    }
    for (const list of grouped.values()) {
      list.sort((a, b) => a.rule_id.localeCompare(b.rule_id));
    }
    return grouped;
  }, [rules]);

  const overriddenCount = useMemo(
    () => rules.filter((r) => r.overridden).length,
    [rules],
  );

  if (!data || rules.length === 0) return null;

  function toggleRule(rule: JudgeRuleRow) {
    if (!canEdit || rule.locked || !data) return;
    const cls = rule.verdict_class || "confirmable_destructive";
    const classRules = byClass.get(cls) ?? [];
    const activeInClass = classRules.filter((r) => !r.overridden && !r.locked);
    if (
      !rule.overridden &&
      activeInClass.length === 1 &&
      activeInClass[0].rule_id === rule.rule_id
    ) {
      // #22 requirement 3: last surviving rule in the class.
      const ok = window.confirm(
        `This override removes the LAST active rule in class ` +
          `"${CLASS_LABEL[cls] ?? cls}".\n\nVerdicts that fall solely ` +
          `into this class will no longer block.\n\nContinue?`,
      );
      if (!ok) return;
    }
    // Build the family's next opt-out list from the current state.
    const family = rule.family;
    const familyOverrides = new Set(
      rules
        .filter((r) => r.family === family && r.overridden && !r.locked)
        .map((r) => r.rule_id),
    );
    if (rule.overridden) familyOverrides.delete(rule.rule_id);
    else familyOverrides.add(rule.rule_id);
    onSaveFamily(family, [...familyOverrides].sort());
  }

  return (
    <div className="mt-4 rounded-2xl border border-castle-line bg-white/[0.02]">
      <header
        className="flex cursor-pointer items-center gap-3 px-4 py-3"
        onClick={() => setCollapsed((c) => !c)}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") setCollapsed((c) => !c);
        }}
      >
        <span aria-hidden="true" className="text-castle-mute">
          {collapsed ? "▸" : "▾"}
        </span>
        <strong className="text-sm text-slate-200">Judge rule overrides</strong>
        <span className="text-[11px] uppercase tracking-widest text-castle-mute">
          {overriddenCount} of {rules.length} rules overridden
        </span>
        {overriddenCount > 0 && (
          <span className="rounded-full border border-castle-warn/40 bg-castle-warn/10 px-2 py-0.5 text-[10px] font-bold uppercase tracking-widest text-castle-warn">
            protection thinned
          </span>
        )}
      </header>

      {!collapsed && (
        <div className="border-t border-castle-line px-4 py-3">
          <input
            type="search"
            className="settings-search mb-3"
            placeholder="Filter rules — id / description match"
            value={searchTerm}
            onChange={(e) => setSearchTerm(e.target.value)}
          />

          {CLASS_ORDER.filter((cls) => byClass.has(cls)).map((cls) => {
            const classRules = (byClass.get(cls) ?? []).filter((rule) => {
              const term = searchTerm.trim().toLowerCase();
              if (!term) return true;
              return (
                rule.rule_id.toLowerCase().includes(term) ||
                rule.description.toLowerCase().includes(term) ||
                rule.family.toLowerCase().includes(term)
              );
            });
            const total = byClass.get(cls)?.length ?? 0;
            const overridden =
              byClass.get(cls)?.filter((r) => r.overridden).length ?? 0;
            const lastRuleClose = total - overridden === 1;
            return (
              <section key={cls} className="mb-4">
                <div
                  className={
                    "mb-1 flex items-center gap-2 border-b pb-1 text-[11px] font-bold uppercase tracking-widest " +
                    (CLASS_TONE[cls] ?? "text-castle-mute border-castle-line")
                  }
                >
                  <span>{CLASS_LABEL[cls] ?? cls}</span>
                  {/* #22 requirement 2: class subtotal, always visible. */}
                  <span className="font-normal normal-case tracking-normal text-castle-mute">
                    {overridden} of {total} rules overridden
                  </span>
                  {lastRuleClose && overridden > 0 && (
                    <span className="font-normal normal-case tracking-normal text-castle-warn">
                      one override away from disabling this class
                    </span>
                  )}
                </div>
                <ul className="flex flex-col">
                  {classRules.map((rule) => (
                    <li
                      key={rule.rule_id}
                      className="flex items-center gap-2 rounded px-1 py-0.5 text-xs hover:bg-white/[0.03]"
                    >
                      <input
                        type="checkbox"
                        checked={rule.overridden}
                        disabled={!canEdit || rule.locked || !!saving}
                        onChange={() => toggleRule(rule)}
                        title={
                          rule.locked
                            ? "LOCKED — this rule can never be overridden"
                            : rule.overridden
                            ? `Re-enable ${rule.rule_id}`
                            : `Override (disable) ${rule.rule_id} at ${activeLayer}`
                        }
                      />
                      <code
                        className={
                          rule.overridden
                            ? "text-castle-mute line-through"
                            : "text-slate-200"
                        }
                      >
                        {rule.rule_id}
                      </code>
                      <span className="rounded border border-castle-line px-1 text-[10px] uppercase tracking-widest text-castle-mute">
                        {rule.family}
                      </span>
                      <span className="text-[10px] uppercase tracking-widest text-castle-mute">
                        {rule.risk}
                      </span>
                      {rule.locked && (
                        <span className="rounded border border-castle-deny/40 bg-castle-deny/10 px-1 text-[10px] font-bold uppercase tracking-widest text-castle-deny">
                          locked
                        </span>
                      )}
                      <span className="min-w-0 flex-1 truncate text-castle-mute">
                        {rule.description}
                      </span>
                    </li>
                  ))}
                </ul>
              </section>
            );
          })}

          {/* #22 requirement 4: within-class thinning vs class-shift. */}
          <p className="mt-2 border-t border-castle-line pt-2 text-[11px] leading-snug text-castle-mute">
            Overrides thin protection <em>within</em> a class — they never
            shift a verdict's dominant class. To fully unblock a
            multi-class judge catch, every active rule_id in every active
            class must be overridden; partial overrides preserve the
            dominant class. Overriding the last active rule in a class
            removes that class's defense entirely for verdicts that fall
            solely into it. Locked rules (credential exfiltration,
            download-then-execute chains, catastrophic destructive
            patterns) always stay active. Every change is audited
            (judge_rule_disabled / judge_rule_enabled).
          </p>
        </div>
      )}
    </div>
  );
}
