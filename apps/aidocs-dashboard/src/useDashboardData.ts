import { useEffect, useState } from "react";
import {
  loadDashboard,
  loadManagedProjects,
  loadTomlDocuments,
  saveConfigSetting,
  saveTomlDocument,
  type DashboardConfigEntry,
  type DashboardManagedProject,
  type DashboardSnapshot,
  type DashboardTomlDocument,
} from "./dashboardApi";
import { asText, parseEntryValue } from "./dashboardUtils";

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

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    Promise.all([
      loadManagedProjects(selectedProjectRoot),
      loadDashboard(selectedProjectRoot, selectedSessionId === "__all__" ? undefined : selectedSessionId),
      loadTomlDocuments(selectedProjectRoot, selectedSessionId === "__all__" ? undefined : selectedSessionId),
    ])
      .then(([projectItems, data, documents]) => {
        if (cancelled) {
          return;
        }

        setProjects(projectItems);
        setSnapshot(data);
        setTomlDocuments(documents);
        setSelectedProjectRoot((current) => current ?? data.project.project_root);
        setSelectedSessionId((current) => {
          const next = current && data.sessions.some((session) => session.session_id === current)
            ? current
            : data.selected_session_id ?? undefined;
          return current === next ? current : next;
        });
        setDraftValues(
          Object.fromEntries(
            data.config.entries.map((entry) => [entry.path, asText(entry.current_value)]),
          ),
        );
        setTomlDrafts(
          Object.fromEntries(documents.map((document) => [document.path, document.content])),
        );
        setSelectedTomlPath((current) =>
          current && documents.some((document) => document.path === current)
            ? current
            : documents[0]?.path ?? null,
        );
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : String(err));
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [refreshToken, selectedProjectRoot, selectedSessionId]);

  async function saveConfigEntry(entry: DashboardConfigEntry, scope?: string) {
    const rawValue = draftValues[entry.path] ?? asText(entry.current_value);
    setSavingSetting(entry.path);
    setNotice(null);
    setError(null);
    try {
      const parsedValue = parseEntryValue(entry, rawValue);
      const response = await saveConfigSetting(
        entry.path,
        parsedValue,
        selectedProjectRoot,
        scope,
        selectedSessionId,
      );
      setSnapshot(response.snapshot);
      setDraftValues(
        Object.fromEntries(
          response.snapshot.config.entries.map((item) => [item.path, asText(item.current_value)]),
        ),
      );
      setNotice(response.message);
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
    const currentProjectRoot = selectedProjectRoot ?? snapshot?.project.project_root;
    if (!projectRoot || projectRoot === currentProjectRoot) {
      return;
    }
    setSelectedProjectRoot(projectRoot);
    setSelectedSessionId(undefined);
    setSelectedTomlPath(null);
    setNotice(null);
    setError(null);
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
    saveTomlPath,
    refresh,
  };
}
