/**
 * BashPolicyPanel — Phase 5c (2026-05-02).
 *
 * Per-command 3-state allow/deny/bubble grid for the bash policy
 * (the rules `evaluate_bash_policy` consults when ai_run dispatches
 * a shell command). Runs alongside SettingsPage; consumes
 * snapshot.config.bash_policy populated by
 * runtime_presentation_service.dashboard_bash_policy.
 *
 * UX rules (Empire's decree):
 *   - Each row = one command (e.g. "python", "git", "curl").
 *   - Each layer column = factory / global / project / session.
 *   - Each cell is a 3-state toggle: green (allow) / red (deny) /
 *     gray (bubble = inherit from lower-priority layer).
 *   - Within a layer, deny overpowers allow (you can't have both
 *     for the same command in the same layer).
 *   - Cross-layer cascade: lower layer's explicit allow/deny shadows
 *     upper layer's. Bubble at every layer = "fall through to
 *     bash.default" (allow or block).
 *   - The "effective" column shows the resolved state for that
 *     command after the cascade.
 *   - Factory column is read-only (you can't write the factory layer
 *     from the dashboard — it's code-defined).
 */
import { useMemo, useState } from "react";
import { configEditingAvailable } from "./entitlements";
import type {
  BashCommandRow,
  BashCommandTriState,
  BashPolicySnapshot,
} from "./dashboardApi";

type WritableLayer = "global" | "project" | "session";

export type BashPolicyPanelProps = {
  policy: BashPolicySnapshot | null | undefined;
  /** Active scope for writes — driven by SettingsPage's tab. */
  activeLayer: WritableLayer;
  /** Whether project/session scopes are reachable. */
  hasProject: boolean;
  hasSession: boolean;
  /** Save a single bash.allow.<cmd> or bash.deny.<cmd> at scope. */
  onSetCommandState: (
    cmd: string,
    layer: WritableLayer,
    nextState: BashCommandTriState,
  ) => void;
  /** Inflight saves keyed by `${layer}:bash.allow.${cmd}` */
  saving?: string | null;
};

// Row shape for optimistically-added commands the snapshot doesn't
// know yet (#156): all-bubble until the server round-trip lands.
const FALLBACK_ROW: BashCommandRow = {
  factory: "bubble",
  global: "bubble",
  project: "bubble",
  session: "bubble",
  effective: "bubble",
  patterns: null,
};

const STATE_LABEL: Record<BashCommandTriState, string> = {
  allow: "✓",
  deny: "✕",
  bubble: "·",
};

const STATE_TITLE: Record<BashCommandTriState, string> = {
  allow: "Allow — command runs at this layer",
  deny: "Deny — command refused at this layer",
  bubble: "Bubble — inherit from lower layer (no explicit setting)",
};

function nextStateForward(current: BashCommandTriState): BashCommandTriState {
  // Cycle: bubble → allow → deny → bubble
  if (current === "bubble") return "allow";
  if (current === "allow") return "deny";
  return "bubble";
}

