import { useEffect, useState } from "react";
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
} from "./dashboardApi";
import { parseEntryValue } from "./dashboardUtils";
import { onScopeChange } from "./webmcpScope";

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
  }, [selectedProjectRoot, selectedSessionId, scopeNonce]);

  // ── Effect B: snapshot — on project/session change AND on refresh ──
  // This is the live-state fetch the refresh button re-fires; one subprocess.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    (async () => {
      try {
        const data = await loadDashboard(
          selectedProjectRoot,
          selectedSessionId === "__all__" ? undefined : selectedSessionId,
        );
        if (cancelled) return;

        setSnapshot(data);
        setSelectedProjectRoot((current) => current ?? data.project.project_root);
        setSelectedSessionId((current) => {
          const next = current && data.sessions.some((s) => s.session_id === current)
            ? current
            : data.selected_session_id ?? undefined;
          return current === next ? current : next;
        });
        setDraftValues({});
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      } finally {
        // 2026-05-03 polish: artificial 600ms minimum-loading floor
        // removed. King reported 6s+ refreshes on aidocs and 10s+ on
        // dentalapp; the flicker-prevention floor was adding dead
        // time when the underlying fetch was already heavy. The
        // spinner is its own honest signal — no need to fake duration.
        if (!cancelled) setLoading(false);
      }
    })();

    return () => { cancelled = true; };
  }, [selectedProjectRoot, selectedSessionId, refreshToken, scopeNonce]);

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
    saveTomlPath,
    refresh,
  };
}
