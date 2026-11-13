// Session detail popup — pure helpers.
//
// Kept separate from the modal component so the parts that can be WRONG in a
// way the operator would act on (the copyable CLI command, the host-binding
// join) are unit-tested without rendering.

export type HostAgent = {
  host_session_id: string;
  agent_context_id?: string;
  role: string;
  session_id: string;
  host_kind?: string;
  live: boolean;
  pid?: number | null;
  activated_at?: string;
  last_updated?: string;
  source?: string;
};

export type SessionRow = {
  session_id: string;
  title: string | null;
  status: string | null;
  owner: string | null;
  goal: string | null;
  last_updated: string | null;
  selected: boolean;
  managed: boolean;
  owner_granted?: boolean;
};

/** Display name for a session row.
 *
 * `title ?? session_id` was the bug behind the "session with no name": the
 * sessions ledger stores an EMPTY STRING for untitled sessions, and `??` only
 * falls back on null/undefined, so a blank title rendered as a blank cell —
 * a nameless row whose delete dialog nonetheless knew its id. Any blank-ish
 * title falls back to the id here.
 */
export function sessionDisplayName(session: Pick<SessionRow, "session_id" | "title">): string {
  const title = (session.title ?? "").trim();
  return title || session.session_id;
}

/** True when the ledger has no real title, so the UI can say so explicitly
 *  rather than silently showing an id that looks like a name. */
export function sessionTitleIsMissing(session: Pick<SessionRow, "title">): boolean {
  return !(session.title ?? "").trim();
}

/** Host bindings for one session, live ones first, then most recent.
 *
 * The join key is `session_id`. NOT `aidocs_session_id` — that is a derived
 * 16-char security-scope hash (sha256 of project+host+session), never a
 * foreign key to the sessions ledger.
 */
export function hostBindingsForSession(
  agents: HostAgent[] | undefined,
  sessionId: string,
): HostAgent[] {
  if (!agents || !sessionId) return [];
  return agents
    .filter((a) => a.session_id === sessionId)
    .sort((a, b) => {
      if (a.live !== b.live) return a.live ? -1 : 1;
      return String(b.last_updated ?? "").localeCompare(String(a.last_updated ?? ""));
    });
}

/** Host TYPE is reliable (recorded for every worker row); host ID is not always
 *  stamped. Renders what is known without implying the rest. */
export function describeHost(agent: HostAgent): string {
  const kind = (agent.host_kind ?? "").trim() || "unknown host";
  const id = (agent.host_session_id ?? "").trim();
  return id ? `${kind} · ${id}` : `${kind} · (host id not stamped)`;
}

function quote(value: string): string {
  // Quote only when needed; Windows project roots contain spaces and backslashes.
  return /[\s"]/.test(value) ? `"${value.replace(/"/g, '\\"')}"` : value;
}

export type SessionCommand = { label: string; command: string; note?: string };

/** The commands an operator can paste to reach this session.
 *
 * This is the REAL argv the dashboard itself uses (`dashboard-connect-session`
 * with `--session`), not an invented convenience syntax — a copyable command
 * that does not exist is worse than none. The CLI registers exactly three
 * session commands (dashboard-{connect,create,delete}-session); anything else
 * offered here would fail when pasted, so nothing else is offered.
 */
export function sessionCliCommands(projectRoot: string | null, sessionId: string): SessionCommand[] {
  const root = (projectRoot ?? "").trim();
  const rootArg = root ? ` ${quote(root)}` : "";
  return [
    {
      label: "Bind this session (managed mode)",
      command: `aidocs dashboard-connect-session${rootArg} --session ${quote(sessionId)}`,
      note: root
        ? "Requires an authenticated operator; add --operator-token if it is not cached."
        : "No project root selected — add the project path as the first argument.",
    },
  ];
}