export function BashPolicyPanel({
  policy,
  activeLayer,
  hasProject,
  hasSession,
  onSetCommandState,
  saving,
}: BashPolicyPanelProps) {
  const [collapsed, setCollapsed] = useState(false);
  const [searchTerm, setSearchTerm] = useState("");
  const [filter, setFilter] = useState<
    "all" | "allow" | "deny" | "bubble" | "modified"
  >("all");
  // Optimistic overlay: { "cmd:layer": next-state } applied
  // immediately on click so the cell flips before the round-trip
  // returns. Cleared when the incoming policy reflects the change.
  const [optimistic, setOptimistic] = useState<
    Record<string, BashCommandTriState>
  >({});
  // Backlog #156 — add-command UI. Pending adds render optimistically
  // until the snapshot's command universe includes them.
  const [addName, setAddName] = useState("");
  const [addError, setAddError] = useState<string | null>(null);
  const [pendingAdds, setPendingAdds] = useState<string[]>([]);

  // Reconcile optimistic overlay on each policy update — any key
  // whose server value matches the optimistic one is cleared.
  useMemo(() => {
    if (!policy) return;
    setOptimistic((prev) => {
      let changed = false;
      const next: Record<string, BashCommandTriState> = {};
      for (const [k, v] of Object.entries(prev)) {
        const [cmd, layer] = k.split(":") as [string, WritableLayer];
        const serverState = policy.commands[cmd]?.[layer];
        if (serverState === v) {
          changed = true; // server caught up — drop the override
        } else {
          next[k] = v;
        }
      }
      return changed ? next : prev;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [policy]);

  function readState(cmd: string, layer: WritableLayer): BashCommandTriState {
    const key = `${cmd}:${layer}`;
    if (optimistic[key]) return optimistic[key];
    return policy?.commands[cmd]?.[layer] ?? "bubble";
  }

  // Config writes aren't wired over the web gate yet → all layers read-only in web,
  // so the cells render as non-editable (no fake clickable boxes / optimistic flips).
  const canEditBash = configEditingAvailable();
  const layerEditable: Record<WritableLayer, boolean> = {
    global: canEditBash,
    project: canEditBash && hasProject,
    session: canEditBash && hasSession,
  };

  const allCommands = useMemo(() => {
    if (!policy) return [];
    const known = new Set(Object.keys(policy.commands));
    // Reconcile pending optimistic adds: once the snapshot's universe
    // includes them they stop being "pending".
    const stillPending = pendingAdds.filter((cmd) => !known.has(cmd));
    return [...known, ...stillPending].sort();
  }, [policy, pendingAdds]);

  // Command-name validation (#156): plain binary names only — no
  // spaces, no shell metacharacters, no path separators.
  const ADD_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;

  function handleAddCommand() {
    const cmd = addName.trim().toLowerCase();
    if (!cmd) return;
    if (!ADD_NAME_RE.test(cmd)) {
      setAddError(
        "Command names: letters, digits, '.', '-', '_' only (no spaces or shell metacharacters).",
      );
      return;
    }
    if (allCommands.includes(cmd)) {
      setAddError(`\`${cmd}\` is already in the policy grid.`);
      return;
    }
    setAddError(null);
    setAddName("");
    // Default initial state = allow at the active layer: the operator's
    // intent when adding a command is to grant it (an all-bubble row
    // would not even persist). They can cycle to deny/bubble after.
    setPendingAdds((prev) => [...prev, cmd]);
    setOptimistic((prev) => ({ ...prev, [`${cmd}:${activeLayer}`]: "allow" }));
    onSetCommandState(cmd, activeLayer, "allow");
  }

  const counts = useMemo(() => {
    let allowEff = 0;
    let denyEff = 0;
    let bubbleEff = 0;
    let modifiedAtActive = 0;
    if (!policy) return { allowEff, denyEff, bubbleEff, modifiedAtActive };
    for (const cmd of allCommands) {
      const row = policy.commands[cmd] ?? FALLBACK_ROW;
      if (row.effective === "allow") allowEff += 1;
      else if (row.effective === "deny") denyEff += 1;
      else bubbleEff += 1;
      if (row[activeLayer] !== "bubble") modifiedAtActive += 1;
    }
    return { allowEff, denyEff, bubbleEff, modifiedAtActive };
  }, [allCommands, policy, activeLayer]);

  const visibleCommands = useMemo(() => {
    if (!policy) return [];
    return allCommands.filter((cmd) => {
      if (searchTerm.trim()) {
        if (!cmd.toLowerCase().includes(searchTerm.trim().toLowerCase()))
          return false;
      }
      const row = policy.commands[cmd] ?? FALLBACK_ROW;
      if (filter === "allow" && row.effective !== "allow") return false;
      if (filter === "deny" && row.effective !== "deny") return false;
      if (filter === "bubble" && row.effective !== "bubble") return false;
      if (filter === "modified" && row[activeLayer] === "bubble") return false;
      return true;
    });
  }, [allCommands, policy, searchTerm, filter, activeLayer]);

  if (!policy) {
    return null;
  }

  function handleCellClick(cmd: string, layer: WritableLayer) {
    if (!layerEditable[layer]) return;
    if (!policy) return;
    const current = readState(cmd, layer);
    const next = nextStateForward(current);
    // Optimistic flip — UI updates instantly; server reconciles
    // when the snapshot returns.
    setOptimistic((prev) => ({ ...prev, [`${cmd}:${layer}`]: next }));
    onSetCommandState(cmd, layer, next);
  }

  return (
    <div className={"bash-policy-panel" + (collapsed ? " is-collapsed" : "")}>
      <header
        className="bash-policy-header"
        onClick={() => setCollapsed((c) => !c)}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") setCollapsed((c) => !c);
        }}
      >
        <span className="bash-policy-toggle" aria-hidden="true">
          {collapsed ? "▸" : "▾"}
        </span>
        <strong>Shell policy</strong>
        <span className="bash-policy-summary">
          <span className="bash-policy-count bash-policy-count-allow">
            {counts.allowEff} allow
          </span>
          <span className="bash-policy-count bash-policy-count-deny">
            {counts.denyEff} deny
          </span>
          <span className="bash-policy-count bash-policy-count-bubble">
            {counts.bubbleEff} bubble
          </span>
          <span className="bash-policy-count bash-policy-count-mod">
            {counts.modifiedAtActive} set at {activeLayer}
          </span>
        </span>
        <span className="bash-policy-default">
          fallback: <code>{policy.default}</code>
        </span>
      </header>

      {!collapsed && (
        <>
          <div className="bash-policy-controls">
            <input
              type="search"
              className="settings-search"
              placeholder="Filter commands — name match"
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
            />
            <div className="settings-filter-chips">
              {(["all", "allow", "deny", "bubble", "modified"] as const).map(
                (f) => (
                  <button
                    key={f}
                    type="button"
                    className={"filter-chip" + (filter === f ? " is-active" : "")}
                    onClick={() => setFilter(f)}
                  >
                    {f}
                  </button>
                ),
              )}
            </div>

            {/* Backlog #156 — add a command family to the policy grid.
                Creates bash.allow.<cmd>=["*"] at the active layer; the
                operator cycles the cell afterwards if deny was meant. */}
            {layerEditable[activeLayer] ? (
              <div className="bash-policy-add">
                <input
                  type="text"
                  className="settings-search bash-policy-add-input"
                  placeholder={`Add command at ${activeLayer} (e.g. psql)`}
                  value={addName}
                  onChange={(e) => {
                    setAddName(e.target.value);
                    if (addError) setAddError(null);
                  }}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") handleAddCommand();
                  }}
                />
                <button
                  type="button"
                  className="filter-chip bash-policy-add-btn"
                  disabled={!addName.trim()}
                  onClick={handleAddCommand}
                  title={`Add this command as allow at the ${activeLayer} layer`}
                >
                  + add
                </button>
                {addError ? (
                  <span className="bash-policy-add-error">{addError}</span>
                ) : null}
              </div>
            ) : null}

          </div>

          <div className="bash-policy-grid-wrap">
            <table className="bash-policy-grid">
              <thead>
                <tr>
                  <th className="bash-cmd-col">command</th>
                  <th className="bash-layer-col">factory</th>
                  <th className="bash-layer-col">global</th>
                  <th className="bash-layer-col">project</th>
                  <th className="bash-layer-col">session</th>
                  <th className="bash-effective-col">effective</th>
                  <th className="bash-clear-col" aria-label="clear at active layer" />
                </tr>
              </thead>
              <tbody>
                {visibleCommands.length === 0 ? (
                  <tr>
                    <td colSpan={7} className="bash-empty">
                      No commands match the current filters.
                    </td>
                  </tr>
                ) : (
                  visibleCommands.map((cmd) => {
                    const row = policy.commands[cmd] ?? FALLBACK_ROW;
                    const inflight =
                      saving === `${activeLayer}:bash.allow.${cmd}` ||
                      saving === `${activeLayer}:bash.deny.${cmd}`;
                    return (
                      <tr key={cmd} className={inflight ? "is-saving" : ""}>
                        <th scope="row" className="bash-cmd-name">
                          <code>{cmd}</code>
                          {row.patterns && row.patterns.length > 0 &&
                          row.patterns[0] !== "*" ? (
                            <span className="bash-pattern-list">
                              {row.patterns.join(" · ")}
                            </span>
                          ) : null}
                        </th>
                        {(
                          ["factory", "global", "project", "session"] as const
                        ).map((layer) => {
                          const state =
                            layer === "factory"
                              ? row[layer]
                              : readState(cmd, layer as WritableLayer);
                          const editable =
                            layer !== "factory" &&
                            layerEditable[layer as WritableLayer] &&
                            layer === activeLayer;
                          const cellClass = [
                            "bash-cell",
                            `bash-cell-${state}`,
                            editable
                              ? "bash-cell-editable"
                              : "bash-cell-readonly",
                            layer === activeLayer
                              ? "bash-cell-active-layer"
                              : null,
                          ]
                            .filter(Boolean)
                            .join(" ");
                          return (
                            <td
                              key={layer}
                              className={cellClass}
                              onClick={() => {
                                if (editable)
                                  handleCellClick(cmd, layer as WritableLayer);
                              }}
                              role={editable ? "button" : undefined}
                              tabIndex={editable ? 0 : undefined}
                              onKeyDown={(e) => {
                                if (
                                  editable &&
                                  (e.key === "Enter" || e.key === " ")
                                ) {
                                  handleCellClick(cmd, layer as WritableLayer);
                                }
                              }}
                              title={
                                STATE_TITLE[state] +
                                (editable
                                  ? "\n(click to cycle bubble → allow → deny)"
                                  : layer === "factory"
                                  ? "\n(factory layer is read-only)"
                                  : `\n(switch the page tab to ${layer} to edit this column)`)
                              }
                            >
                              {STATE_LABEL[state]}
                            </td>
                          );
                        })}
                        <td
                          className={`bash-effective-cell bash-cell-${row.effective}`}
                          title={STATE_TITLE[row.effective]}
                        >
                          {STATE_LABEL[row.effective]}
                          <span className="bash-effective-tag">
                            {row.effective}
                          </span>
                        </td>
                        {/* #156 — per-row remove: clears the active
                            layer's setting back to bubble (the factory
                            column is never removable). */}
                        <td className="bash-clear-cell">
                          {layerEditable[activeLayer] &&
                          readState(cmd, activeLayer) !== "bubble" ? (
                            <button
                              type="button"
                              className="bash-clear-btn"
                              title={`Clear \`${cmd}\` at ${activeLayer} (back to bubble/inherit)`}
                              onClick={() => {
                                setOptimistic((prev) => ({
                                  ...prev,
                                  [`${cmd}:${activeLayer}`]: "bubble",
                                }));
                                onSetCommandState(cmd, activeLayer, "bubble");
                              }}
                            >
                              ×
                            </button>
                          ) : null}
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
