import { useEffect, useRef, useState } from "react";
import {
  loadDashboard,
  loadManagedProjects,
  loadTomlDocuments,
  saveConfigSetting,
  deleteConfigSetting,
  batchConfigSettings,
  saveTomlDocument,
  type ConfigBatchOperation,
  type DashboardConfigEntry,
  type DashboardManagedProject,
  type DashboardSnapshot,
  type DashboardTomlDocument,
  dashboardLiveCursor,
  startCursorWatcher,
} from "./dashboardApi";
import { parseEntryValue } from "./dashboardUtils";
import { getScope, isWebBuild, loadGateConnection, onScopeChange } from "./webmcpScope";

export function useDashboardData() {
  const [snapshot, setSnapshot] = useState<DashboardSnapshot | null>(null);
  const [projects, setProjects] = useState<DashboardManagedProject[]>([]);
  const [tomlDocuments, setTomlDocuments] = useState<DashboardTomlDocument[]>([]);
  const [selectedProjectRoot, setSelectedProjectRoot] = useState<string | undefined>(undefined);
  const [selectedSessionId, setSelectedSessionId] = useState<string | undefined>(undefined);
  const [selectedTomlPath, setSelectedTomlPath] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [draftValues, setDraftValues] = useState<Record<string, string>>({});
  const [tomlDrafts, setTomlDrafts] = useState<Record<string, string>>({});
  const [savingSetting, setSavingSetting] = useState<string | null>(null);
  const [savingTomlPath, setSavingTomlPath] = useState<string | null>(null);
  const [refreshToken, setRefreshToken] = useState(0);
  // Bumped when a project is imported so Effect A (the project list) re-fetches.
  // refresh() only re-fires the snapshot (Effect B), not the list — so without
  // this a freshly imported project never appeared until a full page reload.
  const [projectsNonce, setProjectsNonce] = useState(0);
  // Silent auto-refresh bookkeeping: is the current snapshot run a poll, and is a
  // snapshot fetch already in flight (so polls don't pile up subprocess fetches).
  const pollSilentRef = useRef(false);
  const inFlightRef = useRef(false);
  // Mirror of `snapshot` for effects with [] deps (#210: the slice-load
  // decision needs "is a full snapshot already on screen" without
  // re-subscribing the load effect to snapshot changes).
  const snapshotRef = useRef<DashboardSnapshot | null>(null);
  snapshotRef.current = snapshot;
  // Current project root mirrored into a ref so the cheap cursor-poll interval
  // (deps []) reads the live value without re-subscribing every render.
  const selectedProjectRootRef = useRef<string | undefined>(undefined);
  selectedProjectRootRef.current = selectedProjectRoot;
  // Bumped on every scope flip so BOTH the project-list (Effect A) and the
  // snapshot (Effect B) reload from the new source — loadManagedProjects /
  // loadDashboard branch on scope, but neither selectedProjectRoot nor
  // selectedSessionId changes when the scope toggles.
  const [scopeNonce, setScopeNonce] = useState(0);
  useEffect(
    () =>
      onScopeChange(() => {
        // Clear the selection so it re-seeds from the new scope's data (a local
        // fs path is meaningless to the gate and vice-versa); the load effects
        // below repopulate selectedProjectRoot/Session from the fresh snapshot.
        setSelectedProjectRoot(undefined);
        setSelectedSessionId(undefined);
        setScopeNonce((n) => n + 1);
      }),
    [],
  );

  // PERF (2026-05-26): split the load effect so refresh() (which bumps
  // refreshToken) only re-fires the SNAPSHOT fetch — not the TOML fetch.
  // Each native fetch spawns a fresh Python CLI subprocess (~2.6s cold-boot
  // each), so combining them in one refresh-bound effect doubles the wait
  // unnecessarily. Trust/security/degraded truth is unaffected: the snapshot
  // carries every live signal (config_entries, bash_policy, rbac, freezes,
  // degraded_state); TOML documents are operator-edited config that only
  // change on a project/session switch or an explicit save (whose path
  // updates local state directly).

  // ── Effect A: project list + TOML documents — on project/session change ──
  // Skips refresh-only re-runs. loadManagedProjects is pure Rust; the
  // TOML fetch is the expensive subprocess this effect protects from refresh.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const projectItems = await loadManagedProjects(selectedProjectRoot);
        if (cancelled) return;
        setProjects(projectItems);
        // Web scope: auto-bind the current (or first) project when nothing is
        // selected yet. dashboard_snapshot is per-bound-project, so without an
        // initial selection the snapshot is empty and the dashboard renders blank.
        // (Local scope seeds selectedProjectRoot from the snapshot's own project.)
        if (getScope() === "web" && projectItems.length > 0) {
          setSelectedProjectRoot(
            (cur) =>
              cur ??
              (projectItems.find((p) => p.current)?.project_root ?? projectItems[0]?.project_root),
          );
        }
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      }
      try {
        const docs = await loadTomlDocuments(
          selectedProjectRoot,
          selectedSessionId === "__all__" ? undefined : selectedSessionId,
        );
        if (cancelled) return;
        setTomlDocuments(docs);
        setTomlDrafts(Object.fromEntries(docs.map((d) => [d.path, d.content])));
        setSelectedTomlPath((current) =>
          current && docs.some((d) => d.path === current) ? current : docs[0]?.path ?? null,
        );
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      }
    })();
    return () => { cancelled = true; };
  }, [selectedProjectRoot, selectedSessionId, scopeNonce, projectsNonce]);

  // ── Effect B: snapshot — on project/session change AND on refresh ──
  // This is the live-state fetch the refresh button re-fires; one subprocess.
  useEffect(() => {
    let cancelled = false;
    // A poll-triggered run is SILENT: no spinner, no error toast, so the stream
    // and lanes update in place. Manual refresh / project switch still show load.
    const silent = pollSilentRef.current;
    pollSilentRef.current = false;
    inFlightRef.current = true;
    // #210 slice-loads: a silent (poll/push) refresh with a snapshot already
    // on screen requests ONLY the live sections and merges them — no full
    // python snapshot build, no full re-render of static sections. Manual
    // refresh / project switch / web scope still load full.
    const liveSlice = silent && !isWebBuild() && snapshotRef.current !== null;
    if (!silent) {
      setLoading(true);
      setError(null);
    }

    (async () => {
      try {
        // Fetch-timeout race (#201): a wedged python spawn (cold palace / DB
        // lock) must never permanently stick inFlightRef and freeze the surface.
        // If the load hangs past 15s, reject so the finally clears inFlightRef
        // and the last snapshot stays; the next cursor change retries.
        const data = await Promise.race([
          loadDashboard(
            selectedProjectRoot,
            selectedSessionId === "__all__" ? undefined : selectedSessionId,
            liveSlice,
          ),
          new Promise<DashboardSnapshot>((_, reject) =>
            setTimeout(() => reject(new Error("dashboard load timed out")), 15000),
          ),
        ]);
        if (cancelled) return;

        if (data.live_only) {
          // Merge the live slice into the last full snapshot; a lost previous
          // snapshot (shouldn't happen — liveSlice requires one) falls back
          // to keeping what we have rather than rendering a partial payload.
          setSnapshot((prev) =>
            prev
              ? {
                  ...prev,
                  plans: data.plans,
                  degraded_state: data.degraded_state,
                  // Gate liveness rides the LIVE slice: a gate that dies
                  // mid-session must go red on the next refresh, not linger
                  // green from the last full load.
                  gate_health: data.gate_health,
                  freezes: data.freezes,
                  execution: data.execution,
                  token_usage: data.token_usage,
                }
              : prev,
          );
          return;
        }
        setSnapshot(data);
        setSelectedProjectRoot((current) => current ?? data.project.project_root);
        setSelectedSessionId((current) => {
          const next = current && data.sessions.some((s) => s.session_id === current)
            ? current
            : data.selected_session_id ?? undefined;
          return current ?? next /* set-once storm fix: keep selection once set, never auto-thrash */;
        });
        setDraftValues({});
      } catch (err) {
        if (!cancelled && !silent) setError(err instanceof Error ? err.message : String(err));
      } finally {
        // 2026-05-03 polish: artificial 600ms minimum-loading floor
        // removed. Empire reported 6s+ refreshes on aidocs and 10s+ on
        // dentalapp; the flicker-prevention floor was adding dead
        // time when the underlying fetch was already heavy. The
        // spinner is its own honest signal — no need to fake duration.
        if (!cancelled) setLoading(false);
        inFlightRef.current = false;
      }
    })();

    return () => { cancelled = true; };
  }, [selectedProjectRoot, selectedSessionId, refreshToken, scopeNonce]);

  // Desktop PUSH (#204 clause c, dashboard-war): the Rust cursor-watcher
  // thread emits `aidocs://live-cursor` on change — true event push, so the
  // webview does ZERO periodic invokes while idle. The 2s cursor-poll below
  // stays only as the fallback when the watcher/listener isn't available
  // (web build, event-API failure); watcherPushRef flips it off.
  const watcherPushRef = useRef(false);
  useEffect(() => {
    if (isWebBuild()) return;
    let disposed = false;
    let unlisten: (() => void) | null = null;
    (async () => {
      try {
        const { listen } = await import("@tauri-apps/api/event");
        unlisten = await listen("aidocs://live-cursor", () => {
          if (inFlightRef.current) return;
          pollSilentRef.current = true;
          setRefreshToken((token) => token + 1);
        });
        if (disposed) {
          unlisten();
          return;
        }
        watcherPushRef.current = true;
      } catch {
        // No event API — the 2s cursor poll below keeps the surface live.
      }
    })();
    return () => {
      disposed = true;
      watcherPushRef.current = false;
      unlisten?.();
    };
  }, []);
  useEffect(() => {
    if (isWebBuild() || !selectedProjectRoot) return;
    // (Re)start the watcher for the selected project; a failure just leaves
    // the poll fallback active.
    startCursorWatcher(selectedProjectRoot).catch(() => {
      watcherPushRef.current = false;
    });
  }, [selectedProjectRoot]);

  // Change-detection refresh — FALLBACK poll (replaces the old blind 2s
  // full-snapshot poll that spawned cli-dashboard every tick — the process
  // storm + refresh stutter). Every 2s read a CHEAP sqlite-derived live
  // cursor (Rust, NO python spawn) and pull the full snapshot ONLY when it
  // changes; plus a slow safety refresh every ~30s so static sections (repo
  // summary, config) still catch up. Visible-only; skips while a fetch is in
  // flight; SKIPS the cursor read entirely while the watcher push is active.
  useEffect(() => {
    let lastCursor = "";
    let sinceFullMs = 0;
    let stopped = false;
    const tick = async () => {
      if (typeof document !== "undefined" && document.visibilityState !== "visible") return;
      if (inFlightRef.current) return;
      sinceFullMs += 2000;
      let changed = false;
      const root = selectedProjectRootRef.current;
      if (root && !watcherPushRef.current) {
        try {
          const res = await dashboardLiveCursor(root);
          const sig = res?.cursor ?? "";
          if (sig && lastCursor && sig !== lastCursor) changed = true;
          if (sig) lastCursor = sig;
        } catch {
          // Cursor unavailable — fall back to the slow safety refresh below.
        }
      }
      if (stopped) return;
      if (changed || sinceFullMs >= 30000) {
        sinceFullMs = 0;
        pollSilentRef.current = true;
        setRefreshToken((token) => token + 1);
      }
    };
    const id = setInterval(() => void tick(), 2000);
    return () => {
      stopped = true;
      clearInterval(id);
    };
  }, []);

  // Push (SSE): when the gate exposes an event stream, subscribe so the snapshot
  // refreshes the instant execution state changes — true push, not the 2s poll above.
  // Fail-soft: EventSource auto-reconnects natively, and the poll keeps the surface
  // live if the stream is unavailable, so this is safe even before the gate route lands.
  useEffect(() => {
    if (!isWebBuild() || typeof EventSource === "undefined") return;
    // EventSource can't send the Authorization header, so pass the gate token as a
    // query param (the gate reads ?access_token= when no bearer header is present).
    const _conn = loadGateConnection();
    const _url = _conn?.accessToken
      ? `/v1/events?access_token=${encodeURIComponent(_conn.accessToken)}`
      : "/v1/events";
    const es = new EventSource(_url);
    es.onmessage = () => {
      if (inFlightRef.current) return;
      pollSilentRef.current = true;
      setRefreshToken((token) => token + 1);
    };
    return () => es.close();
  }, []);

  async function saveConfigEntry(entry: DashboardConfigEntry, scope: string, value: string, reason?: string) {
    setSavingSetting(entry.path);
    setNotice(null);
    setError(null);
    try {
      let response;
      if (value === "__inherit__") {
        response = await deleteConfigSetting(entry.path, selectedProjectRoot, scope, selectedSessionId);
      } else {
        const parsedValue = parseEntryValue(entry, value);
        response = await saveConfigSetting(entry.path, parsedValue, selectedProjectRoot, scope, selectedSessionId, reason);
      }
      setSnapshot(response.snapshot);
      setDraftValues({});
      setNotice(response.message);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSavingSetting(null);
    }
  }

  async function saveConfigBatch(items: Array<{ entry: DashboardConfigEntry; value: string }>, scope: string) {
    if (!items.length) return;
    setSavingSetting("batch");
    setNotice(null);
    setError(null);
    try {
      const operations: ConfigBatchOperation[] = items.map(({ entry, value }) => {
        if (value === "__inherit__") {
          return { action: "delete" as const, setting_path: entry.path, scope, session_id: selectedSessionId };
        }
        return { action: "set" as const, setting_path: entry.path, value: parseEntryValue(entry, value), scope, session_id: selectedSessionId };
      });
      const response = await batchConfigSettings(operations, selectedProjectRoot);
      setSnapshot(response.snapshot);
      setDraftValues({});
      setNotice(response.message);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSavingSetting(null);
    }
  }


  async function saveBashCommandState(
    cmd: string,
    layer: "global" | "project" | "session",
    nextState: "allow" | "deny" | "bubble",
  ) {
    // Phase 5c (2026-05-02): bash policy 3-state toggle. Each
    // state-transition is a 2-row batch — set one path, delete the
    // other — so we never end up with both allow+deny live at once
    // (within-layer "deny overpowers allow" invariant).
    const allowPath = `bash.allow.${cmd}`;
    const denyPath = `bash.deny.${cmd}`;
    setSavingSetting(`${layer}:${nextState === "deny" ? denyPath : allowPath}`);
    setNotice(null);
    setError(null);
    try {
      const ops: ConfigBatchOperation[] = [];
      if (nextState === "allow") {
        ops.push({ action: "set", setting_path: allowPath, value: ["*"], scope: layer, session_id: selectedSessionId });
        ops.push({ action: "delete", setting_path: denyPath, scope: layer, session_id: selectedSessionId });
      } else if (nextState === "deny") {
        ops.push({ action: "set", setting_path: denyPath, value: ["*"], scope: layer, session_id: selectedSessionId });
        ops.push({ action: "delete", setting_path: allowPath, scope: layer, session_id: selectedSessionId });
      } else {
        // bubble — clear both
        ops.push({ action: "delete", setting_path: allowPath, scope: layer, session_id: selectedSessionId });
        ops.push({ action: "delete", setting_path: denyPath, scope: layer, session_id: selectedSessionId });
      }
      const response = await batchConfigSettings(ops, selectedProjectRoot);
      setSnapshot(response.snapshot);
      setNotice(`bash.${cmd} → ${nextState} at ${layer}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSavingSetting(null);
    }
  }


  async function saveJudgeOverrideFamily(
    family: string,
    ruleIds: string[],
    layer: "global" | "project" | "session",
  ) {
    // Backlog #19/#22: persist one family's judge-rule opt-out list.
    // The canonical row is security.judge_override.<family>; the
    // server emits judge_rule_disabled/enabled audit events per delta.
    const path = `security.judge_override.${family}`;
    setSavingSetting(`${layer}:${path}`);
    setNotice(null);
    setError(null);
    try {
      const ops: ConfigBatchOperation[] = [
        {
          action: "set",
          setting_path: path,
          value: ruleIds,
          scope: layer,
          session_id: selectedSessionId,
        },
      ];
      const response = await batchConfigSettings(ops, selectedProjectRoot);
      setSnapshot(response.snapshot);
      setNotice(
        `judge overrides (${family}) → ${ruleIds.length} rule${ruleIds.length === 1 ? "" : "s"} disabled at ${layer}`,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSavingSetting(null);
    }
  }

  async function saveTomlPath(path: string, content: string) {
    setSavingTomlPath(path);
    setNotice(null);
    setError(null);
    try {
      const response = await saveTomlDocument(
        path,
        content,
        selectedSessionId,
        selectedProjectRoot,
      );
      setTomlDocuments(response.documents);
      setTomlDrafts(
        Object.fromEntries(response.documents.map((document) => [document.path, document.content])),
      );
      setNotice(response.message);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSavingTomlPath(null);
    }
  }

  function handleProjectChange(projectRoot: string) {
    // Phoenix 2026-05-07: normalize path separators + case before
    // compare — option.value may carry backslashes on Windows while
    // snapshot's stored project_root could be slashes (or vice versa).
    // Naive === early-returns on a real switch. Force-refresh after
    // so the effect always fires.
    const norm = (p: string | undefined | null) =>
      (p ?? "").replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
    const currentProjectRoot = selectedProjectRoot ?? snapshot?.project.project_root;
    if (!projectRoot || norm(projectRoot) === norm(currentProjectRoot)) {
      return;
    }
    setSelectedProjectRoot(projectRoot);
    setSelectedSessionId(undefined);
    setSelectedTomlPath(null);
    setNotice(null);
    setError(null);
    setRefreshToken((value) => value + 1);
  }

  function setDraftValue(path: string, value: string) {
    setDraftValues((current) => ({ ...current, [path]: value }));
  }

  function setTomlDraft(path: string, value: string) {
    setTomlDrafts((current) => ({ ...current, [path]: value }));
  }

  function refresh() {
    setRefreshToken((value) => value + 1);
  }

  function reloadProjects() {
    // Re-run Effect A (the project list) so a freshly imported project shows
    // without a full reload. Effect A's web-scope auto-select then binds it
    // when nothing is selected yet (the empty "Load a project" state), which
    // cascades into the snapshot fetch. refresh() can't do this — it only
    // bumps refreshToken, which Effect A intentionally ignores (perf).
    setProjectsNonce((value) => value + 1);
  }

  return {
    snapshot,
    projects,
    tomlDocuments,
    selectedProjectRoot,
    selectedSessionId,
    selectedTomlPath,
    loading,
    error,
    notice,
    draftValues,
    tomlDrafts,
    savingSetting,
    savingTomlPath,
    setSelectedSessionId,
    setSelectedTomlPath,
    setError,
    setNotice,
    setDraftValue,
    setTomlDraft,
    handleProjectChange,
    saveConfigEntry,
    saveConfigBatch,
    saveBashCommandState,
    saveJudgeOverrideFamily,
    saveTomlPath,
    refresh,
    reloadProjects,
  };
}
