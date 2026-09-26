/**
 * CommandPalette — Phase 6e (2026-05-02).
 *
 * Linear/Raycast-style overlay. Single search input, fuzzy results
 * categorized by kind (Navigate / Settings / Actions). Keyboard-
 * driven: ↑/↓ to move, Enter to fire, Esc to close, Tab to filter
 * to one category.
 *
 * The palette is presentational — it doesn't own action handlers.
 * App.tsx passes commands as a flat list of {id, kind, label,
 * subtitle?, run}. The palette filters + ranks + renders.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowRight,
  Command as CommandIcon,
  Gauge,
  Search,
  Settings as SettingsIcon,
  Zap,
} from "lucide-react";

export type PaletteCommandKind = "navigate" | "setting" | "action";

export type PaletteCommand = {
  id: string;
  kind: PaletteCommandKind;
  label: string;
  subtitle?: string;
  /** Optional shortcut hint shown on the right. */
  shortcut?: string;
  /** Fired when the user picks this command. The palette closes
   * automatically before run() is called. */
  run: () => void;
};

export type CommandPaletteProps = {
  open: boolean;
  onClose: () => void;
  commands: PaletteCommand[];
};

const KIND_LABEL: Record<PaletteCommandKind, string> = {
  navigate: "Navigate",
  setting: "Settings",
  action: "Actions",
};

const KIND_ORDER: PaletteCommandKind[] = ["action", "navigate", "setting"];

const KIND_ICON: Record<
  PaletteCommandKind,
  React.ComponentType<{ className?: string }>
> = {
  navigate: Gauge,
  setting: SettingsIcon,
  action: Zap,
};

function fuzzyScore(haystack: string, needle: string): number {
  // Cheap fuzzy: returns higher score for closer match. 0 = no match.
  if (!needle) return 1;
  const h = haystack.toLowerCase();
  const n = needle.toLowerCase().trim();
  if (!n) return 1;
  if (h === n) return 1000;
  if (h.startsWith(n)) return 500;
  if (h.includes(n)) return 200 + (1 / (1 + h.indexOf(n))) * 100;
  // Subsequence match — every char in needle appears in haystack in order.
  let hi = 0;
  let matched = 0;
  for (let ni = 0; ni < n.length; ni++) {
    while (hi < h.length && h[hi] !== n[ni]) hi++;
    if (hi >= h.length) return 0;
    matched += 1;
    hi += 1;
  }
  return matched === n.length ? 50 : 0;
}

