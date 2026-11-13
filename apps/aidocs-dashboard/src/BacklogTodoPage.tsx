/**
 * BacklogTodoPage — combined Backlog + Todos view (Phoenix 2026-05-09).
 *
 * Shell pre-created by Phoenix conductor; cerberus B1 worker filled in.
 * Two-tab page: [Backlog | Todos]. Each tab is a list with per-row
 * actions (edit content, delete with reason, change status, change
 * priority/urgency) + relation chips when items are linked
 * (promoted_from_todo_id / promoted_to_backlog_id / linked_task_id).
 *
 * Wraps Tauri commands tauri_backlog_* + tauri_todo_* which invoke
 * the AIDOCS backlog/todo stores (agent surface: ai_backlog +
 * ai_task todo modes since the #83 merge) through the existing
 * run_python_with_args bridge (mirrors the msg_send pattern).
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  backlogAdd,
  backlogList,
  backlogRemove,
  backlogUpdate,
  todoList,
  todoRemove,
  todoUpdate,
  type BacklogAuthority,
  type BacklogItem,
  type TodoItem,
} from "./dashboardApi";
import { refusalBanner, type Banner } from "./authorityPresentation";

export type BacklogTodoPageProps = {
  projectRoot: string | null;
  sessionId: string | null;
};

type Tab = "backlog" | "todos";

// Mirrors project_backlog_store._STATUSES minus the two states reached ONLY by
// a dedicated operation: `removed` (tombstone, needs a reason) and `merged` (a
// RELATION, #450/#580). `rejected` was MISSING until 2026-07-30 — a rejected
// item rendered in a <select> whose value was not among its options, so the
// browser showed the first option instead and the operator saw REJECTED items
// labelled OPEN, with no way to set that status at all. Pinned by
// mcp/tests/security/test_dashboard_client_truth_parity.py, which READS
// _STATUSES rather than restating it.
const STATUSES = ["open", "in_progress", "done", "blocked", "rejected"];
const PRIORITIES = ["idea", "low", "normal", "high", "urgent", "critical"];
const TODO_URGENCIES = ["low", "normal", "high", "urgent", "critical"];

// #573 KIND — WHAT KIND OF MONSTER an item is, orthogonal to priority (which
// says how much it matters). severity x kind is the triage grid; high-severity
// x known-fix is the actionable quadrant. Mirrors
// project_backlog_store.KIND_ORDER, cheapest-to-act-on first; ORDER IS PART OF
// THE TRUTH and is asserted, not just membership.
//
// Added 2026-07-30. Until then the dashboard had NO kind surface at all — the
// operator could neither see nor set half his own triage grid. That was an
// ABSENCE, not a drift: a registered truth with no consumer is inert in exactly
// the way nothing else detects.
//
// KIND_UNSET ("") is deliberately ABSENT here: it is the stored default and
// validate_kind() REFUSES it as a write value, because an item nobody rated
// must stay distinguishable from one deliberately marked `investigate`. The
// select below shows "unrated" as a DISABLED option so the state is visible
// without being settable. Pinned by
// mcp/tests/security/test_dashboard_client_truth_parity.py.
const KINDS = ["known-fix", "wire-up", "design", "investigate", "research"];
const KIND_UNRATED_LABEL = "unrated";

// #101/#236: operator-facing urgency markers — same ladder as the MCP tools.
const URGENCY_ICONS: Record<string, string> = {
  critical: "🔴",
  urgent: "🟠",
  high: "🟡",
  normal: "⚪",
  low: "·",
  idea: "…",
};

// #236: ladder rank for the urgency sort — higher renders first.
const TIER_RANK: Record<string, number> = {
  critical: 5,
  urgent: 4,
  high: 3,
  normal: 2,
  low: 1,
  idea: 0,
};

/** Backlog rows carry `priority`; todo rows carry `urgency`. One ladder. */
function tierOf(item: BacklogItem | TodoItem, isBacklog: boolean): string {
  const raw = isBacklog
    ? (item as BacklogItem).priority
    : (item as TodoItem).urgency;
  return (raw as string) || "normal";
}

function relationChips(item: BacklogItem | TodoItem): string[] {
  const chips: string[] = [];
  const promoFrom = (item as BacklogItem).promoted_from_todo_id;
  const promoTo = (item as TodoItem).promoted_to_backlog_id;
  const linked = (item as TodoItem).linked_task_id;
  if (promoFrom) chips.push(`from todo #${promoFrom}`);
  if (promoTo) chips.push(`-> backlog #${promoTo}`);
  if (linked) chips.push(`task ${linked}`);
  return chips;
}

