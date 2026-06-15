/**
 * RBACPage — Phase 5d (2026-05-02).
 *
 * Roles & Users dashboard. Read-only for the first cut: every
 * mutation (create user / approve escalation / etc.) is gated
 * through the existing MCP rbac_* tools and not yet wired to a
 * Tauri command. The page surfaces what the king can see today;
 * actions follow in a later phase.
 *
 * Sections:
 *   - Summary chips (user count / role count / pending escalations).
 *   - Users table — identity_users rows.
 *   - Roles table — rbac_roles rows + permission count + inheritance.
 *   - Permissions catalog — rbac_permissions (read-only, code-defined).
 *   - Pending escalations — admins triage these. Click a row to copy
 *     the request_id (action wiring is follow-up work).
 */
import { useMemo, useState } from "react";
import type { RBACSnapshot } from "./dashboardApi";

export type RBACPageProps = {
  rbac: RBACSnapshot | null | undefined;
};

type Tab = "users" | "roles" | "permissions" | "escalations";

export function RBACPage({ rbac }: RBACPageProps) {
  const [activeTab, setActiveTab] = useState<Tab>("users");
  const [searchTerm, setSearchTerm] = useState("");

  const filteredUsers = useMemo(() => {
    if (!rbac) return [];
    const t = searchTerm.trim().toLowerCase();
    if (!t) return rbac.users;
    return rbac.users.filter(
      (u) =>
        u.email.toLowerCase().includes(t) ||
        u.user_id.toLowerCase().includes(t) ||
        u.role.toLowerCase().includes(t),
    );
  }, [rbac, searchTerm]);

  const filteredRoles = useMemo(() => {
    if (!rbac) return [];
    const t = searchTerm.trim().toLowerCase();
    if (!t) return rbac.roles;
    return rbac.roles.filter(
      (r) =>
        r.name.toLowerCase().includes(t) ||
        r.description.toLowerCase().includes(t) ||
        r.role_id.toLowerCase().includes(t),
    );
  }, [rbac, searchTerm]);

  const filteredPerms = useMemo(() => {
    if (!rbac) return [];
    const t = searchTerm.trim().toLowerCase();
    if (!t) return rbac.permissions;
    return rbac.permissions.filter(
      (p) =>
        p.name.toLowerCase().includes(t) ||
        p.description.toLowerCase().includes(t),
    );
  }, [rbac, searchTerm]);

  const filteredEsc = useMemo(() => {
    if (!rbac) return [];
    const t = searchTerm.trim().toLowerCase();
    if (!t) return rbac.pending_escalations;
    return rbac.pending_escalations.filter(
      (e) =>
        e.requester_label.toLowerCase().includes(t) ||
        e.gate_permission.toLowerCase().includes(t) ||
        (e.session_id ?? "").toLowerCase().includes(t) ||
        (e.task_id ?? "").toLowerCase().includes(t),
    );
  }, [rbac, searchTerm]);

  if (!rbac) {
    return (
      <section className="page page-config">
        <div className="rbac-empty">
          RBAC snapshot unavailable. Confirm a project is selected and
          identity_store + rbac_store have been initialized for it.
        </div>
      </section>
    );
  }

  const placeholder: Record<Tab, string> = {
    users: "Filter by email, user_id, role tag",
    roles: "Filter by role name, description, role_id",
    permissions: "Filter by permission name or description",
    escalations: "Filter by requester, permission, session, task",
  };

  return (
    <section className="page page-config rbac-page">
      <div className="page-fixed-header rbac-header-row">
        <div className="config-tabs">
          {(["users", "roles", "permissions", "escalations"] as const).map(
            (tab) => (
              <button
                key={tab}
                type="button"
                className={
                  activeTab === tab ? "config-tab is-active" : "config-tab"
                }
                onClick={() => setActiveTab(tab)}
              >
                {tab === "users"
                  ? `Users (${rbac.users.length})`
                  : tab === "roles"
                  ? `Roles (${rbac.roles.length})`
                  : tab === "permissions"
                  ? `Permissions (${rbac.permissions.length})`
                  : `Pending (${rbac.pending_escalations.length})`}
              </button>
            ),
          )}
        </div>
        <div className="config-header-actions">
          <span className="rbac-readonly-banner" title="Read-only first cut. Mutations go through MCP rbac_* tools.">
            read-only
          </span>
        </div>
      </div>

      <div className="rbac-control-strip">
        <input
          type="search"
          className="settings-search"
          placeholder={placeholder[activeTab]}
          value={searchTerm}
          onChange={(e) => setSearchTerm(e.target.value)}
        />
      </div>

      <div className="page-scroll-region">
        {activeTab === "users" && (
          <div className="rbac-table-wrap">
            <table className="rbac-table">
              <thead>
                <tr>
                  <th>email</th>
                  <th>role tag</th>
                  <th>user_id</th>
                  <th>created</th>
                  <th>state</th>
                </tr>
              </thead>
              <tbody>
                {filteredUsers.length === 0 ? (
                  <tr>
                    <td colSpan={5} className="rbac-empty">
                      No users match the current filter.
                    </td>
                  </tr>
                ) : (
                  filteredUsers.map((u) => (
                    <tr key={u.user_id} className={u.disabled ? "rbac-row-disabled" : ""}>
                      <td>
                        <strong>{u.email}</strong>
                      </td>
                      <td>
                        <span className="rbac-role-pill">{u.role}</span>
                      </td>
                      <td>
                        <code className="rbac-mono">{u.user_id}</code>
                      </td>
                      <td className="rbac-mono rbac-soft">{u.created_at}</td>
                      <td>
                        {u.disabled ? (
                          <span className="rbac-state-disabled">disabled</span>
                        ) : (
                          <span className="rbac-state-active">active</span>
                        )}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        )}

        {activeTab === "roles" && (
          <div className="rbac-table-wrap">
            <table className="rbac-table">
              <thead>
                <tr>
                  <th>name</th>
                  <th>description</th>
                  <th>permissions</th>
                  <th>rank</th>
                  <th>inherits</th>
                  <th>system?</th>
                </tr>
              </thead>
              <tbody>
                {filteredRoles.length === 0 ? (
                  <tr>
                    <td colSpan={6} className="rbac-empty">
                      No roles match the current filter.
                    </td>
                  </tr>
                ) : (
                  filteredRoles.map((r) => (
                    <tr key={r.role_id}>
                      <td>
                        <strong>{r.name}</strong>
                        <span className="rbac-mono rbac-soft rbac-roleid">
                          {r.role_id}
                        </span>
                      </td>
                      <td className="rbac-soft">{r.description || "—"}</td>
                      <td className="rbac-mono">{r.permission_count}</td>
                      <td className="rbac-mono">{r.rank}</td>
                      <td className="rbac-mono rbac-soft">
                        {r.inherits_from_role_key ?? "—"}
                      </td>
                      <td>
                        {r.is_system ? (
                          <span className="rbac-state-system">system</span>
                        ) : (
                          <span className="rbac-state-custom">custom</span>
                        )}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        )}

        {activeTab === "permissions" && (
          <div className="rbac-table-wrap">
            <table className="rbac-table">
              <thead>
                <tr>
                  <th>permission name</th>
                  <th>description</th>
                </tr>
              </thead>
              <tbody>
                {filteredPerms.length === 0 ? (
                  <tr>
                    <td colSpan={2} className="rbac-empty">
                      No permissions match the current filter.
                    </td>
                  </tr>
                ) : (
                  filteredPerms.map((p) => (
                    <tr key={p.name}>
                      <td>
                        <code className="rbac-mono">{p.name}</code>
                      </td>
                      <td className="rbac-soft">{p.description || "—"}</td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        )}

        {activeTab === "escalations" && (
          <div className="rbac-table-wrap">
            <table className="rbac-table">
              <thead>
                <tr>
                  <th>requester</th>
                  <th>permission</th>
                  <th>phrase</th>
                  <th>session / task</th>
                  <th>sticky?</th>
                  <th>created · expires</th>
                </tr>
              </thead>
              <tbody>
                {filteredEsc.length === 0 ? (
                  <tr>
                    <td colSpan={6} className="rbac-empty">
                      No pending escalations.
                    </td>
                  </tr>
                ) : (
                  filteredEsc.map((e) => (
                    <tr key={e.request_id}>
                      <td>
                        <strong>{e.requester_label}</strong>
                        <span className="rbac-mono rbac-soft rbac-roleid">
                          {e.requester_user_id || "—"}
                        </span>
                      </td>
                      <td>
                        <code className="rbac-mono">{e.gate_permission}</code>
                      </td>
                      <td className="rbac-soft">
                        <em>{e.gate_phrase || "—"}</em>
                      </td>
                      <td className="rbac-mono rbac-soft">
                        {e.session_id ?? "—"}
                        {e.task_id ? ` / ${e.task_id}` : ""}
                      </td>
                      <td>
                        {e.sticky ? (
                          <span className="rbac-state-sticky">sticky</span>
                        ) : (
                          <span className="rbac-state-once">once</span>
                        )}
                      </td>
                      <td className="rbac-mono rbac-soft">
                        {e.created_at}
                        {e.expires_at ? ` → ${e.expires_at}` : ""}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </section>
  );
}