export function CommandPalette({
  open,
  onClose,
  commands,
}: CommandPaletteProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const [kindFilter, setKindFilter] = useState<PaletteCommandKind | null>(null);

  // Reset state when opening.
  useEffect(() => {
    if (open) {
      setQuery("");
      setActiveIndex(0);
      setKindFilter(null);
      // Defer focus so the input is mounted.
      const t = setTimeout(() => inputRef.current?.focus(), 10);
      return () => clearTimeout(t);
    }
  }, [open]);

  // Filter + rank.
  const ranked = useMemo(() => {
    const filtered = kindFilter
      ? commands.filter((c) => c.kind === kindFilter)
      : commands;
    if (!query.trim()) return filtered;
    return filtered
      .map((cmd) => {
        const labelScore = fuzzyScore(cmd.label, query);
        const subtitleScore = cmd.subtitle
          ? fuzzyScore(cmd.subtitle, query) * 0.5
          : 0;
        const idScore = fuzzyScore(cmd.id, query) * 0.7;
        const score = Math.max(labelScore, subtitleScore, idScore);
        return { cmd, score };
      })
      .filter((row) => row.score > 0)
      .sort((a, b) => b.score - a.score)
      .map((row) => row.cmd);
  }, [commands, query, kindFilter]);

  // Group by kind.
  const grouped = useMemo(() => {
    const groups = new Map<PaletteCommandKind, PaletteCommand[]>();
    for (const cmd of ranked) {
      groups.set(cmd.kind, [...(groups.get(cmd.kind) ?? []), cmd]);
    }
    return KIND_ORDER.filter((k) => groups.has(k)).map((k) => ({
      kind: k,
      items: groups.get(k)!,
    }));
  }, [ranked]);

  // Keyboard handling.
  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
        return;
      }
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setActiveIndex((i) => Math.min(ranked.length - 1, i + 1));
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setActiveIndex((i) => Math.max(0, i - 1));
      } else if (e.key === "Enter") {
        e.preventDefault();
        const target = ranked[activeIndex];
        if (target) {
          onClose();
          // Defer to next tick so the palette is fully closed before
          // the action fires (in case the action navigates or opens
          // another modal).
          setTimeout(() => target.run(), 0);
        }
      } else if (e.key === "Tab") {
        e.preventDefault();
        // Cycle kind filter: null → action → navigate → setting → null.
        setKindFilter((current) => {
          if (current === null) return "action";
          if (current === "action") return "navigate";
          if (current === "navigate") return "setting";
          return null;
        });
        setActiveIndex(0);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open, ranked, activeIndex, onClose]);

  // Reset active index when query / filter changes.
  useEffect(() => {
    setActiveIndex(0);
  }, [query, kindFilter]);

  // Scroll active row into view.
  useEffect(() => {
    if (!listRef.current) return;
    const el = listRef.current.querySelector(
      `[data-cmd-idx="${activeIndex}"]`,
    ) as HTMLElement | null;
    el?.scrollIntoView({ block: "nearest" });
  }, [activeIndex]);

  if (!open) return null;

  // Build a flat-index map so each grouped row knows its index in the
  // ranked array for keyboard activation.
  const indexMap = new Map<string, number>();
  ranked.forEach((cmd, i) => indexMap.set(cmd.id, i));

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/60 pt-[12vh] backdrop-blur-sm"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        className="w-full max-w-[640px] overflow-hidden rounded-2xl border border-castle-line bg-castle-panel shadow-2xl"
        role="dialog"
        aria-label="Command palette"
      >
        {/* Search input */}
        <div className="flex items-center gap-3 border-b border-castle-line px-4 py-3">
          <Search className="h-4 w-4 text-castle-mute" />
          <input
            ref={inputRef}
            type="text"
            placeholder={
              kindFilter
                ? `Search ${KIND_LABEL[kindFilter].toLowerCase()}...`
                : "Type to search — Tab to filter, Enter to run, Esc to close"
            }
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            className="flex-1 bg-transparent text-base text-slate-100 placeholder:text-castle-mute focus:outline-none"
          />
          {kindFilter && (
            <button
              type="button"
              onClick={() => setKindFilter(null)}
              className="rounded-md border border-castle-line bg-white/[0.04] px-2 py-0.5 text-[11px] text-castle-mute hover:text-slate-200"
              title="Clear filter (Tab)"
            >
              filter: {KIND_LABEL[kindFilter]} ✕
            </button>
          )}
          <span className="rounded-md border border-castle-line bg-white/[0.04] px-2 py-0.5 text-[11px] text-castle-mute">
            Esc
          </span>
        </div>

        {/* Results list */}
        <div ref={listRef} className="max-h-[55vh] overflow-y-auto py-2">
          {grouped.length === 0 ? (
            <div className="px-4 py-8 text-center text-sm text-castle-mute">
              No commands match. Try different words, or press Tab to clear
              the filter.
            </div>
          ) : (
            grouped.map((group) => {
              const Icon = KIND_ICON[group.kind];
              return (
                <div key={group.kind} className="mb-2 last:mb-0">
                  <div className="flex items-center gap-2 px-4 py-1 text-[10px] font-black uppercase tracking-widest text-castle-mute">
                    <Icon className="h-3 w-3" />
                    {KIND_LABEL[group.kind]}
                  </div>
                  {group.items.map((cmd) => {
                    const idx = indexMap.get(cmd.id) ?? -1;
                    const isActive = idx === activeIndex;
                    return (
                      <button
                        key={cmd.id}
                        type="button"
                        data-cmd-idx={idx}
                        onMouseEnter={() => setActiveIndex(idx)}
                        onClick={() => {
                          onClose();
                          setTimeout(() => cmd.run(), 0);
                        }}
                        className={
                          "flex w-full items-center gap-3 px-4 py-2.5 text-left transition " +
                          (isActive
                            ? "bg-castle-allow/10 text-white"
                            : "text-slate-200 hover:bg-white/[0.03]")
                        }
                      >
                        <div className="min-w-0 flex-1">
                          <div className="truncate text-sm font-bold">
                            {cmd.label}
                          </div>
                          {cmd.subtitle && (
                            <div className="mt-0.5 truncate text-[11px] text-castle-mute">
                              {cmd.subtitle}
                            </div>
                          )}
                        </div>
                        {cmd.shortcut && (
                          <span className="rounded-md border border-castle-line bg-white/[0.04] px-2 py-0.5 font-mono text-[10px] text-castle-mute">
                            {cmd.shortcut}
                          </span>
                        )}
                        {isActive && (
                          <ArrowRight className="h-3.5 w-3.5 text-castle-allow" />
                        )}
                      </button>
                    );
                  })}
                </div>
              );
            })
          )}
        </div>

        {/* Footer hint strip */}
        <div className="flex items-center gap-3 border-t border-castle-line bg-black/30 px-4 py-2 text-[10px] text-castle-mute">
          <span className="flex items-center gap-1">
            <CommandIcon className="h-3 w-3" />
            <kbd>↑</kbd>/<kbd>↓</kbd> move
          </span>
          <span>
            <kbd>↵</kbd> run
          </span>
          <span>
            <kbd>Tab</kbd> filter
          </span>
          <span>
            <kbd>Esc</kbd> close
          </span>
          <span className="ml-auto">{ranked.length} commands</span>
        </div>
      </div>
    </div>
  );
}
