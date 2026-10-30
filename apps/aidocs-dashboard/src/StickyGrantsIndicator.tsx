/**
 * StickyGrantsIndicator — backlog #21 (2026-07).
 *
 * Always-visible sticky-perms signal: a header badge with (!) that
 * appears whenever the selected session has active sticky user-intent
 * grants (tier-1 tool grants / tier-2 bash subcommand scopes). Click
 * drills into the grant list. No sticky active = no badge — sticky
 * must never feel like a silent security gap, but the indicator must
 * not eat chrome space when there is nothing to warn about.
 *
 * Data: snapshot.sticky_grants (runtime_presentation_service.
 * dashboard_sticky_grants — display surface, never epoch-filtered).
 *
 * Revoke: `onRevokeTool` is optional — when the host app has a revoke
 * transport wired (StickyGrantsStore.revoke_tool endpoint), each tool
 * group gets a one-click revoke button. Absent handler = list-only.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import type { StickyGrantRow, StickyGrantsSnapshot } from "./dashboardApi";

export type StickyGrantsIndicatorProps = {
  sticky: StickyGrantsSnapshot | null | undefined;
  onRevokeTool?: (tool: string) => void | Promise<void>;
};

type ToolGroup = {
  tool: string;
  grants: StickyGrantRow[];
};

export function StickyGrantsIndicator({
  sticky,
  onRevokeTool,
}: StickyGrantsIndicatorProps) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement | null>(null);

  // Group by tool so `bash (5 subcommand scopes)` renders compactly
  // instead of five near-identical rows (badge-fatigue guard).
  const groups = useMemo<ToolGroup[]>(() => {
    const byTool = new Map<string, StickyGrantRow[]>();
    for (const grant of sticky?.grants ?? []) {
      const list = byTool.get(grant.tool) ?? [];
      list.push(grant);
      byTool.set(grant.tool, list);
    }
    return [...byTool.entries()]
      .map(([tool, grants]) => ({ tool, grants }))
      .sort((a, b) => a.tool.localeCompare(b.tool));
  }, [sticky]);

  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    window.addEventListener("mousedown", handler);
    return () => window.removeEventListener("mousedown", handler);
  }, [open]);

  const count = sticky?.count ?? 0;
  if (!count) return null; // no sticky active = no badge

  return (
    <div ref={wrapRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex h-10 items-center gap-1.5 rounded-xl border border-castle-warn/40 bg-castle-warn/10 px-3 text-xs font-bold uppercase tracking-widest text-castle-warn hover:bg-castle-warn/20"
        title={`${count} sticky permission grant${count === 1 ? "" : "s"} active for this session — click for details`}
      >
        <span aria-hidden="true">(!)</span>
        <span>sticky ×{count}</span>
      </button>
      {open && (
        <div className="absolute right-0 top-12 z-50 w-96 rounded-2xl border border-castle-line bg-castle-panel p-3 shadow-2xl">
          <div className="mb-2 text-[10px] font-black uppercase tracking-widest text-castle-warn">
            Active sticky grants — session {sticky?.session_id ?? "?"}
          </div>
          <div className="mb-2 text-[11px] leading-snug text-castle-mute">
            These tools stay granted for the rest of the session without
            further prompts. Revoke anything you no longer intend to keep
            elevated.
          </div>
          <ul className="flex max-h-72 flex-col gap-2 overflow-y-auto">
            {groups.map((group) => (
              <li
                key={group.tool}
                className="rounded-xl border border-castle-line bg-black/25 p-2"
              >
                <div className="flex items-center justify-between gap-2">
                  <div className="min-w-0">
                    <code className="text-xs font-bold text-slate-200">
                      {group.tool}
                    </code>
                    <span className="ml-2 text-[10px] uppercase tracking-widest text-castle-mute">
                      {group.grants.length === 1 &&
                      !group.grants[0].subcommand
                        ? `tier ${group.grants[0].tier}`
                        : `${group.grants.length} scope${group.grants.length === 1 ? "" : "s"}`}
                    </span>
                  </div>
                  {onRevokeTool ? (
                    <button
                      type="button"
                      className="rounded-lg border border-castle-deny/40 bg-castle-deny/10 px-2 py-0.5 text-[10px] font-bold uppercase tracking-widest text-castle-deny hover:bg-castle-deny/25"
                      onClick={() => void onRevokeTool(group.tool)}
                      title={`Revoke every sticky grant for ${group.tool} (audited)`}
                    >
                      revoke
                    </button>
                  ) : null}
                </div>
                <ul className="mt-1 flex flex-col gap-0.5">
                  {group.grants.map((grant) => (
                    <li
                      key={grant.grant_id}
                      className="flex items-baseline justify-between gap-2 text-[11px] text-castle-mute"
                    >
                      <span className="truncate">
                        {grant.subcommand ? (
                          <code>{grant.subcommand}</code>
                        ) : (
                          <span>full tool</span>
                        )}
                        {grant.registered_by ? ` · ${grant.registered_by}` : ""}
                      </span>
                      <span className="shrink-0">
                        {(grant.registered_at || "").slice(0, 16).replace("T", " ")}
                      </span>
                    </li>
                  ))}
                </ul>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
