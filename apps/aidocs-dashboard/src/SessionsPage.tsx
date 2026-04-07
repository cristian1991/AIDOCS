import type { SessionsPageProps } from "./dashboardTypes";

export function SessionsPage({ sessions, sessionValue, onSelectSession }: SessionsPageProps) {
  return (
    <section className="page">
      <div className="flat-table">
        <div className="table-head session-table-row" aria-hidden="true">
          <span>Session</span>
          <span>Status</span>
          <span>Owner</span>
          <span>Updated</span>
        </div>
        {sessions.map((session) => (
          <button
            key={session.session_id}
            className={session.session_id === sessionValue ? "table-row session-table-row is-selected" : "table-row session-table-row"}
            type="button"
            onClick={() => onSelectSession(session.session_id)}
          >
            <span>{session.title ?? session.session_id}</span>
            <span>{session.status ?? "unknown"}</span>
            <span>{session.owner ?? "unowned"}</span>
            <span>{session.last_updated ?? "n/a"}</span>
          </button>
        ))}
      </div>
    </section>
  );
}