/**
 * A refused act rendered AS A REFUSAL (2026-07-30).
 *
 * Backlog CRUD is permission-gated. The one thing this panel exists to prevent
 * is a gate verdict arriving as blank data: "No backlog items." over a refused
 * read is indistinguishable from an empty backlog, and that is precisely how a
 * 145-item backlog read as nothing at all.
 */
function RefusalPanel({ banner }: { banner: Banner }) {
  return (
    <div className="backlog-todo-refusal" role="alert">
      <div className="backlog-todo-refusal-title">{banner.title}</div>
      <div className="backlog-todo-refusal-message">{banner.message}</div>
      <div className="backlog-todo-refusal-hint">{banner.hint}</div>
      <div className="backlog-todo-refusal-code">reason: {banner.code}</div>
    </div>
  );
}

export function BacklogTodoPage({ projectRoot, sessionId }: BacklogTodoPageProps) {
  const [tab, setTab] = useState<Tab>("backlog");
  const [backlog, setBacklog] = useState<BacklogItem[]>([]);
  const [todos, setTodos] = useState<TodoItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Two refusal channels, because the two failures mean different things:
  //   readRefusal   — the LIST could not be read; it REPLACES the list, so the
  //                   operator never sees an empty state that is really a gate.
  //   actionRefusal — a write was refused; the list is still valid, so this is
  //                   a banner beside it. Never a silent no-op: the API layer's
  //                   `{"ok": true, "applied": []}` defect is not reproduced.
  const [readRefusal, setReadRefusal] = useState<Banner | null>(null);
  const [actionRefusal, setActionRefusal] = useState<Banner | null>(null);

  /** Returns true when the call was REFUSED (caller must not proceed). */
  function noteActionRefusal(r: BacklogAuthority): boolean {
    if (r?.ok === false) {
      setActionRefusal(refusalBanner(r));
      return true;
    }
    setActionRefusal(null);
    return false;
  }
  const [newContent, setNewContent] = useState("");
  const [newPriority, setNewPriority] = useState<string>("normal");
  const [tierFilter, setTierFilter] = useState<string>("all");
  // #236: opt-in urgency sort — off preserves the store's list order.
  const [sortByUrgency, setSortByUrgency] = useState(false);

  const root = projectRoot ?? undefined;

  const refreshBacklog = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await backlogList(root);
      // A REFUSAL IS NOT AN EMPTY BACKLOG. `items` is ABSENT on a refusal, so
      // the old `r.items ?? []` turned every gate verdict into "No backlog
      // items." — a defect reported in the language of absence.
      if (r.ok === false) {
        setReadRefusal(refusalBanner(r));
        setBacklog([]);
        return;
      }
      setReadRefusal(null);
      setBacklog(r.items ?? []);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [root]);

  const refreshTodos = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await todoList(root, { sessionId: sessionId ?? undefined });
      setTodos(r.items ?? []);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [root, sessionId]);

  useEffect(() => {
    if (tab === "backlog") void refreshBacklog();
    else void refreshTodos();
  }, [tab, refreshBacklog, refreshTodos]);

  async function handleAddBacklog() {
    if (!newContent.trim()) return;
    try {
      const r = await backlogAdd(newContent.trim(), root, { priority: newPriority });
      // Refused: keep the text in the box (the operator should not lose it) and
      // show WHY. Clearing the input would look exactly like a successful add.
      if (noteActionRefusal(r)) return;
      setNewContent("");
      await refreshBacklog();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function handleBacklogStatus(item: BacklogItem, status: string) {
    try {
      if (noteActionRefusal(await backlogUpdate(item.id, { status }, root))) return;
      await refreshBacklog();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function handleBacklogPriority(item: BacklogItem, priority: string) {
    try {
      if (noteActionRefusal(await backlogUpdate(item.id, { priority }, root))) return;
      await refreshBacklog();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  // #573: rating an item's kind. One-way by design — the store refuses a write
  // back to unrated (KIND_UNSET is not storable), so the empty option below is
  // display-only. Kind is MEANT to change as understanding improves, which is
  // why this is an ordinary inline edit and not a one-shot classification.
  async function handleBacklogKind(item: BacklogItem, kind: string) {
    if (!kind) return; // the disabled "unrated" option — never sent
    try {
      if (noteActionRefusal(await backlogUpdate(item.id, { kind }, root))) return;
      await refreshBacklog();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function handleBacklogContent(item: BacklogItem) {
    const next = window.prompt("Edit content:", item.content ?? item.title ?? "");
    if (next === null || next === (item.content ?? "")) return;
    try {
      if (noteActionRefusal(await backlogUpdate(item.id, { content: next }, root))) return;
      await refreshBacklog();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function handleBacklogRemove(item: BacklogItem) {
    const reason = window.prompt("Reason for removal:");
    if (!reason) return;
    try {
      if (noteActionRefusal(await backlogRemove(item.id, reason, root))) return;
      await refreshBacklog();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  // The store's update() requires the owner task_id; the row itself knows
  // its owner, and session_id rides along for the same-session escape.
  function todoOwner(item: TodoItem): { taskId?: string; sessionId?: string } {
    return {
      taskId: item.task_id ?? undefined,
      sessionId: item.session_id ?? undefined,
    };
  }

  async function handleTodoStatus(item: TodoItem, status: string) {
    try {
      await todoUpdate(item.id, { status, ...todoOwner(item) }, root);
      await refreshTodos();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function handleTodoUrgency(item: TodoItem, urgency: string) {
    try {
      await todoUpdate(item.id, { urgency, ...todoOwner(item) }, root);
      await refreshTodos();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function handleTodoContent(item: TodoItem) {
    const next = window.prompt("Edit content:", item.content ?? "");
    if (next === null || next === (item.content ?? "")) return;
    try {
      await todoUpdate(item.id, { content: next, ...todoOwner(item) }, root);
      await refreshTodos();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function handleTodoRemove(item: TodoItem) {
    const reason = window.prompt("Reason for removal:");
    if (!reason) return;
    try {
      await todoRemove(item.id, reason, root);
      await refreshTodos();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  const items: Array<BacklogItem | TodoItem> = useMemo(() => {
    const isBacklog = tab === "backlog";
    const base = isBacklog ? backlog : todos;
    const filtered =
      tierFilter === "all" ? base : base.filter((it) => tierOf(it, isBacklog) === tierFilter);
    if (!sortByUrgency) return filtered;
    // Stable sort: critical first, store order preserved within a tier.
    return [...filtered].sort(
      (a, b) => (TIER_RANK[tierOf(b, isBacklog)] ?? 2) - (TIER_RANK[tierOf(a, isBacklog)] ?? 2),
    );
  }, [tab, backlog, todos, tierFilter, sortByUrgency]);

  // #236: "N urgent" — urgent+critical rows in the CURRENT tab, pre-filter,
  // so the badge stays honest while a narrower tier filter is applied.
  const urgentCount = useMemo(() => {
    const isBacklog = tab === "backlog";
    const base = isBacklog ? backlog : todos;
    return base.filter((it) => {
      const tier = tierOf(it, isBacklog);
      return tier === "urgent" || tier === "critical";
    }).length;
  }, [tab, backlog, todos]);

  return (
    <div className="backlog-todo-page">
      <div className="backlog-todo-tabs">
        <button
          type="button"
          className={"backlog-todo-tab" + (tab === "backlog" ? " is-active" : "")}
          onClick={() => setTab("backlog")}
        >
          Backlog ({readRefusal && tab === "backlog" ? "—" : backlog.length})
        </button>
        <button
          type="button"
          className={"backlog-todo-tab" + (tab === "todos" ? " is-active" : "")}
          onClick={() => setTab("todos")}
        >
          Todos ({todos.length})
        </button>
        {urgentCount > 0 ? (
          <button
            type="button"
            className="backlog-todo-urgent-count"
            title="Urgent + critical items in this tab — click to sort them to the top"
            onClick={() => setSortByUrgency((v) => !v)}
          >
            {URGENCY_ICONS.urgent} {urgentCount} urgent
          </button>
        ) : null}
        <select
          className="backlog-todo-tier-filter"
          value={tierFilter}
          onChange={(e) => setTierFilter(e.target.value)}
          title="Filter by urgency/priority tier"
        >
          <option value="all">all tiers</option>
          {(tab === "backlog" ? PRIORITIES : TODO_URGENCIES).map((p) => (
            <option key={p} value={p}>
              {URGENCY_ICONS[p] ?? ""} {p}
            </option>
          ))}
        </select>
        <button
          type="button"
          className={"backlog-todo-sort" + (sortByUrgency ? " is-active" : "")}
          title="Toggle sorting by urgency (critical first)"
          onClick={() => setSortByUrgency((v) => !v)}
        >
          Sort: {sortByUrgency ? "urgency" : "default"}
        </button>
        <button
          type="button"
          className="backlog-todo-refresh"
          onClick={() => (tab === "backlog" ? void refreshBacklog() : void refreshTodos())}
        >
          Refresh
        </button>
      </div>

      {error ? <div className="backlog-todo-error">{error}</div> : null}
      {actionRefusal ? <RefusalPanel banner={actionRefusal} /> : null}

      {tab === "backlog" ? (
        <div className="backlog-todo-add">
          <input
            type="text"
            placeholder="New backlog item..."
            value={newContent}
            onChange={(e) => setNewContent(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void handleAddBacklog();
            }}
          />
          <select value={newPriority} onChange={(e) => setNewPriority(e.target.value)}>
            {PRIORITIES.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
          <button type="button" onClick={() => void handleAddBacklog()}>
            Add
          </button>
        </div>
      ) : null}

      <div className="backlog-todo-body">
        {loading ? (
          <div className="backlog-todo-empty">Loading...</div>
        ) : readRefusal && tab === "backlog" ? (
          // The refusal REPLACES the list. Never the empty-state copy.
          <RefusalPanel banner={readRefusal} />
        ) : items.length === 0 ? (
          <div className="backlog-todo-empty">
            {tab === "backlog" ? "No backlog items." : "No todos for this session."}
          </div>
        ) : (
          <ul className="backlog-todo-list">
            {items.map((item) => {
              const chips = relationChips(item);
              const text = (item as BacklogItem).content ?? (item as BacklogItem).title ?? "";
              const isBacklog = tab === "backlog";
              const tier = tierOf(item, isBacklog);
              const tierClass =
                tier === "critical" ? " is-critical" : tier === "urgent" ? " is-urgent" : "";
              return (
                <li key={item.id} className={"backlog-todo-row" + tierClass}>
                  <div className="backlog-todo-row-main">
                    <span className="backlog-todo-tier-icon" title={tier}>
                      {URGENCY_ICONS[tier] ?? "⚪"}
                    </span>
                    <span className="backlog-todo-id">#{item.id}</span>
                    <span className="backlog-todo-text">{text}</span>
                    {chips.map((c) => (
                      <span key={c} className="backlog-todo-chip">
                        {c}
                      </span>
                    ))}
                  </div>
                  <div className="backlog-todo-row-actions">
                    <select
                      value={item.status ?? "open"}
                      onChange={(e) =>
                        isBacklog
                          ? void handleBacklogStatus(item as BacklogItem, e.target.value)
                          : void handleTodoStatus(item as TodoItem, e.target.value)
                      }
                    >
                      {STATUSES.map((s) => (
                        <option key={s} value={s}>
                          {s}
                        </option>
                      ))}
                    </select>
                    {isBacklog ? (
                      <>
                        <select
                          value={(item as BacklogItem).priority ?? "normal"}
                          onChange={(e) => void handleBacklogPriority(item as BacklogItem, e.target.value)}
                        >
                          {PRIORITIES.map((p) => (
                            <option key={p} value={p}>
                              {URGENCY_ICONS[p] ?? ""} {p}
                            </option>
                          ))}
                        </select>
                        {/* #573 kind — the other axis of the triage grid. */}
                        <select
                          className="backlog-todo-kind"
                          title="Kind (#573): what kind of work this is — orthogonal to priority"
                          value={((item as BacklogItem).kind as string) || ""}
                          onChange={(e) => void handleBacklogKind(item as BacklogItem, e.target.value)}
                        >
                          {/* Display-only: the store refuses a write back to unrated. */}
                          <option value="" disabled>
                            {KIND_UNRATED_LABEL}
                          </option>
                          {KINDS.map((k) => (
                            <option key={k} value={k}>
                              {k}
                            </option>
                          ))}
                        </select>
                      </>
                    ) : (
                      <select
                        value={(item as TodoItem).urgency ?? "normal"}
                        onChange={(e) => void handleTodoUrgency(item as TodoItem, e.target.value)}
                      >
                        {TODO_URGENCIES.map((p) => (
                          <option key={p} value={p}>
                            {URGENCY_ICONS[p] ?? ""} {p}
                          </option>
                        ))}
                      </select>
                    )}
                    <button
                      type="button"
                      onClick={() =>
                        isBacklog
                          ? void handleBacklogContent(item as BacklogItem)
                          : void handleTodoContent(item as TodoItem)
                      }
                    >
                      Edit
                    </button>
                    <button
                      type="button"
                      onClick={() =>
                        isBacklog
                          ? void handleBacklogRemove(item as BacklogItem)
                          : void handleTodoRemove(item as TodoItem)
                      }
                    >
                      Delete
                    </button>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}
