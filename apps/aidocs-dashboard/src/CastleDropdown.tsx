/**
 * CastleDropdown — Phase 6f.1 (2026-05-02).
 *
 * Tailwind-native replacement for the old HeaderDropdown. Renders
 * with the castle-shell visual language: rounded-xl border, dark
 * background, tiny uppercase label above the selected value, chevron
 * indicator on the right. Match-by-default with the project/session
 * pills the shell renders when no selector is provided.
 *
 * API mirrors HeaderDropdown so swap-in is one-line per call site.
 */
import { useEffect, useRef, useState } from "react";
import { ChevronDown } from "lucide-react";
import type { DropdownOption } from "./dashboardUtils";

export type CastleDropdownProps = {
  label: string;
  value: string;
  options: DropdownOption[];
  open: boolean;
  onToggle: () => void;
  onSelect: (value: string) => void;
  /** When true, the value is rendered with the castle-allow accent
   * color (matches the session-pill convention from the shell). */
  accent?: boolean;
  /** Min width override; default 230px to match the shell pills. */
  minWidth?: number;
};

export function CastleDropdown({
  label,
  value,
  options,
  open,
  onToggle,
  onSelect,
  accent = false,
  minWidth = 230,
}: CastleDropdownProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const [highlight, setHighlight] = useState<number>(0);

  const selected =
    options.find((option) => option.value === value) ?? options[0] ?? null;

  // Close on outside-click. Safe to use mousedown again now that
  // option buttons fire onSelect via onMouseDown — the option's
  // mousedown runs in the SAME native event as this document
  // listener, and the option handler's preventDefault + the
  // inside-rootRef contains() check together ensure the dropdown
  // doesn't close from under the option click.
  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (!rootRef.current) return;
      if (rootRef.current.contains(e.target as Node)) return;
      onToggle();
    };
    const t = setTimeout(
      () => document.addEventListener("mousedown", handler),
      0,
    );
    return () => {
      clearTimeout(t);
      document.removeEventListener("mousedown", handler);
    };
  }, [open, onToggle]);

  // Keyboard navigation when open.
  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onToggle();
        return;
      }
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setHighlight((i) => Math.min(options.length - 1, i + 1));
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setHighlight((i) => Math.max(0, i - 1));
      } else if (e.key === "Enter") {
        e.preventDefault();
        const target = options[highlight];
        if (target) onSelect(target.value);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open, options, highlight, onSelect, onToggle]);

  // Reset highlight when opening.
  useEffect(() => {
    if (open) {
      const idx = options.findIndex((o) => o.value === value);
      setHighlight(idx >= 0 ? idx : 0);
    }
  }, [open, options, value]);

  return (
    <div
      ref={rootRef}
      className="relative"
      style={{ minWidth: `${minWidth}px` }}
    >
      <button
        type="button"
        onClick={onToggle}
        aria-haspopup="listbox"
        aria-expanded={open}
        className={
          "flex w-full items-center gap-2 rounded-xl border bg-black/20 px-3 py-2 text-left transition " +
          (open
            ? "border-castle-allow/35 bg-black/30"
            : "border-castle-line hover:bg-black/30")
        }
      >
        <div className="min-w-0 flex-1">
          <div className="text-[9px] font-bold uppercase tracking-widest text-castle-mute">
            {label}
          </div>
          <div
            className={
              "truncate text-sm " +
              (accent ? "text-castle-allow" : "text-slate-200")
            }
          >
            {selected?.label ?? "—"}
          </div>
        </div>
        <ChevronDown
          className={
            "h-3.5 w-3.5 shrink-0 text-castle-mute transition-transform " +
            (open ? "rotate-180 text-castle-allow" : "")
          }
        />
      </button>

      {open && (
        <div
          className="absolute left-0 right-0 top-full z-40 mt-1 max-h-[60vh] overflow-y-auto rounded-xl border border-castle-line bg-castle-panel shadow-2xl"
          role="listbox"
          aria-label={label}
        >
          {options.length === 0 ? (
            <div className="px-3 py-4 text-center text-xs text-castle-mute">
              No options.
            </div>
          ) : (
            options.map((option, idx) => {
              const isSelected = option.value === value;
              const isHighlighted = idx === highlight;
              return (
                <button
                  key={option.value}
                  type="button"
                  onMouseDown={(e) => {
                    // Phoenix 2026-05-07: WebView2 (Tauri 2) does
                    // not reliably dispatch React's synthetic onClick
                    // for buttons inside an absolute-positioned
                    // popover — clicks were silently lost. mousedown
                    // is the reliable channel. preventDefault stops
                    // the implicit blur/focus-shuffle that would
                    // otherwise re-fire focus events.
                    e.preventDefault();
                    onSelect(option.value);
                  }}
                  onMouseEnter={() => setHighlight(idx)}
                  className={
                    "flex w-full flex-col items-start px-3 py-2 text-left transition " +
                    (isHighlighted
                      ? "bg-castle-allow/10"
                      : "hover:bg-white/[0.03]") +
                    (isSelected ? " border-l-2 border-castle-allow" : " border-l-2 border-transparent")
                  }
                >
                  <span
                    className={
                      "truncate text-sm " +
                      (isSelected
                        ? accent
                          ? "text-castle-allow font-bold"
                          : "text-white font-bold"
                        : "text-slate-200")
                    }
                  >
                    {option.label}
                  </span>
                  {option.subtitle && (
                    <span className="mt-0.5 truncate text-[11px] text-castle-mute">
                      {option.subtitle}
                    </span>
                  )}
                </button>
              );
            })
          )}
        </div>
      )}
    </div>
  );
}
