import { useEffect, useState } from "react";
import {
  gateMsgDelete,
  gateMsgList,
  gateMsgUpsert,
  vocabDeleteGroup,
  vocabGetGrouped,
  vocabListKinds,
  vocabListLangs,
  vocabUpsertGroup,
  type GateMsgRow,
  type VocabGroup,
} from "./dashboardApi";

// Phase 4d — schema-aware editor for the empire intent-tokens store.
// Replaces the file-shaped TomlConfigsPage path for intent_tokens/ and
// gate_messages/. Rows in sqlite, not files. Generic form per kind;
// per-kind specialized fields (requires_scope toggle for intent_phrase,
// tools-recommendation textarea for domain_hint, target picker for
// memory_route) land as a follow-up — for now attrs are a JSON
// textarea so power-operators can edit them directly.

// Kinds the dashboard editor exposes. Grant-detector internals
// (approve_verb, deny_verb, scopeless_*, person sets, negation,
// tool_alias, domain_alias) are NOT in this list — they're managed
// by intent_grant_detector's _EN_FALLBACK literal + migrate, not
// hand-edited.
const EDITABLE_KINDS: ReadonlyArray<{ kind: string; label: string; help: string }> = [
  { kind: "action_token",       label: "Action tokens",        help: "Verbs that classify user prompts (edit, fix, git_commit, …)." },
  { kind: "intent_guard",       label: "Intent guard",         help: "write / bash / destructive verb categories enforced by the guard." },
  { kind: "intent_phrase",      label: "Intent phrases",       help: "Closed-vocabulary phrases that fire plan-mode entry/exit." },
  { kind: "plan_vague_pattern", label: "Plan vague patterns",  help: "Phrases that flag a vague plan step (rejected by validator)." },
  { kind: "skill_trigger",      label: "Skill triggers",       help: "Per-skill intent/workflow token lists." },
  { kind: "domain_hint",        label: "Domain hints",         help: "Domain vocabulary → recommended tools." },
  { kind: "memory_route",       label: "Memory routes",        help: "Tokens that route memory captures to a target file." },
  { kind: "tool_discovery",     label: "Tool discovery",       help: "Keyword categories that surface a list of recommended tools." },
];

const NEW_GROUP_SENTINEL = "__new__";

function attrsToText(attrs: Record<string, unknown>): string {
  try {
    return JSON.stringify(attrs ?? {}, null, 2);
  } catch {
    return "{}";
  }
}

