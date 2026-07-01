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
 * AIDOCS MCP tools ai_backlog / ai_todo through the existing
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
  type BacklogItem,
  type TodoItem,
} from "./dashboardApi";

export type BacklogTodoPageProps = {
  projectRoot: string | null;
  sessionId: string | null;
};

type Tab = "backlog" | "todos";

const STATUSES = ["open", "in_progress", "done", "blocked"];
const PRIORITIES = ["idea", "low", "normal", "high", "urgent", "critical"];
const TODO_URGENCIES = ["low", "normal", "high", "urgent", "critical"];

// #101/#236: operator-facing urgency markers — same ladder as the MCP tools.
const URGENCY_ICONS: Record<string, string> = {
  critical: "🔴",
  urgent: "🟠",
  high: "🟡",
  normal: "⚪",
  low: "·",
  idea: "…",
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

export function BacklogTodoPage({ projectRoot, sessionId }: BacklogTodoPageProps) {
  const [tab, setTab] = useState<Tab>("backlog");
  const [backlog, setBacklog] = useState<BacklogItem[]>([]);
  const [todos, setTodos] = useState<TodoItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [newContent, setNewContent] = useState("");
  const [newPriority, setNewPriority] = useState<string>("normal");
  const [tierFilter, setTierFilter] = useState<string>("all");

  const root = projectRoot ?? undefined;

  const refreshBacklog = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await backlogList(root);
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
      await backlogAdd(newContent.trim(), root, { priority: newPriority });
      setNewContent("");
      await refreshBacklog();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function handleBacklogStatus(item: BacklogItem, status: string) {
    try {
      await backlogUpdate(item.id, { status }, root);
      await refreshBacklog();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function handleBacklogPriority(item: BacklogItem, priority: string) {
    try {
      await backlogUpdate(item.id, { priority }, root);
      await refreshBacklog();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function handleBacklogContent(item: BacklogItem) {
    const next = window.prompt("Edit content:", item.content ?? item.title ?? "");
    if (next === null || next === (item.content ?? "")) return;
    try {
      await backlogUpdate(item.id, { content: next }, root);
      await refreshBacklog();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function handleBacklogRemove(item: BacklogItem) {
    const reason = window.prompt("Reason for removal:");
    if (!reason) return;
    try {
      await backlogRemove(item.id, reason, root);
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
    const base = tab === "backlog" ? backlog : todos;
    if (tierFilter === "all") return base;
    return base.filter((it) => tierOf(it, tab === "backlog") === tierFilter);
  }, [tab, backlog, todos, tierFilter]);

  return (
    <div className="backlog-todo-page">
      <div className="backlog-todo-tabs">
        <button
          type="button"
          className={"backlog-todo-tab" + (tab === "backlog" ? " is-active" : "")}
          onClick={() => setTab("backlog")}
        >
          Backlog ({backlog.length})
        </button>
        <button
          type="button"
          className={"backlog-todo-tab" + (tab === "todos" ? " is-active" : "")}
          onClick={() => setTab("todos")}
        >
          Todos ({todos.length})
        </button>
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
          className="backlog-todo-refresh"
          onClick={() => (tab === "backlog" ? void refreshBacklog() : void refreshTodos())}
        >
          Refresh
        </button>
      </div>

      {error ? <div className="backlog-todo-error">{error}</div> : null}

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
