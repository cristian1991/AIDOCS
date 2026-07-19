from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ._sqlite_index_store_base import SQLiteIndexStoreBase


class SessionQueryGateStore(SQLiteIndexStoreBase):
    """Per-session query-gate state — sqlite-backed replacement for
    ``.MEMORY/sessions/{id}/query-gate.json``.

    One row per ``session_id``. List/dict fields live as JSON-encoded
    TEXT columns because callers always read the whole row and never
    query into those fields — normalizing them into child tables would
    multiply joins without buying any query expressiveness.

    Legacy JSONs for all existing sessions are ingested and hard-deleted
    on first ``init_db()`` per project. The store is the single source
    of truth post-Beat-3.

    ─── AIDOCS-SEC INVARIANT ───────────────────────────────────────
    Blocked or unmanaged prompt MUST NOT change any privilege-relevant
    session state.

    When you add a new ``set_*`` method that writes privilege-relevant
    state (tool permissions, escalation, lane identity, confirmation
    state, credentials, grant flags) you MUST:

      1. Add the backing column to ``_PRIVILEGE_COLUMNS`` so the
         snapshot/restore hotfix covers it.
      2. OR document here why the write is safe to run before
         validation (carve-out: liveness, audit, defensive detection).
      3. Add a test in ``test_prompt_mutation_plan.py`` asserting the
         column is unchanged after a blocked prompt.

    The contract: every privilege-relevant column must be either in
    ``_PRIVILEGE_COLUMNS`` (restored on block) OR explicitly carved
    out with a written justification. Silent additions break the
    invariant.

    Full design rationale: ``.MEMORY/domains/security-model.md``
    sections 11 (review findings) + SEC-001 ticket scope.
    ─────────────────────────────────────────────────────────────────
    """

    _TABLE_COLUMNS: tuple[str, ...] = (
        "session_id",
        "last_tool",
        "known_exact_paths",
        "current_lane_id",
        "lane_exact_paths",
        "lane_allowed_tools",
        "lane_extra_tools",
        "lane_raw_tools_granted",
        "user_intent_tools",
        "user_intent_bash_subcommands",
        "turn_edited_files",
        "current_task_id",
        "updated_at",
    )

    def init_db(self, project_root: Path) -> None:
        with self.session(project_root) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS session_query_gate (
                    session_id TEXT PRIMARY KEY,
                    last_tool TEXT,
                    known_exact_paths TEXT NOT NULL DEFAULT '[]',
                    current_lane_id TEXT,
                    lane_exact_paths TEXT NOT NULL DEFAULT '[]',
                    lane_allowed_tools TEXT NOT NULL DEFAULT '[]',
                    lane_extra_tools TEXT NOT NULL DEFAULT '[]',
                    lane_raw_tools_granted TEXT NOT NULL DEFAULT '{}',
                    user_intent_tools TEXT NOT NULL DEFAULT '[]',
                    user_intent_bash_subcommands TEXT NOT NULL DEFAULT '[]',
                    turn_edited_files TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT
                );
                """,
            )
            # #151 rename migration (2026-07-12): `last_cli_session_id` was a
            # misnomer — it stores the HOST_SESSION_ID the PreToolUse hook
            # stamps (recovery fallback). Renamed to `last_host_session_id`.
            # MUST run BEFORE the additive ALTER loop below so an existing
            # DB gets its old column RENAMED IN PLACE (data preserved) and
            # the later ADD COLUMN no-ops. Guarded: runs only when the old
            # column exists and the new one does not; never crashes init_db
            # on either state (fresh DB / already-migrated DB).
            try:
                cols = {
                    str(r[1])
                    for r in conn.execute(
                        "PRAGMA table_info(session_query_gate)"
                    ).fetchall()
                }
                if (
                    "last_cli_session_id" in cols
                    and "last_host_session_id" not in cols
                ):
                    conn.execute(
                        "ALTER TABLE session_query_gate "
                        "RENAME COLUMN last_cli_session_id "
                        "TO last_host_session_id"
                    )
            except Exception:
                # Defensive: migration state must never break init_db.
                pass
            # Plan-mode columns added 2026-04-18 (beat 1, phase 2). Pre-existing
            # rows get the defaults via the additive ALTER. ADD COLUMN IF NOT
            # EXISTS isn't supported until sqlite 3.35; the try/pass is the
            # standard ingest-on-init upgrade pattern used by the other stores.
            for column_def in (
                "ALTER TABLE session_query_gate ADD COLUMN plan_mode_active INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE session_query_gate ADD COLUMN plan_mode_scope TEXT",
                "ALTER TABLE session_query_gate ADD COLUMN plan_mode_started_at TEXT",
                "ALTER TABLE session_query_gate ADD COLUMN plan_mode_last_activity_at TEXT",
                # Beat 2 of conductor-test prep (2026-04-18): conductor pre-grants
                # deferred MCP tools to a lane so the sub-agent calls them by
                # name without paying ToolSearch round-trips. Stored as
                # JSON dict[lane_id → list[tool_name]] mirroring
                # lane_raw_tools_granted shape.
                "ALTER TABLE session_query_gate ADD COLUMN lane_eager_tools_granted TEXT NOT NULL DEFAULT '{}'",
                # Audit hardening (2026-04-19): current_task_id is the SHA-
                # based id of the active task on this session. task_begin
                # writes it; task_complete clears it. Every execution_event
                # row records the value at insert time so "what task was
                # the agent doing when this happened" becomes one JOIN.
                # Empty string (default) means "no task active".
                "ALTER TABLE session_query_gate ADD COLUMN current_task_id TEXT NOT NULL DEFAULT ''",
                # forced_work_active (2026-04-20, VESTIGIAL since
                # 2026-04-30): tracked the conductor forced-work mode
                # override. Kept in schema for migration safety after
                # the autowake feature was removed; no code path
                # reads or writes it. Defense-in-depth: still listed
                # in _PRIVILEGE_COLUMNS so a blocked prompt cannot
                # mutate it even if a future code path resurrects it.
                "ALTER TABLE session_query_gate ADD COLUMN forced_work_active INTEGER NOT NULL DEFAULT 0",
                # Ask-state confirmation (2026-04-20): when the judge
                # catches a destructive command AND an active user
                # intent covers it, the pending_confirmation row holds
                # the question surfaced to the operator. Next prompt
                # is parsed for yes/no and either flips this into
                # last_confirmed_operation (one-shot bypass) or clears
                # it. TTL hard-capped at 1 turn — `session_turn_counter`
                # increments on each UserPromptSubmit; any confirmation
                # whose turn_at_create < counter - 0 is expired.
                "ALTER TABLE session_query_gate ADD COLUMN pending_confirmation TEXT",
                "ALTER TABLE session_query_gate ADD COLUMN last_confirmed_operation TEXT",
                "ALTER TABLE session_query_gate ADD COLUMN session_turn_counter INTEGER NOT NULL DEFAULT 0",
                # Causal turn id (#441, 2026-07-18). Server-minted opaque id of
                # the CURRENT operator turn. Rotated at UserPromptSubmit only
                # when the provenance-floored prompt text is operator-authored
                # AND the origin gate marks the prompt authority-bearing —
                # harness interrupts / mid-turn injections / worker prompts
                # never rotate it. Every execution_events row records the
                # value at insert time (column `turn_id`, hash-bound since
                # audit v4) so "which operator instruction caused this tool
                # call" is one JOIN. NOT privilege-relevant state (carve-out:
                # audit attribution, like current_task_id) — a blocked prompt
                # keeps its minted turn so the block events attribute to it.
                "ALTER TABLE session_query_gate ADD COLUMN current_turn_id TEXT NOT NULL DEFAULT ''",
                # last_autowake_at (2026-04-20, VESTIGIAL since
                # 2026-04-30): tracked the most recent ScheduleWakeup.
                # Kept in schema for migration safety after autowake
                # removal. No code path reads or writes it.
                "ALTER TABLE session_query_gate ADD COLUMN last_autowake_at INTEGER NOT NULL DEFAULT 0",
                # Compaction grace: records the epoch when the
                # session's context_compact event last fired. Still
                # used by session-freeze recovery (compaction grace
                # window). Unrelated to the removed autowake feature.
                "ALTER TABLE session_query_gate ADD COLUMN last_compaction_at INTEGER NOT NULL DEFAULT 0",
                # Grants generation (2026-04-24): monotonic counter bumped
                # every time sticky NLP tool grants change. The MCP
                # server's call_tool wrapper reads this per-call, compares
                # to its in-process "last synced" value, and when it
                # advances: pulls the current sticky grants from sqlite
                # and calls server.enable(names=...) on any newly-granted
                # deferred tool. FastMCP auto-emits notifications/tools/
                # list_changed from enable(), so the host re-fetches and
                # the tool becomes visible+callable mid-session without
                # an MCP restart. Non-sticky / raw-tool grants do NOT
                # bump this counter (they stay per-turn and don't need
                # in-process enable flips).
                "ALTER TABLE session_query_gate ADD COLUMN grants_generation INTEGER NOT NULL DEFAULT 0",
                # User-intent credentials (2026-04-21): JSON list of
                # provider-credential tokens that appeared verbatim in
                # the most recent UserPromptSubmit. When the judge fires
                # a FILE_*_KEY verdict and the matched token is in this
                # list, the hard-block is downgraded to an ask-state
                # confirm (user pasted the key → user intent covers it).
                # TTL: cleared at each new UserPromptSubmit — only the
                # CURRENT prompt's credentials count.
                "ALTER TABLE session_query_gate ADD COLUMN user_intent_credentials TEXT NOT NULL DEFAULT '[]'",
                # User-intent destructive tokens (2026-04-25, Phase 4
                # of backlog #15). JSON list of destructive-intent
                # tokens matched in the most recent UserPromptSubmit
                # ("nuke", "delete", "wipe", "force", "destroy", etc.).
                # When the judge fires a destructive-pattern verdict
                # AND this list is non-empty, the hard-block downgrades
                # to an ask-state confirm — operator expressed intent,
                # gets the final sign-off. Empty list → judge hard-
                # blocks as before (no intent, no ask).
                # TTL: cleared at each UserPromptSubmit — only the
                # CURRENT prompt's tokens count.
                "ALTER TABLE session_query_gate ADD COLUMN user_intent_destructive TEXT NOT NULL DEFAULT '[]'",
                # Fresh-CLI detection (2026-04-21). Claude Code sends a
                # per-process session_id UUID in every hook payload; it's
                # stable across turns in the same window and rotates on
                # reopen. Storing the last seen value lets us detect when
                # a fresh CLI has inherited an AIDOCS-managed session's
                # sqlite state (known_exact_paths, current_lane_id) even
                # though the agent's own in-memory context is empty.
                # When it changes, clear known_exact_paths + force a
                # session_connect before any other tool.
                "ALTER TABLE session_query_gate ADD COLUMN last_host_session_id TEXT NOT NULL DEFAULT ''",
                # Sticky "must reconnect" flag. Raised when fresh-CLI is
                # detected; cleared by session_connect (or any
                # explicit session-bind tool). While true, only a
                # bootstrap tool allowlist can run — every other tool
                # is hard-refused with "call session_connect first".
                "ALTER TABLE session_query_gate ADD COLUMN requires_reconnect INTEGER NOT NULL DEFAULT 0",
                # Per-turn config_set grants (2026-04-21). Populated by
                # the UPS path from canonical_intent_registry.detect_config_grants_v2.
                # MUST live in sqlite because the MCP tool server and
                # the hook run in separate processes — protected_file_
                # runtime's module-level dict is per-process and doesn't
                # cross the boundary. JSON dict: {dotted.key: value}.
                # Cleared wholesale on each new UserPromptSubmit.
                "ALTER TABLE session_query_gate ADD COLUMN config_grants TEXT NOT NULL DEFAULT '{}'",
                # Per-turn DNT grant axes (2026-05-12). Same cross-process
                # rationale as config_grants — claude_hook (in CC's hook
                # subprocess) writes them; ai_protect / file_ops (in the
                # MCP server subprocess) read them. The module-level lists
                # in protected_file_runtime never crossed the boundary,
                # so ai_protect refused every grant in production.
                # JSON arrays of normalized relative paths. Cleared
                # wholesale on each new UserPromptSubmit.
                "ALTER TABLE session_query_gate ADD COLUMN protect_grants TEXT NOT NULL DEFAULT '[]'",
                "ALTER TABLE session_query_gate ADD COLUMN unprotect_grants TEXT NOT NULL DEFAULT '[]'",
                "ALTER TABLE session_query_gate ADD COLUMN protected_edit_grants TEXT NOT NULL DEFAULT '[]'",
                # Per-turn agent-research override (#365, 2026-07-15). SAME
                # cross-process rationale as config_grants above: prompt_mutator
                # (in the hook subprocess) derives it from the operator's prompt;
                # the MCP-server subprocess reads it in the agent-brief gate. The
                # module-level flag in protected_file_runtime never crossed that
                # boundary, so the advertised 'delegate research' override was
                # DEAD in production for every real operator. Its OWN column —
                # axes that mean different things do not share a store.
                "ALTER TABLE session_query_gate ADD COLUMN agent_research_override INTEGER NOT NULL DEFAULT 0",
                # SEC-006 (2026-04-23): grant provenance. JSON dict
                # keyed by tool_name → {source_kind, actor, scope,
                # created_at, expires_at}. Read path stays back-compat
                # (legacy get_user_intent_tools still returns list[str])
                # — this column is ADDITIVE metadata. Legacy writes
                # get source_kind='unknown' when read via the new API.
                # Cleared in sync with user_intent_tools on each
                # turn reset to avoid stale attribution bleeding
                # across prompts.
                "ALTER TABLE session_query_gate ADD COLUMN user_intent_tools_meta TEXT NOT NULL DEFAULT '{}'",
                # SEC-005 (2026-04-23): degraded-state visibility. Flag +
                # reason + timestamp + last failure event id. Set by
                # SEC-002 mutation rollback path; cleared by operator
                # recovery actions (Retry/Reconnect/Clear State in the
                # dashboard). Absent fields stay at schema defaults
                # for legacy rows.
                "ALTER TABLE session_query_gate ADD COLUMN degraded_state INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE session_query_gate ADD COLUMN degraded_reason TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE session_query_gate ADD COLUMN degraded_at TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE session_query_gate ADD COLUMN last_failure_event_id TEXT NOT NULL DEFAULT ''",
                # AIDOCS shell provider lock — Batch A (2026-04-29).
                # Dev-flavor session-scoped override for ai_run shell
                # provider. ONLY honored when distribution.flavor=="dev"
                # (read-side enforced in shell_resolver._read_dev_session_
                # override AND in this store's get_dev_ai_run_bash_path
                # for defense in depth). Validated at write time:
                # absolute path, not under project_root, not cmd.exe,
                # not pwsh.exe / powershell.exe, probe-passes. NEVER
                # repo-file-backed (sqlite-only, /.MEMORY/.index/ is
                # gitignored). NEVER serialized into project config.
                # Audited via dev_ai_run_bash_path_set event on every
                # write attempt (accept and reject). Privilege-relevant
                # — added to _PRIVILEGE_COLUMNS for SEC-001 snapshot/
                # restore coverage.
                "ALTER TABLE session_query_gate ADD COLUMN dev_ai_run_bash_path TEXT NOT NULL DEFAULT ''",
                # #464 (2026-07-18): owned host-session-id chain. JSON list of
                # EVERY host/harness identity axis the authenticated hooks have
                # stamped for this managed session — host session UUIDs (which
                # ROTATE on CLI resume/reopen, so the single-slot
                # last_host_session_id loses the old axis) plus the harness
                # transcript-dir UUID (the id Claude Code keys its
                # <TEMP>/claude/<slug>/<uuid>/tasks/ artifact home by). The
                # session-artifact recognizer matches ownership against this
                # FULL chain, so a session can read its OWN task output after a
                # resume. Carve-out like last_host_session_id (identity stamp,
                # written at PreToolUse from the authenticated hook payload —
                # never from prompt content), NOT in _PRIVILEGE_COLUMNS.
                "ALTER TABLE session_query_gate ADD COLUMN host_session_id_chain TEXT NOT NULL DEFAULT '[]'",
            ):
                try:
                    conn.execute(column_def)
                except Exception:
                    # Already exists — additive migration is idempotent.
                    pass
        self._ingest_all_legacy_json(project_root)

    # ── legacy migration ──

    def _legacy_json_path(self, project_root: Path, session_id: str) -> Path:
        return project_root / ".MEMORY" / "sessions" / session_id / "query-gate.json"

    def _ingest_all_legacy_json(self, project_root: Path) -> None:
        sessions_dir = project_root / ".MEMORY" / "sessions"
        if not sessions_dir.is_dir():
            return
        for session_dir in sessions_dir.iterdir():
            if not session_dir.is_dir():
                continue
            self._ingest_single_legacy_json(project_root, session_dir.name)

    def _ingest_single_legacy_json(self, project_root: Path, session_id: str) -> None:
        path = self._legacy_json_path(project_root, session_id)
        if not path.is_file():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            # Corrupt JSON stays on disk for operator triage; the store
            # falls back to an empty session row so the rest of the
            # project keeps working.
            return
        if not isinstance(raw, dict):
            return
        with self.session(project_root) as conn:
            existing = conn.execute(
                "SELECT 1 FROM session_query_gate WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if existing is not None:
                path.unlink()
                return
            conn.execute(
                """
                INSERT INTO session_query_gate (
                    session_id, last_tool, known_exact_paths,
                    current_lane_id, lane_exact_paths,
                    lane_allowed_tools, lane_extra_tools,
                    lane_raw_tools_granted, user_intent_tools,
                    user_intent_bash_subcommands, turn_edited_files,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    raw.get("last_tool"),
                    json.dumps(self._normalize_list(raw.get("known_exact_paths"))),
                    raw.get("current_lane_id"),
                    json.dumps(self._normalize_list(raw.get("lane_exact_paths"))),
                    json.dumps(self._normalize_list(raw.get("lane_allowed_tools"))),
                    json.dumps(self._normalize_list(raw.get("lane_extra_tools"))),
                    json.dumps(self._normalize_dict_of_lists(raw.get("lane_raw_tools_granted"))),
                    json.dumps(self._normalize_list(raw.get("user_intent_tools"))),
                    json.dumps(self._normalize_list(raw.get("user_intent_bash_subcommands"))),
                    json.dumps(self._normalize_list(raw.get("turn_edited_files"))),
                    raw.get("updated_at"),
                ),
            )
        path.unlink()

    @staticmethod
    def _normalize_list(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(v) for v in value if isinstance(v, str) and v]
        return []

    @staticmethod
    def _normalize_dict_of_lists(value: Any) -> dict[str, list[str]]:
        if not isinstance(value, dict):
            return {}
        cleaned: dict[str, list[str]] = {}
        for key, tools in value.items():
            if not isinstance(key, str) or not key.strip():
                continue
            if not isinstance(tools, list):
                continue
            norm = [str(t).strip().lower() for t in tools if isinstance(t, str) and str(t).strip()]
            if norm:
                cleaned[key.strip()] = norm
        return cleaned

    # ── row helpers ──

    def _ensure_row(self, conn: Any, session_id: str) -> None:
        # Callers that read-only tolerate the "no row" case, but writers
        # need a row present to UPDATE. Use INSERT OR IGNORE so we don't
        # clobber existing state when ensuring.
        conn.execute(
            "INSERT OR IGNORE INTO session_query_gate (session_id) VALUES (?)",
            (session_id,),
        )

    def _read_row(self, conn: Any, session_id: str) -> dict[str, Any] | None:
        row = conn.execute(
            """
            SELECT last_tool, known_exact_paths, current_lane_id,
                   lane_exact_paths, lane_allowed_tools, lane_extra_tools,
                   lane_raw_tools_granted, user_intent_tools,
                   user_intent_bash_subcommands, user_intent_destructive,
                   turn_edited_files, updated_at
              FROM session_query_gate WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "last_tool": row["last_tool"],
            "known_exact_paths": json.loads(row["known_exact_paths"] or "[]"),
            "current_lane_id": row["current_lane_id"],
            "lane_exact_paths": json.loads(row["lane_exact_paths"] or "[]"),
            "lane_allowed_tools": json.loads(row["lane_allowed_tools"] or "[]"),
            "lane_extra_tools": json.loads(row["lane_extra_tools"] or "[]"),
            "lane_raw_tools_granted": json.loads(row["lane_raw_tools_granted"] or "{}"),
            "user_intent_tools": json.loads(row["user_intent_tools"] or "[]"),
            "user_intent_bash_subcommands": json.loads(row["user_intent_bash_subcommands"] or "[]"),
            "user_intent_destructive": row["user_intent_destructive"],
            "turn_edited_files": json.loads(row["turn_edited_files"] or "[]"),
            "updated_at": row["updated_at"],
        }

    def _empty_state(self, session_id: str) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "last_tool": None,
            "known_exact_paths": [],
            "current_lane_id": None,
            "lane_exact_paths": [],
            "lane_allowed_tools": [],
            "lane_extra_tools": [],
            "lane_raw_tools_granted": {},
            "updated_at": None,
        }

    # ── public API mirroring the legacy QueryGateStore ──

    def get(self, project_root: Path, session_id: str) -> dict[str, Any]:
        with self.session(project_root) as conn:
            row = self._read_row(conn, session_id)
        if row is None:
            return self._empty_state(session_id)
        return {
            "session_id": session_id,
            "last_tool": row["last_tool"],
            "known_exact_paths": row["known_exact_paths"],
            "current_lane_id": row["current_lane_id"],
            "lane_exact_paths": row["lane_exact_paths"],
            "lane_allowed_tools": row["lane_allowed_tools"],
            "lane_extra_tools": row["lane_extra_tools"],
            "lane_raw_tools_granted": row["lane_raw_tools_granted"],
            "updated_at": row["updated_at"],
        }

    def get_last_host_session_id(
        self,
        project_root: Path,
        session_id: str,
    ) -> str:
        """Return the last host session id stamped for this session, or "".

        Added 2026-05-07: the per-conductor mapping recovery path needs this
        column, but the general get() omits it (only returns lane state).
        Direct column read keeps the schema honest without fattening get().
        """
        with self.session(project_root) as conn:
            row = conn.execute(
                "SELECT last_host_session_id FROM session_query_gate WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return ""
        return str(row[0] or "").strip()

    # ── #464: owned host-session-id chain ──
    #
    # Every id the caller's session has LEGITIMATELY owned: host session
    # UUIDs stamped by the hooks (they rotate on CLI resume) and the
    # harness transcript-dir UUID (the axis Claude Code keys its
    # task-artifact home by). Append-only with a bounded cap; entries
    # only ever come from authenticated hook stamps for THIS session's
    # row, so a foreign session's uuid can never enter the chain.

    _HOST_ID_CHAIN_CAP = 16

    def record_host_session_ids(
        self,
        project_root: Path,
        session_id: str,
        ids: list[str] | tuple[str, ...],
    ) -> bool:
        """Merge ``ids`` into the session's owned host-id chain.

        Case-insensitive de-dup, insertion order preserved, capped at the
        most recent ``_HOST_ID_CHAIN_CAP`` entries (oldest evicted first).
        Returns True when the stored chain changed. Cheap no-op when every
        id is already present.
        """
        cleaned = [s for s in (str(i or "").strip() for i in ids or []) if s]
        if not cleaned or not session_id:
            return False
        with self.session(project_root) as conn:
            self._ensure_row(conn, session_id)
            row = conn.execute(
                "SELECT host_session_id_chain FROM session_query_gate "
                "WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            try:
                chain = json.loads(str(row[0] or "[]")) if row else []
            except Exception:
                chain = []
            if not isinstance(chain, list):
                chain = []
            chain = [str(c).strip() for c in chain if str(c).strip()]
            seen = {c.lower() for c in chain}
            changed = False
            for cid in cleaned:
                if cid.lower() in seen:
                    continue
                chain.append(cid)
                seen.add(cid.lower())
                changed = True
            if not changed:
                return False
            chain = chain[-self._HOST_ID_CHAIN_CAP :]
            conn.execute(
                "UPDATE session_query_gate SET host_session_id_chain = ? "
                "WHERE session_id = ?",
                (json.dumps(chain), session_id),
            )
            return True

    def get_host_session_id_chain(
        self,
        project_root: Path,
        session_id: str,
    ) -> list[str]:
        """Return the session's owned host-id chain (may be empty)."""
        if not session_id:
            return []
        with self.session(project_root) as conn:
            try:
                row = conn.execute(
                    "SELECT host_session_id_chain FROM session_query_gate "
                    "WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
            except Exception:
                return []
        if row is None:
            return []
        try:
            chain = json.loads(str(row[0] or "[]"))
        except Exception:
            return []
        if not isinstance(chain, list):
            return []
        return [str(c).strip() for c in chain if str(c).strip()]

    _KEEP = object()

    def set(
        self,
        project_root: Path,
        session_id: str,
        *,
        last_tool: str | None = None,
        known_exact_paths: Any = _KEEP,
        current_lane_id: Any = _KEEP,
        lane_exact_paths: Any = _KEEP,
        lane_allowed_tools: Any = _KEEP,
        lane_extra_tools: Any = _KEEP,
        lane_raw_tools_granted: Any = _KEEP,
    ) -> dict[str, Any]:
        # _KEEP preserves the existing column value; anything else
        # (including None) becomes an explicit overwrite. Same semantics
        # as the legacy QueryGateStore so callers don't have to change.
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.session(project_root) as conn:
            self._ensure_row(conn, session_id)
            current = self._read_row(conn, session_id) or {}

            def resolve_list(val: Any, key: str) -> list[str]:
                if val is self._KEEP:
                    return list(current.get(key, []))
                return list(dict.fromkeys(val or []))


            resolved_current_lane = (
                current.get("current_lane_id")
                if current_lane_id is self._KEEP
                else (str(current_lane_id).strip() if current_lane_id else None)
            )
            resolved_lane_raw = (
                dict(current.get("lane_raw_tools_granted", {}))
                if lane_raw_tools_granted is self._KEEP
                else dict(lane_raw_tools_granted or {})
            )

            conn.execute(
                """
                UPDATE session_query_gate SET
                    last_tool = ?,
                    known_exact_paths = ?,
                    current_lane_id = ?,
                    lane_exact_paths = ?,
                    lane_allowed_tools = ?,
                    lane_extra_tools = ?,
                    lane_raw_tools_granted = ?,
                    updated_at = ?
                  WHERE session_id = ?
                """,
                (
                    last_tool,
                    json.dumps(resolve_list(known_exact_paths, "known_exact_paths")),
                    resolved_current_lane,
                    json.dumps(resolve_list(lane_exact_paths, "lane_exact_paths")),
                    json.dumps(resolve_list(lane_allowed_tools, "lane_allowed_tools")),
                    json.dumps(resolve_list(lane_extra_tools, "lane_extra_tools")),
                    json.dumps(resolved_lane_raw),
                    now,
                    session_id,
                ),
            )
        return self.get(project_root, session_id)

    # ── per-turn grants + turn-edited tracker ──

    def set_user_intent_tools(
        self,
        project_root: Path,
        session_id: str,
        tools: list[str],
        *,
        provenance: dict | None = None,
    ) -> None:
        """Write the per-turn user-intent tool list.

        SEC-006 (2026-04-23): accepts an optional provenance dict
        mapping tool_name → {source_kind, actor, scope, created_at,
        expires_at}. Writes alongside the string list into the
        additive user_intent_tools_meta column. Legacy callers (no
        provenance kwarg) keep working; their meta is filtered down
        to {tool: {source_kind: 'unknown'}} on read via the new API.
        When tools list shrinks (or empties), meta entries for the
        dropped tools are removed — revocation must be attributable.

        SEC-007 (2026-04-23): on distribution.flavor='corpo',
        grants with source_kind in {nlp, escalation} start with
        state='pending' and are EXCLUDED from the active
        user_intent_tools list — only confirmation via
        activate_pending_grant flips state to 'active' and adds the
        tool to the active set. Solo/dev flavors keep auto-activate
        behavior for back-compat.
        """
        normalized = [t.lower() for t in tools]
        normalized_set = set(normalized)

        # SEC-007: resolve flavor once. Only 'corpo' triggers the
        # confirmation gate; solo/dev/unknown auto-activate.
        try:
            from .config import get_setting as _get_setting

            flavor = (
                str(
                    _get_setting(
                        "distribution.flavor",
                        project_root=project_root,
                        default="solo",
                    )
                    or "solo",
                )
                .strip()
                .lower()
            )
        except Exception:
            flavor = "solo"
        requires_confirm_sources = {"nlp", "escalation"}

        # Build meta payload. Keep only entries whose tool is still in
        # the new list — any dropped tool loses its provenance, matching
        # "grant revocation is explicit, no lingering attribution."
        # Per-entry state defaults to 'active' except when SEC-007
        # applies.
        meta_payload: dict = {}
        pending_set: set[str] = set()
        if provenance:
            for tool_name, meta in provenance.items():
                key = str(tool_name).strip().lower()
                if key not in normalized_set:
                    continue
                if not isinstance(meta, dict):
                    continue
                source_kind = str(meta.get("source_kind") or "unknown")
                state = "active"
                if flavor == "corpo" and source_kind in requires_confirm_sources:
                    state = "pending"
                    pending_set.add(key)
                meta_payload[key] = {
                    "source_kind": source_kind,
                    "actor": str(meta.get("actor") or ""),
                    "scope": str(meta.get("scope") or "turn"),
                    "created_at": str(meta.get("created_at") or ""),
                    "expires_at": (
                        str(meta["expires_at"]) if meta.get("expires_at") is not None else None
                    ),
                    "state": state,
                }

        # Active set: tools that are either NOT in pending_set (active
        # by default — legacy writes, solo flavor, or corpo+regex/etc)
        # OR explicitly state='active' in meta.
        active_tools = [t for t in normalized if t not in pending_set]

        with self.session(project_root) as conn:
            self._ensure_row(conn, session_id)
            conn.execute(
                "UPDATE session_query_gate SET "
                "user_intent_tools = ?, user_intent_tools_meta = ? "
                "WHERE session_id = ?",
                (
                    json.dumps(active_tools),
                    json.dumps(meta_payload),
                    session_id,
                ),
            )

    def activate_pending_grant(
        self,
        project_root: Path,
        session_id: str,
        tool_name: str,
    ) -> None:
        """SEC-007: flip a pending grant to state='active' and add it
        to the active tool list. No-op when the grant doesn't exist
        (operator may have denied between the detection and the
        confirmation yes). Never raises on unknown tools — audit
        chain captures attempted operations separately.
        """
        key = str(tool_name).strip().lower()
        if not key:
            return
        with self.session(project_root) as conn:
            row = conn.execute(
                "SELECT user_intent_tools, user_intent_tools_meta "
                "FROM session_query_gate WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return
            try:
                meta = json.loads(row["user_intent_tools_meta"] or "{}")
            except Exception:
                meta = {}
            if not isinstance(meta, dict) or key not in meta:
                return
            entry = meta[key]
            if not isinstance(entry, dict):
                return
            if entry.get("state") != "pending":
                # already active or some other state — no-op
                return
            entry["state"] = "active"
            meta[key] = entry
            try:
                active = json.loads(row["user_intent_tools"] or "[]")
            except Exception:
                active = []
            if key not in active:
                active.append(key)
            conn.execute(
                "UPDATE session_query_gate SET "
                "user_intent_tools = ?, user_intent_tools_meta = ? "
                "WHERE session_id = ?",
                (
                    json.dumps(sorted(active)),
                    json.dumps(meta),
                    session_id,
                ),
            )

    def deny_pending_grant(
        self,
        project_root: Path,
        session_id: str,
        tool_name: str,
    ) -> None:
        """SEC-007: drop a pending grant entirely. Removes both the
        meta entry and any stale active-list entry, so a later
        accidental confirmation can't resurrect a denied grant.
        """
        key = str(tool_name).strip().lower()
        if not key:
            return
        with self.session(project_root) as conn:
            row = conn.execute(
                "SELECT user_intent_tools, user_intent_tools_meta "
                "FROM session_query_gate WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return
            try:
                meta = json.loads(row["user_intent_tools_meta"] or "{}")
            except Exception:
                meta = {}
            if isinstance(meta, dict) and key in meta:
                meta.pop(key, None)
            try:
                active = json.loads(row["user_intent_tools"] or "[]")
            except Exception:
                active = []
            active = [t for t in active if t != key]
            conn.execute(
                "UPDATE session_query_gate SET "
                "user_intent_tools = ?, user_intent_tools_meta = ? "
                "WHERE session_id = ?",
                (
                    json.dumps(active),
                    json.dumps(meta),
                    session_id,
                ),
            )

    # ── SEC-005 degraded-state signaling (2026-04-23) ──

    def _audit_state_change(
        self,
        project_root: Path,
        session_id: str,
        *,
        event_kind: str,
        payload: dict[str, object] | None = None,
    ) -> None:
        """#136: emit a lightweight audit event for a SECURITY-STATE
        session_query_gate mutation (degraded-state, reconnect-gate clear,
        privilege restore) that was previously invisible to the audit chain.
        Best-effort — never blocks the write. Deliberately NOT applied to the
        grant sinks (already audited via the query_gate facade + sticky_grants
        store) nor to hot-path ephemeral updates (known_exact_paths /
        turn/generation counters / compaction stamp / the #151 host-id bridge),
        which fire per-UPS/per-read and would flood the ledger."""
        try:
            from .execution_index_store import ExecutionIndexStore

            ExecutionIndexStore().record_event(
                project_root,
                event_kind=event_kind,
                source_kind="session_query_gate_store",
                session_id=session_id or None,
                action_kind="state_change",
                target_entity=session_id,
                status="updated",
                payload={"session_id": session_id, **(payload or {})},
            )
        except Exception:
            pass

    def set_degraded_state(
        self,
        project_root: Path,
        session_id: str,
        *,
        reason: str,
        failure_event_id: str = "",
    ) -> None:
        """Flip the session into degraded state. Latest call wins —
        reason/timestamp/event_id overwrite any prior degraded snapshot.
        Dashboard surfaces a red badge while this is set; operator
        clears via recovery actions (Retry/Reconnect/Clear State).
        """
        now = self._timestamp()
        with self.session(project_root) as conn:
            self._ensure_row(conn, session_id)
            conn.execute(
                "UPDATE session_query_gate SET "
                "degraded_state = 1, degraded_reason = ?, "
                "degraded_at = ?, last_failure_event_id = ? "
                "WHERE session_id = ?",
                (str(reason or ""), now, str(failure_event_id or ""), session_id),
            )
        self._audit_state_change(
            project_root,
            session_id,
            event_kind="session.degraded_set",
            payload={"reason": str(reason or ""), "failure_event_id": str(failure_event_id or "")},
        )

    def clear_degraded_state(
        self,
        project_root: Path,
        session_id: str,
    ) -> None:
        """Reset degraded state back to clean. Wipes reason + event
        id too so the dashboard doesn't show stale failure text after
        recovery.
        """
        with self.session(project_root) as conn:
            self._ensure_row(conn, session_id)
            conn.execute(
                "UPDATE session_query_gate SET "
                "degraded_state = 0, degraded_reason = '', "
                "degraded_at = '', last_failure_event_id = '' "
                "WHERE session_id = ?",
                (session_id,),
            )
        self._audit_state_change(
            project_root, session_id, event_kind="session.degraded_cleared"
        )

    def get_degraded_state(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict:
        """Return a dict describing current degraded state. Always
        returns a dict (never None) so the dashboard has a stable
        shape — degraded=False + empty fields is the clean state.
        """
        with self.session(project_root) as conn:
            row = conn.execute(
                "SELECT degraded_state, degraded_reason, degraded_at, "
                "last_failure_event_id FROM session_query_gate "
                "WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return {
                "degraded": False,
                "reason": "",
                "degraded_at": "",
                "last_failure_event_id": "",
            }
        return {
            "degraded": bool(row["degraded_state"]),
            "reason": str(row["degraded_reason"] or ""),
            "degraded_at": str(row["degraded_at"] or ""),
            "last_failure_event_id": str(row["last_failure_event_id"] or ""),
        }

    def get_user_intent_tools_meta(self, project_root: Path, session_id: str) -> dict:
        """SEC-006 read-side helper. Returns the stored provenance
        dict. Tools present in user_intent_tools but missing from
        the meta dict are synthesized with source_kind='unknown' so
        callers always get a complete mapping.
        """
        with self.session(project_root) as conn:
            row = conn.execute(
                "SELECT user_intent_tools, user_intent_tools_meta "
                "FROM session_query_gate WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return {}
        try:
            tools = json.loads(row["user_intent_tools"] or "[]")
        except Exception:
            tools = []
        try:
            meta = json.loads(row["user_intent_tools_meta"] or "{}")
        except Exception:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        # Synthesize unknown-source entries for legacy rows. Always
        # include 'state' in the returned dict (defaults to 'active'
        # for legacy entries — they already gate-checked fine pre-
        # SEC-007, so flipping them to pending now would break flows).
        # Also expose pending entries that exist in meta but aren't
        # in the active-tools list (so confirmation UI can see them).
        result: dict = {}
        for t in tools:
            key = str(t).strip().lower()
            if key in meta and isinstance(meta[key], dict):
                entry = dict(meta[key])
                entry.setdefault("state", "active")
                result[key] = entry
            else:
                result[key] = {
                    "source_kind": "unknown",
                    "actor": "",
                    "scope": "turn",
                    "created_at": "",
                    "expires_at": None,
                    "state": "active",
                }
        # Pending entries (not in the active tools list) still surface.
        for key, entry in meta.items():
            if not isinstance(entry, dict):
                continue
            if key in result:
                continue
            pending_entry = dict(entry)
            pending_entry.setdefault("state", "pending")
            result[key] = pending_entry
        return result

    def set_current_task_id(
        self,
        project_root: Path,
        session_id: str,
        task_id: str,
    ) -> None:
        """Set the active task id for audit stamping.

        Pass empty string to clear (task_complete does that). Cheap
        writer — hot-path on every task_begin / task_complete and a
        once-per-session cost for every other gate write.
        """
        with self.session(project_root) as conn:
            self._ensure_row(conn, session_id)
            conn.execute(
                "UPDATE session_query_gate SET current_task_id = ? WHERE session_id = ?",
                (str(task_id or ""), session_id),
            )

    def get_current_task_id(
        self,
        project_root: Path,
        session_id: str,
    ) -> str:
        """Read the active task id. Empty string = no task active.
        Callers treat empty identically to 'no audit linkage available'.
        """
        with self.session(project_root) as conn:
            row = conn.execute(
                "SELECT current_task_id FROM session_query_gate WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return ""
        return str(row["current_task_id"] or "")

    def get_user_intent_tools(self, project_root: Path, session_id: str) -> list[str]:
        with self.session(project_root) as conn:
            row = self._read_row(conn, session_id)
        if row is None:
            return []
        return list(row["user_intent_tools"])

    def set_user_intent_bash_subcommands(
        self,
        project_root: Path,
        session_id: str,
        subcommands: list[str],
    ) -> None:
        with self.session(project_root) as conn:
            self._ensure_row(conn, session_id)
            conn.execute(
                "UPDATE session_query_gate SET user_intent_bash_subcommands = ? "
                "WHERE session_id = ?",
                (json.dumps([s.lower() for s in subcommands]), session_id),
            )

    def get_user_intent_bash_subcommands(self, project_root: Path, session_id: str) -> list[str]:
        with self.session(project_root) as conn:
            row = self._read_row(conn, session_id)
        if row is None:
            return []
        return list(row["user_intent_bash_subcommands"])

    # ── AIDOCS shell provider lock — dev session override ─────────────
    # See .MEMORY/system/security-gates.md invariant "AIDOCS shell
    # provider lock" + §6 + §7A. Setter validates path before storing
    # and audits every attempt (accept and reject). Getter enforces
    # read-side flavor gate as defense in depth.

    def set_dev_ai_run_bash_path(
        self,
        project_root: Path,
        session_id: str,
        path: str,
        *,
        actor: str = "",
    ) -> dict[str, object]:
        """Set the dev-flavor session-scoped ai_run shell override.

        Validates before storing:
          - distribution.flavor must be "dev"
          - path must be absolute
          - canonical path must NOT be under project_root
          - basename must NOT be cmd.exe (any case)
          - basename must NOT be pwsh.exe / powershell.exe
          - probe (--version + 'printf aidocs-ok' sentinel) must pass

        Empty string is a valid input — clears the override.

        Emits dev_ai_run_bash_path_set audit event regardless of
        outcome. On rejection, the column is NOT written.

        Returns dict {ok: bool, validation_result: str, path: str}.
        """
        from .execution_index_store import ExecutionIndexStore

        execution = ExecutionIndexStore()
        normalized = (path or "").strip()
        validation_result = "accepted"
        ok = True
        rejection_reason = ""

        # Empty is a valid clear operation — skip validation.
        if normalized:
            # #404: the dev-flavor override surface is retired — every
            # non-empty write is refused; only clears ("") are accepted.
            ok = False
            rejection_reason = (
                "dev_ai_run_bash_path is removed (#404); only '' (clear) is accepted"
            )
            validation_result = "rejected_removed"

        # Audit BEFORE write so a sqlite write failure can't suppress
        # the audit row. Empty path (clear) audits with
        # validation_result="accepted" and path="".
        try:
            execution.record_event(
                project_root,
                event_kind="dev_ai_run_bash_path_set",
                source_kind="session_query_gate_store",
                session_id=session_id,
                capability_name="set_dev_ai_run_bash_path",
                action_kind="config_set",
                target_entity=normalized[:200] if normalized else "",
                status="allowed" if ok else "blocked",
                payload={
                    "session_id": session_id,
                    "path": normalized,
                    "actor": actor,
                    "validation_result": validation_result,
                    "rejection_reason": rejection_reason,
                },
            )
        except Exception:
            # Audit failure must not block the write decision —
            # better to write and miss the audit than refuse on
            # observability failure. But we DO refuse on the
            # validation/probe path above; this catch covers
            # only emit-time exceptions.
            pass

        if not ok:
            return {
                "ok": False,
                "validation_result": validation_result,
                "rejection_reason": rejection_reason,
                "path": normalized,
            }

        with self.session(project_root) as conn:
            self._ensure_row(conn, session_id)
            conn.execute(
                "UPDATE session_query_gate SET dev_ai_run_bash_path = ? WHERE session_id = ?",
                (normalized, session_id),
            )

        return {
            "ok": True,
            "validation_result": validation_result,
            "path": normalized,
        }

    def get_dev_ai_run_bash_path(
        self,
        project_root: Path,
        session_id: str,
    ) -> str:
        """#404: the dev-flavor session shell override is retired.

        Always returns "" — the stored column (if any legacy row
        populated it) is never honored.
        """
        del project_root, session_id
        return ""

    # set_forced_work / get_forced_work REMOVED 2026-04-30 (autowake
    # removal). Underlying forced_work_active column kept in storage
    # for migration safety but no longer read or written.

    # ── Ask-state confirmations (judge-override with 1-turn TTL) ──

    def increment_turn_counter(self, project_root: Path, session_id: str) -> int:
        """Advance the session turn counter by one and return new value.

        Called from UserPromptSubmit so every operator utterance is a
        fresh turn. Used by ask-state TTL: any pending_confirmation whose
        turn_at_create < current counter is expired and auto-cleared.
        """
        with self.session(project_root) as conn:
            self._ensure_row(conn, session_id)
            row = conn.execute(
                "SELECT session_turn_counter FROM session_query_gate WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            new_val = int(row["session_turn_counter"]) + 1 if row else 1
            conn.execute(
                "UPDATE session_query_gate SET session_turn_counter = ? WHERE session_id = ?",
                (new_val, session_id),
            )
        return new_val

    # ── Causal turn id (#441) ─────────────────────────────────────

    def rotate_current_turn_id(
        self,
        project_root: Path,
        session_id: str,
        *,
        instruction_content_hash: str = "",
        actor_id: str = "",
        actor_role: str = "",
        origin_channel: str = "user_prompt_submit",
    ) -> str:
        """Mint a fresh SERVER-GENERATED causal turn id and store it.

        Called from UserPromptSubmit for operator-authored, authority-
        bearing prompts only (see hook_pipeline). The id is minted HERE —
        no caller-supplied value is ever accepted (causal-turn invariant:
        audit correlation identifiers are server-generated; caller-
        supplied correlation IDs alone are never trusted).

        #467: the mint also opens the causal TURN entity (causal_turns row
        in state Open + the revision-1 UserPrompt instruction event carrying
        the prompt's ``instruction_content_hash`` — the prompt as a first-
        class audit event whose identity is provable while its body stays
        protectable) and abandons the session's superseded prior turn.
        Rides the same one-write-per-operator-turn budget (one extra
        transaction on the same DB, per operator prompt only). BEST-EFFORT
        like the mint itself: a causal-store failure leaves the minted id
        in place — the tool chokepoint's intent audit remains the
        fail-closed layer. (Mint fail-direction is an OPEN Empire question
        carried from War AM; this preserves current behavior.)
        """
        from uuid import uuid4

        new_turn_id = f"turn-{uuid4().hex}"
        with self.session(project_root) as conn:
            self._ensure_row(conn, session_id)
            conn.execute(
                "UPDATE session_query_gate SET current_turn_id = ? WHERE session_id = ?",
                (new_turn_id, session_id),
            )
        try:
            from .causal_turn_store import CausalTurnStore

            CausalTurnStore().open_turn(
                project_root,
                session_id,
                new_turn_id,
                content_hash=instruction_content_hash,
                actor_id=actor_id,
                actor_role=actor_role,
                origin_channel=origin_channel,
            )
        except Exception:
            # Best-effort by current doctrine (see docstring): the minted
            # turn id stands; events still bind to it via v5 even when the
            # turn entity row is missing (instruction fields degrade to '').
            pass
        return new_turn_id

    def get_current_turn_id(self, project_root: Path, session_id: str) -> str:
        """Read the current causal turn id ('' when no turn minted yet)."""
        with self.session(project_root) as conn:
            try:
                row = conn.execute(
                    "SELECT current_turn_id FROM session_query_gate WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
            except Exception:
                return ""
        return str(row["current_turn_id"] or "") if row else ""

    def bump_grants_generation(self, project_root: Path, session_id: str) -> int:
        """Advance grants_generation by one; return the new value.

        Called whenever sticky NLP tool grants change. The MCP server's
        call_tool wrapper diffs this against its in-process "last synced"
        counter to decide if it needs to re-read sqlite grants and flip
        in-process enable state (FastMCP emits list_changed from enable).
        """
        with self.session(project_root) as conn:
            self._ensure_row(conn, session_id)
            row = conn.execute(
                "SELECT grants_generation FROM session_query_gate WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            new_val = int(row["grants_generation"]) + 1 if row else 1
            conn.execute(
                "UPDATE session_query_gate SET grants_generation = ? WHERE session_id = ?",
                (new_val, session_id),
            )
        return new_val

    def get_grants_generation(self, project_root: Path, session_id: str) -> int:
        """Read the current grants generation without mutating."""
        with self.session(project_root) as conn:
            self._ensure_row(conn, session_id)
            row = conn.execute(
                "SELECT grants_generation FROM session_query_gate WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return int(row["grants_generation"]) if row else 0

    def get_turn_counter(self, project_root: Path, session_id: str) -> int:
        with self.session(project_root) as conn:
            try:
                row = conn.execute(
                    "SELECT session_turn_counter FROM session_query_gate WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
            except Exception:
                return 0
        return int(row["session_turn_counter"]) if row else 0

    # stamp_autowake / get_last_autowake REMOVED 2026-04-30 (autowake
    # removal). Underlying last_autowake_at column kept in storage for
    # migration safety but no longer read or written.

    # stamp_compaction / get_last_compaction were REMOVED 2026-07-12. They
    # wrote/read last_compaction_at solely for the force-wakeup guard's +120s
    # post-compaction grace window — the pre-goal forced-work workaround, which
    # was deleted (#81). With no reader, the stamp was a dead write. The
    # last_compaction_at column is retained (schema/migration safety, mirrors
    # last_autowake_at) but AIDOCS no longer drives ScheduleWakeup looping.

    # ── Config-set grants (2026-04-21) ──

    def set_agent_research_override(
        self,
        project_root: Path,
        session_id: str,
        granted: bool,
    ) -> None:
        """Per-turn flag: may a sub-agent dispatch brief carry research/inspection
        language THIS turn? Set from the OPERATOR'S PROMPT by prompt_mutator on
        UserPromptSubmit. MUST live in sqlite (not protected_file_runtime's
        module-level dict) because the hook process writes it and the MCP-server
        process reads it — the module dict never crossed that boundary, which is
        why the 'delegate research' override never fired in production (#365).
        Re-set every turn (False when no phrase), so it evaporates with the turn.
        """
        with self.session(project_root) as conn:
            self._ensure_row(conn, session_id)
            conn.execute(
                "UPDATE session_query_gate SET agent_research_override = ? WHERE session_id = ?",
                (1 if granted else 0, session_id),
            )

    def get_agent_research_override(
        self,
        project_root: Path,
        session_id: str,
    ) -> bool:
        """Read the per-turn agent-research override. False when unset/absent."""
        with self.session(project_root) as conn:
            try:
                row = conn.execute(
                    "SELECT agent_research_override FROM session_query_gate WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
            except Exception:
                return False
        if row is None:
            return False
        try:
            return bool(int(row["agent_research_override"] or 0))
        except (TypeError, ValueError):
            return False

    def set_config_grants(
        self,
        project_root: Path,
        session_id: str,
        grants: dict[str, object],
    ) -> None:
        """Replace the per-turn config_set grant map. Called from the
        UPS path after canonical_intent_registry.detect_config_grants_v2
        parses the prompt. Empty dict clears the override (the common case).
        """
        clean = {str(k).strip(): v for k, v in (grants or {}).items() if str(k).strip()}
        with self.session(project_root) as conn:
            self._ensure_row(conn, session_id)
            conn.execute(
                "UPDATE session_query_gate SET config_grants = ? WHERE session_id = ?",
                (json.dumps(clean, default=str), session_id),
            )

    def get_config_grants(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, object]:
        """Read the per-turn config_set grant map. Empty dict when the
        prompt had no grant phrase.
        """
        with self.session(project_root) as conn:
            try:
                row = conn.execute(
                    "SELECT config_grants FROM session_query_gate WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
            except Exception:
                return {}
        if row is None:
            return {}
        raw = row["config_grants"] or "{}"
        try:
            parsed = json.loads(raw)
        except Exception:
            return {}
        if isinstance(parsed, dict):
            return parsed
        return {}

    # ── DNT grant axes (2026-05-12) — cross-process via sqlite ──
    #
    # claude_hook writes from CC's hook subprocess on UserPromptSubmit;
    # ai_protect / file_ops read from the MCP server subprocess. Each
    # axis is a JSON list of normalized relative paths. Empty list
    # is the common case (no grant phrase in the current prompt).

    def _set_path_list(
        self,
        project_root: Path,
        session_id: str,
        column: str,
        paths: list[str],
    ) -> None:
        clean = [str(p).replace("\\", "/").strip() for p in (paths or []) if str(p or "").strip()]
        with self.session(project_root) as conn:
            self._ensure_row(conn, session_id)
            conn.execute(
                f"UPDATE session_query_gate SET {column} = ? WHERE session_id = ?",
                (json.dumps(clean), session_id),
            )

    def _get_path_list(
        self,
        project_root: Path,
        session_id: str,
        column: str,
    ) -> list[str]:
        with self.session(project_root) as conn:
            try:
                row = conn.execute(
                    f"SELECT {column} FROM session_query_gate WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
            except Exception:
                return []
        if row is None:
            return []
        raw = row[column] or "[]"
        try:
            parsed = json.loads(raw)
        except Exception:
            return []
        return [str(p) for p in parsed] if isinstance(parsed, list) else []

    def set_protect_grants(
        self,
        project_root: Path,
        session_id: str,
        paths: list[str],
    ) -> None:
        self._set_path_list(project_root, session_id, "protect_grants", paths)

    def get_protect_grants(
        self,
        project_root: Path,
        session_id: str,
    ) -> list[str]:
        return self._get_path_list(project_root, session_id, "protect_grants")

    def set_unprotect_grants(
        self,
        project_root: Path,
        session_id: str,
        paths: list[str],
    ) -> None:
        self._set_path_list(
            project_root,
            session_id,
            "unprotect_grants",
            paths,
        )

    def get_unprotect_grants(
        self,
        project_root: Path,
        session_id: str,
    ) -> list[str]:
        return self._get_path_list(
            project_root,
            session_id,
            "unprotect_grants",
        )

    def set_protected_edit_grants(
        self,
        project_root: Path,
        session_id: str,
        paths: list[str],
    ) -> None:
        self._set_path_list(
            project_root,
            session_id,
            "protected_edit_grants",
            paths,
        )

    def get_protected_edit_grants(
        self,
        project_root: Path,
        session_id: str,
    ) -> list[str]:
        return self._get_path_list(
            project_root,
            session_id,
            "protected_edit_grants",
        )

    # ── Fresh-CLI detection (2026-04-21) ──

    def check_and_update_host_session_id(
        self,
        project_root: Path,
        session_id: str,
        host_session_id: str,
    ) -> bool:
        """Compare `host_session_id` to the stored value for this session.

        Returns True when the host session changed (fresh launch
        detected), False when it matched (continuation). On a change:
          * Stamp the new id.
          * Clear known_exact_paths (reads the agent auto-inherited
            from a prior host process must be re-discovered).
          * Raise requires_reconnect=1 so the pre-tool gate refuses
            every tool except session_connect until the agent re-binds.
        Same-id call: no-op, returns False.

        Renamed 2026-05-01 from check_and_update_cli_session_id to match
        the agent_memory_epoch.py identity contract — host_session_id is
        the canonical name. Column renamed to `last_host_session_id`
        in the #151 migration (init_db guarded RENAME COLUMN,
        2026-07-12).
        """
        cli = (host_session_id or "").strip()
        if not cli:
            return False
        # First pass (read-only): determine whether the host id changed
        # and what the current lane binding is. We close this
        # connection before the live-worker probe so the probe can
        # safely open its own sqlite connection without contending
        # with an open write transaction on the same db file.
        with self.session(project_root) as conn:
            self._ensure_row(conn, session_id)
            row = conn.execute(
                "SELECT last_host_session_id, current_lane_id "
                "FROM session_query_gate WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            previous = str(row["last_host_session_id"]) if row else ""
            stale_lane = str((row["current_lane_id"] if row else "") or "").strip()
        # #464: the chain keeps every id this session has owned — the
        # single-slot last_host_session_id overwrite below would otherwise
        # LOSE the pre-rotation uuid the harness may still key task
        # artifacts by. Recorded on both branches (legacy rows have an
        # empty chain even for an unchanged id).
        try:
            self.record_host_session_ids(project_root, session_id, [cli])
        except Exception:
            pass
        if previous == cli:
            return False
        # Fresh CLI process. If a lane is still bound, check whether
        # any live worker row owns it; if not, the binding is stale
        # from the previous CLI and must be cleared so the new
        # process doesn't inherit a dead worker's lane scope.
        clear_lane = False
        if stale_lane:
            try:
                from .session_lane_agents_store import (
                    SessionLaneAgentsStore,
                )

                live = (
                    SessionLaneAgentsStore().get_lane_agents(
                        project_root,
                        session_id=session_id,
                        state_filter="running",
                    )
                    or []
                )
                clear_lane = not any(
                    str(w.get("lane_id") or "").strip() == stale_lane for w in live
                )
            except Exception:
                clear_lane = False
        with self.session(project_root) as conn:
            if clear_lane:
                conn.execute(
                    "UPDATE session_query_gate SET "
                    "last_host_session_id = ?, "
                    "known_exact_paths = '[]', "
                    "requires_reconnect = 1, "
                    "current_lane_id = NULL, "
                    "lane_exact_paths = '[]' "
                    "WHERE session_id = ?",
                    (cli, session_id),
                )
            else:
                conn.execute(
                    "UPDATE session_query_gate SET "
                    "last_host_session_id = ?, "
                    "known_exact_paths = '[]', "
                    "requires_reconnect = 1 "
                    "WHERE session_id = ?",
                    (cli, session_id),
                )
        return True

    def get_requires_reconnect(self, project_root: Path, session_id: str) -> bool:
        with self.session(project_root) as conn:
            try:
                row = conn.execute(
                    "SELECT requires_reconnect FROM session_query_gate WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
            except Exception:
                return False
        if row is None:
            return False
        return bool(row["requires_reconnect"])

    def clear_requires_reconnect(self, project_root: Path, session_id: str) -> None:
        with self.session(project_root) as conn:
            self._ensure_row(conn, session_id)
            conn.execute(
                "UPDATE session_query_gate SET requires_reconnect = 0 WHERE session_id = ?",
                (session_id,),
            )
        self._audit_state_change(
            project_root, session_id, event_kind="session.reconnect_cleared"
        )

    # ── SEC-001 hotfix: snapshot/restore (2026-04-23) ──
    #
    # Temporary containment for A1 (blocked prompt mutates privilege
    # state). Covers the privilege-relevant columns only; audit logs
    # and last_host_session_id are NOT restored because they're
    # carve-outs per the SEC-001 spec.
    #
    # Replaced by the full SEC-001 refactor (plan-before-apply) once
    # that lands. Snapshot captures the current value and restore
    # writes it back verbatim — no diff logic, no partial restore.

    # ─── AIDOCS-SEC INVARIANT (enforcement surface) ────────────────
    # Columns restored by the SEC-001 hotfix on a blocked prompt.
    # forced_work_active is kept in this list defensively even though
    # the autowake feature was removed 2026-04-30 — column still
    # exists in schema for migration safety, so a blocked prompt
    # writing to it (no current code path does, but defense in depth)
    # would still be rolled back.
    # sticky_user_intent_tools lives in a sidecar JSON not the table —
    # sidecar restore is handled by query_gate.restore_privilege_state.
    #
    # WHEN ADDING A NEW PRIVILEGE-RELEVANT COLUMN:
    # Add it here too. Otherwise a blocked prompt can mutate your new
    # column and the snapshot/restore path won't catch it. A test in
    # test_prompt_mutation_plan.py::TestBlockedPromptZeroDelta should
    # assert the new column is unchanged after a policy-blocked prompt.
    # ───────────────────────────────────────────────────────────────
    _PRIVILEGE_COLUMNS: tuple[str, ...] = (
        "user_intent_tools",
        "user_intent_bash_subcommands",
        "user_intent_credentials",
        "forced_work_active",
        "pending_confirmation",
        "last_confirmed_operation",
        "config_grants",
        "agent_research_override",
        "current_lane_id",
        "lane_exact_paths",
        "lane_raw_tools_granted",
        "lane_eager_tools_granted",
        "turn_edited_files",
        # AIDOCS shell provider lock (2026-04-29): the dev-flavor
        # session-scoped ai_run shell override is privilege-relevant
        # — it controls which executable runs shell commands. A
        # blocked or unmanaged prompt MUST NOT mutate it.
        "dev_ai_run_bash_path",
    )

    _PROMPT_SUBMIT_COLUMNS: tuple[str, ...] = (
        *_PRIVILEGE_COLUMNS,
        "user_intent_tools_meta",
        "user_intent_destructive",
        "session_turn_counter",
        "grants_generation",
        "protect_grants",
        "unprotect_grants",
        "protected_edit_grants",
        "plan_mode_active",
        "plan_mode_scope",
        "plan_mode_started_at",
        "plan_mode_last_activity_at",
        "current_task_id",
    )


    def delete_prompt_submit_state(
        self,
        project_root: Path,
        session_id: str,
    ) -> None:
        """Delete authority state for a session created within this submit."""
        with self.session(project_root) as conn:
            conn.execute(
                "DELETE FROM session_query_gate WHERE session_id = ?",
                (session_id,),
            )


    def snapshot_prompt_submit_state(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, object]:
        """Explicit prompt-submit snapshot.

        Unlike the SEC-001 compatibility helper, absence and capture failure
        are never conflated: storage errors raise, while an absent row returns
        captured=True/existed=False so rollback can remove a row created by
        this submit.
        """
        self.init_db(project_root)
        cols = ", ".join(self._PROMPT_SUBMIT_COLUMNS)
        with self.session(project_root) as conn:
            row = conn.execute(
                f"SELECT {cols} FROM session_query_gate WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return {
            "captured": True,
            "existed": row is not None,
            "state": (
                {col: row[col] for col in self._PROMPT_SUBMIT_COLUMNS}
                if row is not None
                else {}
            ),
        }

    def restore_prompt_submit_state(
        self,
        project_root: Path,
        session_id: str,
        snapshot: dict[str, object],
    ) -> None:
        """Restore exactly the scoped authority row captured for one submit."""
        if snapshot.get("captured") is not True:
            raise ValueError("query-gate prompt-submit snapshot was not captured")
        existed = bool(snapshot.get("existed"))
        raw_state = snapshot.get("state")
        state = dict(raw_state) if isinstance(raw_state, dict) else {}
        with self.session(project_root) as conn:
            if not existed:
                conn.execute(
                    "DELETE FROM session_query_gate WHERE session_id = ?",
                    (session_id,),
                )
                return
            missing = [col for col in self._PROMPT_SUBMIT_COLUMNS if col not in state]
            if missing:
                raise ValueError(
                    "query-gate prompt-submit snapshot missing columns: "
                    + ", ".join(missing)
                )
            self._ensure_row(conn, session_id)
            set_clause = ", ".join(f"{col} = ?" for col in self._PROMPT_SUBMIT_COLUMNS)
            conn.execute(
                f"UPDATE session_query_gate SET {set_clause} WHERE session_id = ?",
                (*(state[col] for col in self._PROMPT_SUBMIT_COLUMNS), session_id),
            )


    def snapshot_privilege_state(self, project_root: Path, session_id: str) -> dict[str, object]:
        """Capture privilege-relevant columns as a plain dict. Returns
        empty dict when the row doesn't exist yet (caller treats that
        as 'nothing to restore').
        """
        cols = ", ".join(self._PRIVILEGE_COLUMNS)
        with self.session(project_root) as conn:
            try:
                row = conn.execute(
                    f"SELECT {cols} FROM session_query_gate WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
            except Exception:
                return {}
        if row is None:
            return {}
        return {col: row[col] for col in self._PRIVILEGE_COLUMNS}

    def restore_privilege_state(
        self,
        project_root: Path,
        session_id: str,
        snapshot: dict[str, object],
    ) -> None:
        """Write privilege columns back verbatim. No-op on empty
        snapshot (row didn't exist at snapshot time — don't resurrect
        it). Best-effort: individual column writes use the same
        connection so either all land or none.
        """
        if not snapshot:
            return
        set_clause = ", ".join(f"{col} = ?" for col in self._PRIVILEGE_COLUMNS if col in snapshot)
        if not set_clause:
            return
        params = tuple(snapshot[col] for col in self._PRIVILEGE_COLUMNS if col in snapshot)
        _restored = [col for col in self._PRIVILEGE_COLUMNS if col in snapshot]
        with self.session(project_root) as conn:
            self._ensure_row(conn, session_id)
            conn.execute(
                f"UPDATE session_query_gate SET {set_clause} WHERE session_id = ?",
                (*params, session_id),
            )
        self._audit_state_change(
            project_root,
            session_id,
            event_kind="session.privilege_restored",
            payload={"columns": _restored},
        )

    # ── User-intent credentials (2026-04-21) ──
    #
    # When an operator pastes a provider credential into chat, the
    # token is stashed here verbatim. Subsequent PreToolUse checks that
    # would hard-block on the judge's FILE_*_KEY verdict are downgraded
    # to an ask-state confirm IFF the matched token is in this list.
    # TTL: set_user_intent_credentials replaces the list wholesale on
    # each new UserPromptSubmit — only the CURRENT prompt's tokens
    # grant override. No cross-turn leakage.

    def set_user_intent_credentials(
        self,
        project_root: Path,
        session_id: str,
        tokens: list[str],
    ) -> None:
        """Replace the session's user-intent credential set. Called
        from UserPromptSubmit after scanning the prompt. Empty list
        clears the override (typical case — most prompts have no creds).
        """
        clean = [str(t) for t in tokens if isinstance(t, str) and t.strip()]
        with self.session(project_root) as conn:
            self._ensure_row(conn, session_id)
            conn.execute(
                "UPDATE session_query_gate SET user_intent_credentials = ? WHERE session_id = ?",
                (json.dumps(clean), session_id),
            )

    def set_user_intent_destructive(
        self,
        project_root: Path,
        session_id: str,
        tokens: list[str],
    ) -> None:
        """Replace the session's user-intent destructive-tokens list.
        Turn-scoped — claude_hook clears on every UserPromptSubmit by
        rewriting with the current prompt's matches (or []). Phase 4
        of backlog #15.
        """
        clean = [str(t).strip().lower() for t in tokens if isinstance(t, str) and t.strip()]
        with self.session(project_root) as conn:
            self._ensure_row(conn, session_id)
            conn.execute(
                "UPDATE session_query_gate SET user_intent_destructive = ? WHERE session_id = ?",
                (json.dumps(clean), session_id),
            )

    def get_user_intent_destructive(
        self,
        project_root: Path,
        session_id: str,
    ) -> list[str]:
        with self.session(project_root) as conn:
            row = self._read_row(conn, session_id)
        if row is None:
            return []
        try:
            return list(json.loads(row["user_intent_destructive"] or "[]"))
        except (ValueError, TypeError):
            return []

    def get_user_intent_credentials(self, project_root: Path, session_id: str) -> list[str]:
        """Return the current user-intent credential set (possibly empty)."""
        with self.session(project_root) as conn:
            try:
                row = conn.execute(
                    "SELECT user_intent_credentials FROM session_query_gate WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
            except Exception:
                return []
        if row is None:
            return []
        raw = row["user_intent_credentials"] or "[]"
        try:
            parsed = json.loads(raw)
        except Exception:
            return []
        return [str(t) for t in parsed if isinstance(t, str)]

    def set_pending_confirmation(
        self,
        project_root: Path,
        session_id: str,
        confirmation: dict[str, Any] | None,
    ) -> None:
        """Write or clear the session's single pending confirmation row.

        Pass None to clear. Structure expected:
          {
            "id": str,
            "command_sha": str,
            "question": str,
            "intent_id": str,
            "intent_scope": dict,
            "proposed_action": str,
            "created_at": iso8601,
            "turn_at_create": int,
          }
        """
        with self.session(project_root) as conn:
            self._ensure_row(conn, session_id)
            payload = json.dumps(confirmation, sort_keys=True) if confirmation is not None else None
            conn.execute(
                "UPDATE session_query_gate SET pending_confirmation = ? WHERE session_id = ?",
                (payload, session_id),
            )

    def get_pending_confirmation(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, Any] | None:
        """Read the pending confirmation, auto-clearing if expired.

        TTL rule: confirmation expires after exactly 1 turn. If the
        session_turn_counter is strictly greater than turn_at_create,
        the confirmation is dead; clear it and return None.
        """
        with self.session(project_root) as conn:
            try:
                row = conn.execute(
                    "SELECT pending_confirmation, session_turn_counter "
                    "FROM session_query_gate WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
            except Exception:
                return None
        if row is None:
            return None
        raw = row["pending_confirmation"]
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except Exception:
            self.set_pending_confirmation(project_root, session_id, None)
            return None
        turn_at_create = int(payload.get("turn_at_create", 0))
        current_turn = int(row["session_turn_counter"])
        if current_turn > turn_at_create:
            self.set_pending_confirmation(project_root, session_id, None)
            return None
        return payload

    def set_last_confirmed_operation(
        self,
        project_root: Path,
        session_id: str,
        confirmation: dict[str, Any] | None,
    ) -> None:
        """Record a consumed-at-most-once operator approval.

        Structure:
          {
            "id": str,
            "command_sha": str,
            "consumed": bool,
            "approved_at": iso8601,
          }
        """
        with self.session(project_root) as conn:
            self._ensure_row(conn, session_id)
            payload = json.dumps(confirmation, sort_keys=True) if confirmation is not None else None
            conn.execute(
                "UPDATE session_query_gate SET last_confirmed_operation = ? WHERE session_id = ?",
                (payload, session_id),
            )

    def get_last_confirmed_operation(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, Any] | None:
        with self.session(project_root) as conn:
            try:
                row = conn.execute(
                    "SELECT last_confirmed_operation FROM session_query_gate WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
            except Exception:
                return None
        if row is None or not row["last_confirmed_operation"]:
            return None
        try:
            return json.loads(row["last_confirmed_operation"])
        except Exception:
            return None

    def add_turn_edited_file(
        self,
        project_root: Path,
        session_id: str,
        canonical_path: str,
    ) -> bool:
        # Sequential line-edits to the same file in one turn corrupt
        # line numbers; the second add returns False so the caller
        # forces a batch edit instead.
        normalized = canonical_path.replace("\\", "/").strip()
        with self.session(project_root) as conn:
            self._ensure_row(conn, session_id)
            row = self._read_row(conn, session_id) or {}
            existing = list(row.get("turn_edited_files", []))
            if normalized in existing:
                return False
            existing.append(normalized)
            conn.execute(
                "UPDATE session_query_gate SET turn_edited_files = ? WHERE session_id = ?",
                (json.dumps(existing), session_id),
            )
        return True

    def remove_turn_edited_file(
        self,
        project_root: Path,
        session_id: str,
        canonical_path: str,
    ) -> bool:
        # A FRESH READ of the file (ai_get_lines / ai_bundle / symbol snippet) gives
        # the agent current line numbers again, so a subsequent line-edit is safe —
        # drop just this file from the turn set to re-enable line-edit mode for it
        # (other files' locks persist). Returns True if it was present + removed.
        normalized = canonical_path.replace("\\", "/").strip()
        with self.session(project_root) as conn:
            row = self._read_row(conn, session_id)
            if row is None:
                return False
            existing = list(row.get("turn_edited_files", []))
            if normalized not in existing:
                return False
            existing = [p for p in existing if p != normalized]
            conn.execute(
                "UPDATE session_query_gate SET turn_edited_files = ? WHERE session_id = ?",
                (json.dumps(existing), session_id),
            )
        return True

    def clear_turn_edited_files(self, project_root: Path, session_id: str) -> None:
        with self.session(project_root) as conn:
            row = self._read_row(conn, session_id)
            if row is None:
                return
            conn.execute(
                "UPDATE session_query_gate SET turn_edited_files = '[]' WHERE session_id = ?",
                (session_id,),
            )

    def get_turn_edited_files(self, project_root: Path, session_id: str) -> list[str]:
        with self.session(project_root) as conn:
            row = self._read_row(conn, session_id)
        if row is None:
            return []
        return list(row["turn_edited_files"])

    # ── Plan-mode state ─────────────────────────────────────────────
    # Beat 1 (2026-04-18): closed-vocabulary phrase detection enters
    # plan-mode by writing here. The PreToolUse gate reads here to
    # decide whether to enforce PLAN.md write restrictions.

    def get_plan_mode_state(self, project_root: Path, session_id: str) -> dict[str, Any]:
        with self.session(project_root) as conn:
            row = conn.execute(
                "SELECT plan_mode_active, plan_mode_scope, plan_mode_started_at, "
                "plan_mode_last_activity_at FROM session_query_gate "
                "WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return {
                "active": False,
                "scope": None,
                "started_at": None,
                "last_activity_at": None,
            }
        return {
            "active": bool(row["plan_mode_active"]),
            "scope": row["plan_mode_scope"],
            "started_at": row["plan_mode_started_at"],
            "last_activity_at": row["plan_mode_last_activity_at"],
        }

    def set_plan_mode_state(
        self,
        project_root: Path,
        session_id: str,
        *,
        active: bool,
        scope: str | None = None,
    ) -> None:
        now = self._timestamp()
        with self.session(project_root) as conn:
            self._ensure_row(conn, session_id)
            conn.execute(
                "UPDATE session_query_gate SET "
                "plan_mode_active = ?, "
                "plan_mode_scope = ?, "
                "plan_mode_started_at = ?, "
                "plan_mode_last_activity_at = ? "
                "WHERE session_id = ?",
                (
                    1 if active else 0,
                    scope if active else None,
                    now if active else None,
                    now if active else None,
                    session_id,
                ),
            )

    # ── Lane eager-tool grants ──────────────────────────────────────
    # Conductor pre-grants deferred MCP tools to a specific lane so the
    # spawned agent can call them by name without ToolSearch round-trips.
    # Stored as JSON dict[lane_id → list[tool_name]].

    def get_lane_eager_tools_granted(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, list[str]]:
        with self.session(project_root) as conn:
            row = conn.execute(
                "SELECT lane_eager_tools_granted FROM session_query_gate WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return {}
        try:
            payload = json.loads(row[0] or "{}")
        except Exception:
            return {}
        if not isinstance(payload, dict):
            return {}
        return {
            str(k): [str(t) for t in v if str(t).strip()]
            for k, v in payload.items()
            if isinstance(v, list)
        }

    def set_lane_eager_tools_granted(
        self,
        project_root: Path,
        session_id: str,
        grants: dict[str, list[str]],
    ) -> None:
        # Replace-not-merge semantics: callers wanting append do
        # read-modify-write at their layer so the intent stays explicit.
        normalized = {
            str(k): [str(t) for t in v if str(t).strip()]
            for k, v in (grants or {}).items()
            if isinstance(v, list)
        }
        with self.session(project_root) as conn:
            self._ensure_row(conn, session_id)
            conn.execute(
                "UPDATE session_query_gate SET lane_eager_tools_granted = ? WHERE session_id = ?",
                (json.dumps(normalized), session_id),
            )

    def touch_plan_mode_activity(self, project_root: Path, session_id: str) -> None:
        # TTL auto-exit (phase 7) reads last_activity_at; any plan-related
        # tool call updates it so genuine ongoing work doesn't time out.
        with self.session(project_root) as conn:
            self._ensure_row(conn, session_id)
            conn.execute(
                "UPDATE session_query_gate SET plan_mode_last_activity_at = ? WHERE session_id = ?",
                (self._timestamp(), session_id),
            )