function parseAttrs(raw: string): Record<string, unknown> | null {
  const text = raw.trim();
  if (!text) return {};
  try {
    const parsed = JSON.parse(text);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

export function VocabPage() {
  const [kinds, setKinds] = useState<string[]>([]);
  const [langs, setLangs] = useState<string[]>([]);
  const [selectedKind, setSelectedKind] = useState<string>("action_token");
  const [selectedLang, setSelectedLang] = useState<string>("en");
  const [groups, setGroups] = useState<Record<string, VocabGroup>>({});
  const [selectedGroupKey, setSelectedGroupKey] = useState<string | null>(null);
  const [draftParentKey, setDraftParentKey] = useState("");
  const [draftParentMode, setDraftParentMode] = useState("");
  const [draftTokens, setDraftTokens] = useState("");
  const [draftAttrs, setDraftAttrs] = useState("{}");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    void (async () => {
      try {
        const [k, l] = await Promise.all([vocabListKinds(), vocabListLangs()]);
        setKinds(k);
        setLangs(l.length > 0 ? l : ["en"]);
      } catch (e) {
        setMessage(`Failed to list kinds/langs: ${String(e)}`);
      }
    })();
  }, []);

  useEffect(() => {
    void (async () => {
      setMessage(null);
      try {
        const g = await vocabGetGrouped(selectedKind, selectedLang);
        setGroups(g);
        setSelectedGroupKey(null);
      } catch (e) {
        setMessage(`Failed to load groups: ${String(e)}`);
      }
    })();
  }, [selectedKind, selectedLang]);

  function startEditing(groupKey: string) {
    setSelectedGroupKey(groupKey);
    const g = groups[groupKey];
    if (!g) return;
    setDraftParentKey(g.parent_key);
    setDraftParentMode(g.parent_mode);
    setDraftTokens(g.tokens.join("\n"));
    setDraftAttrs(attrsToText(g.attrs));
  }

  function startNewGroup() {
    setSelectedGroupKey(NEW_GROUP_SENTINEL);
    setDraftParentKey("");
    setDraftParentMode("");
    setDraftTokens("");
    setDraftAttrs("{}");
  }

  async function handleSave() {
    setMessage(null);
    if (!draftParentKey.trim()) {
      setMessage("parent_key is required.");
      return;
    }
    const attrs = parseAttrs(draftAttrs);
    if (attrs === null) {
      setMessage("attrs must be a JSON object.");
      return;
    }
    const tokens = draftTokens
      .split(/\r?\n/)
      .map((t) => t.trim())
      .filter((t) => t.length > 0);
    if (tokens.length === 0) {
      setMessage("Add at least one token (one per line).");
      return;
    }
    setBusy(true);
    try {
      const r = await vocabUpsertGroup(
        selectedKind,
        selectedLang,
        draftParentKey.trim(),
        tokens,
        attrs,
        draftParentMode.trim() || undefined,
      );
      setMessage(`Saved: deleted ${r.deleted}, inserted ${r.inserted}.`);
      const g = await vocabGetGrouped(selectedKind, selectedLang);
      setGroups(g);
    } catch (e) {
      setMessage(`Save failed: ${String(e)}`);
    } finally {
      setBusy(false);
    }
  }

  async function handleDelete() {
    if (!selectedGroupKey || selectedGroupKey === NEW_GROUP_SENTINEL) return;
    if (!confirm(`Delete entire group "${draftParentKey}"?`)) return;
    setBusy(true);
    setMessage(null);
    try {
      const r = await vocabDeleteGroup(
        selectedKind,
        selectedLang,
        draftParentKey.trim(),
        draftParentMode.trim() || undefined,
      );
      setMessage(`Deleted ${r.deleted} rows.`);
      const g = await vocabGetGrouped(selectedKind, selectedLang);
      setGroups(g);
      setSelectedGroupKey(null);
    } catch (e) {
      setMessage(`Delete failed: ${String(e)}`);
    } finally {
      setBusy(false);
    }
  }

  const kindMeta = EDITABLE_KINDS.find((k) => k.kind === selectedKind);
  const groupKeys = Object.keys(groups).sort();

  return (
    <section className="page page-config">
      <div className="page-fixed-header">
        <div className="config-tabs">
          {EDITABLE_KINDS.filter((k) => kinds.length === 0 || kinds.includes(k.kind)).map((k) => (
            <button
              key={k.kind}
              type="button"
              className={selectedKind === k.kind ? "config-tab is-active" : "config-tab"}
              onClick={() => setSelectedKind(k.kind)}
            >
              {k.label}
            </button>
          ))}
        </div>
        <div style={{ display: "flex", gap: 12, alignItems: "center", padding: "8px 0" }}>
          <label>
            Language:&nbsp;
            <select value={selectedLang} onChange={(e) => setSelectedLang(e.target.value)}>
              {langs.map((l) => (
                <option key={l} value={l}>
                  {l}
                </option>
              ))}
            </select>
          </label>
          <button type="button" className="action-button" onClick={startNewGroup}>
            + New group
          </button>
          {kindMeta && <span style={{ color: "var(--text-faint)" }}>{kindMeta.help}</span>}
        </div>
      </div>

      <div className="page-fill-region">
        <div className="toml-layout settings-documents-layout">
          <div className="flat-table">
            <div className="table-head toml-table-row" aria-hidden="true">
              <span>Parent key</span>
              <span>Mode</span>
              <span>Tokens</span>
            </div>
            {groupKeys.length === 0 && (
              <div style={{ padding: "16px 0", color: "var(--text-faint)" }}>
                No groups for {selectedKind} / {selectedLang}.
              </div>
            )}
            {groupKeys.map((gk) => {
              const g = groups[gk];
              return (
                <button
                  key={gk}
                  type="button"
                  className={gk === selectedGroupKey ? "table-row toml-table-row is-selected" : "table-row toml-table-row"}
                  onClick={() => startEditing(gk)}
                >
                  <span title={g.parent_key}>{g.parent_key}</span>
                  <span>{g.parent_mode || "—"}</span>
                  <span>{g.tokens.length}</span>
                </button>
              );
            })}
          </div>

          <div className="editor-panel">
            {selectedGroupKey == null ? (
              <div className="empty-panel">Select a group or click + New group.</div>
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                <label>
                  parent_key:&nbsp;
                  <input
                    type="text"
                    value={draftParentKey}
                    onChange={(e) => setDraftParentKey(e.target.value)}
                    disabled={selectedGroupKey !== NEW_GROUP_SENTINEL}
                  />
                </label>
                <label>
                  parent_mode (optional, e.g. intent / workflow):&nbsp;
                  <input
                    type="text"
                    value={draftParentMode}
                    onChange={(e) => setDraftParentMode(e.target.value)}
                    disabled={selectedGroupKey !== NEW_GROUP_SENTINEL}
                  />
                </label>
                <label style={{ display: "flex", flexDirection: "column", flex: 1 }}>
                  Tokens (one per line):
                  <textarea
                    rows={10}
                    value={draftTokens}
                    onChange={(e) => setDraftTokens(e.target.value)}
                    style={{ fontFamily: "monospace" }}
                  />
                </label>
                <label style={{ display: "flex", flexDirection: "column" }}>
                  attrs (JSON — phrase config, tools list, target path, …):
                  <textarea
                    rows={6}
                    value={draftAttrs}
                    onChange={(e) => setDraftAttrs(e.target.value)}
                    style={{ fontFamily: "monospace" }}
                  />
                </label>
                <div style={{ display: "flex", gap: 8 }}>
                  <button type="button" className="action-button" onClick={handleSave} disabled={busy}>
                    {busy ? "Saving…" : "Save group"}
                  </button>
                  {selectedGroupKey !== NEW_GROUP_SENTINEL && (
                    <button
                      type="button"
                      className="action-button"
                      onClick={handleDelete}
                      disabled={busy}
                      style={{ background: "var(--color-danger, #aa3344)" }}
                    >
                      Delete group
                    </button>
                  )}
                </div>
                {message && (
                  <div style={{ color: message.includes("failed") || message.includes("required") ? "var(--color-danger, #aa3344)" : "var(--text-faint)" }}>
                    {message}
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}

// ── Gate messages panel ──

export function GateMessagesPage() {
  const [rows, setRows] = useState<GateMsgRow[]>([]);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [draftKey, setDraftKey] = useState("");
  const [draftBody, setDraftBody] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  async function reload() {
    setMessage(null);
    try {
      const r = await gateMsgList("en");
      setRows(r);
    } catch (e) {
      setMessage(`Load failed: ${String(e)}`);
    }
  }

  useEffect(() => {
    void reload();
  }, []);

  function startEditing(key: string) {
    setSelectedKey(key);
    const row = rows.find((r) => r.key === key);
    setDraftKey(key);
    setDraftBody(row?.body ?? "");
  }

  function startNew() {
    setSelectedKey(NEW_GROUP_SENTINEL);
    setDraftKey("");
    setDraftBody("");
  }

  async function handleSave() {
    setMessage(null);
    if (!draftKey.trim()) {
      setMessage("key is required.");
      return;
    }
    setBusy(true);
    try {
      const r = await gateMsgUpsert(draftKey.trim(), draftBody, "en");
      setMessage(`Saved: deleted ${r.deleted}, inserted ${r.inserted}.`);
      await reload();
    } catch (e) {
      setMessage(`Save failed: ${String(e)}`);
    } finally {
      setBusy(false);
    }
  }

  async function handleDelete() {
    if (!selectedKey || selectedKey === NEW_GROUP_SENTINEL) return;
    if (!confirm(`Delete gate message "${draftKey}"?`)) return;
    setBusy(true);
    setMessage(null);
    try {
      await gateMsgDelete(draftKey.trim(), "en");
      setMessage("Deleted.");
      await reload();
      setSelectedKey(null);
    } catch (e) {
      setMessage(`Delete failed: ${String(e)}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="page page-config">
      <div className="page-fixed-header">
        <div style={{ display: "flex", gap: 12, alignItems: "center", padding: "8px 0" }}>
          <h2 style={{ margin: 0 }}>Gate messages</h2>
          <button type="button" className="action-button" onClick={startNew}>
            + New message
          </button>
          <span style={{ color: "var(--text-faint)" }}>
            Refusal text and tool-description hints. Use {"{placeholder}"} for variable substitution.
          </span>
        </div>
      </div>

      <div className="page-fill-region">
        <div className="toml-layout settings-documents-layout">
          <div className="flat-table">
            <div className="table-head toml-table-row" aria-hidden="true">
              <span>Key</span>
              <span>Source</span>
              <span>Preview</span>
            </div>
            {rows.map((r) => (
              <button
                key={r.key}
                type="button"
                className={r.key === selectedKey ? "table-row toml-table-row is-selected" : "table-row toml-table-row"}
                onClick={() => startEditing(r.key)}
              >
                <span title={r.key}>{r.key}</span>
                <span>{r.source}</span>
                <span title={r.body}>{r.body.length > 80 ? `${r.body.slice(0, 80)}…` : r.body}</span>
              </button>
            ))}
          </div>
          <div className="editor-panel">
            {selectedKey == null ? (
              <div className="empty-panel">Select a message or click + New message.</div>
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                <label>
                  key:&nbsp;
                  <input
                    type="text"
                    value={draftKey}
                    onChange={(e) => setDraftKey(e.target.value)}
                    disabled={selectedKey !== NEW_GROUP_SENTINEL}
                  />
                </label>
                <label style={{ display: "flex", flexDirection: "column" }}>
                  body:
                  <textarea
                    rows={12}
                    value={draftBody}
                    onChange={(e) => setDraftBody(e.target.value)}
                    style={{ fontFamily: "monospace" }}
                  />
                </label>
                <div style={{ display: "flex", gap: 8 }}>
                  <button type="button" className="action-button" onClick={handleSave} disabled={busy}>
                    {busy ? "Saving…" : "Save"}
                  </button>
                  {selectedKey !== NEW_GROUP_SENTINEL && (
                    <button
                      type="button"
                      className="action-button"
                      onClick={handleDelete}
                      disabled={busy}
                      style={{ background: "var(--color-danger, #aa3344)" }}
                    >
                      Delete
                    </button>
                  )}
                </div>
                {message && (
                  <div style={{ color: message.includes("failed") || message.includes("required") ? "var(--color-danger, #aa3344)" : "var(--text-faint)" }}>
                    {message}
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}
