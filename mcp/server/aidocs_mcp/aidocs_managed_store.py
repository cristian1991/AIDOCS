from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from ._sqlite_connect import mark_schema_ensured as _mark_schema_ensured
from ._sqlite_connect import schema_already_ensured as _schema_already_ensured
from ._sqlite_index_store_base import SQLiteIndexStoreBase


def _pid_is_alive(pid: int) -> bool:
    """Cross-platform PID liveness check.

    Bug 2026-05-13: os.kill(pid, 0) on Windows interacts badly with
    the Python signal handling layer in some pid/handle states — even
    though sig=0 is "no-op probe" on POSIX, on Windows it can leave a
    process handle / IPC primitive in a state that crashes the
    enclosing process's NEXT stdio read. Manifested as MCP-server
    transport death after tests/runtime/test_prune_dead_conductor_
    bindings ran. Root fix: use OpenProcess + GetExitCodeProcess via
    ctypes on Windows (the documented liveness probe), keep
    os.kill(pid, 0) on POSIX where it's a true no-op.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        import ctypes.wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            pid,
        )
        if not handle:
            return False
        try:
            exit_code = ctypes.wintypes.DWORD()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            if not ok:
                return False
            return exit_code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    else:
        import os as _os

        try:
            _os.kill(pid, 0)
            return True
        except (ProcessLookupError, OSError):
            return False


class AidocsManagedStore(SQLiteIndexStoreBase):
    """Project-local managed-mode state — sqlite-backed replacement for
    ``.MEMORY/config/aidocs-managed.json``.

    Single row per project ``(id=1)``. The row is upserted on every
    ``set()`` call. Ingests the legacy JSON on first ``init_db()`` and
    hard-deletes it so the project never carries two sources of truth.
    """

    _SCHEMA_KEYS: frozenset[str] = frozenset(
        {
            "active",
            "session_id",
            "activated_at",
            "last_updated",
            "source",
        },
    )

    def _legacy_json_path(self, project_root: Path) -> Path:
        return project_root / ".MEMORY" / "config" / "aidocs-managed.json"

    def init_db(self, project_root: Path) -> None:
        # ONE schema creation per process per file (#756). This was the single
        # heaviest init_db in the daemon capture -- 1,786 opens -- because it
        # runs on every managed-mode read, and a `CREATE TABLE IF NOT EXISTS`
        # still costs an open, a write lock and a commit to learn there is
        # nothing to do. The memo re-verifies the file exists, so a deleted DB
        # is rebuilt rather than assumed.
        _db = self.db_path(project_root)
        if _schema_already_ensured(_db, "aidocs_managed"):
            return
        with self.session(project_root) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS aidocs_managed (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    active INTEGER NOT NULL DEFAULT 0,
                    session_id TEXT,
                    activated_at TEXT,
                    last_updated TEXT,
                    source TEXT
                );
                """,
            )
            # Additive migration (2026-04-22): stamp the MCP server's
            # per-process boot token on every bind so a restart of the
            # server forces a reconnect even when sqlite says
            # active=true. See managed_mode_service.current_boot_token.
            try:
                conn.execute("ALTER TABLE aidocs_managed ADD COLUMN bound_by_boot_token TEXT")
            except Exception:
                pass
            # Additive migration (2026-04-30): record when the FIRST-
            # EVER bootstrap completed (full _sync_bootstrap_indexes
            # ran). Subsequent managed-mode activations check this
            # column: NULL → first-ever (run full sync), non-NULL →
            # per-launch (read status only). Operator doctrine:
            # "if indexes have never been synced on managed-mode
            # bootstrap = first-ever." A project with zero code
            # files still completes a bootstrap (memory, schema,
            # capabilities, etc.); empty code_files is not the
            # right signal.
            try:
                conn.execute("ALTER TABLE aidocs_managed ADD COLUMN bootstrap_completed_at TEXT")
            except Exception:
                pass
            # #58 (canonical 2026-04-26): per-conductor mapping is the
            # authoritative source for "what session is THIS conductor
            # bound to." Project-keyed singleton above is now a
            # deprecated fallback. See security-gates.md §0.5
            # #50/#54 sub-clause "conductor-bound state keying."
            #
            # PK is cli_session_id alone — sqlite is per-project so
            # project_root is implicit. No cleanup of stale rows in
            # v1; tiny rows, irrelevant accumulation.
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS aidocs_managed_per_conductor (
                    cli_session_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    activated_at TEXT NOT NULL,
                    last_updated TEXT NOT NULL,
                    source TEXT,
                    bound_by_boot_token TEXT
                );
                """,
            )
        self._ingest_legacy_json(project_root)
        _mark_schema_ensured(_db, "aidocs_managed")

    def _ingest_legacy_json(self, project_root: Path) -> None:
        path = self._legacy_json_path(project_root)
        if not path.is_file():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(raw, dict):
            return
        data = {k: v for k, v in raw.items() if k in self._SCHEMA_KEYS}
        with self.session(project_root) as conn:
            existing = conn.execute("SELECT 1 FROM aidocs_managed WHERE id = 1").fetchone()
            if existing is not None:
                path.unlink()
                return
            conn.execute(
                """
                INSERT INTO aidocs_managed
                    (id, active, session_id, activated_at, last_updated, source)
                VALUES (1, ?, ?, ?, ?, ?)
                """,
                (
                    1 if bool(data.get("active")) else 0,
                    data.get("session_id"),
                    data.get("activated_at"),
                    data.get("last_updated"),
                    data.get("source"),
                ),
            )
        path.unlink()

    def get(self, project_root: Path) -> dict[str, Any]:
        with self.session(project_root) as conn:
            row = conn.execute(
                "SELECT active, session_id, activated_at, last_updated, "
                "source, bound_by_boot_token "
                "FROM aidocs_managed WHERE id = 1",
            ).fetchone()
        if row is None:
            return {
                "active": False,
                "session_id": None,
                "activated_at": None,
                "last_updated": None,
                "source": None,
                "bound_by_boot_token": None,
            }
        keys = row.keys() if hasattr(row, "keys") else []
        return {
            "active": bool(row["active"]),
            "session_id": row["session_id"],
            "activated_at": row["activated_at"],
            "last_updated": row["last_updated"],
            "source": row["source"],
            "bound_by_boot_token": (
                row["bound_by_boot_token"] if "bound_by_boot_token" in keys else None
            ),
        }

    def set(
        self,
        project_root: Path,
        *,
        session_id: str,
        source: str = "/aidocs",
        boot_token: str | None = None,
    ) -> dict[str, Any]:
        now = self._timestamp()
        current = self.get(project_root)
        # activated_at resets when the session identity changes because the
        # dashboard treats a session switch as a new bind window for uptime
        # display. Rebinding the same session refreshes last_updated only.
        session_changed = current.get("session_id") != session_id
        activated_at = (
            now if session_changed or not current.get("activated_at") else current["activated_at"]
        )
        with self.session(project_root) as conn:
            conn.execute(
                """
                INSERT INTO aidocs_managed
                    (id, active, session_id, activated_at, last_updated,
                     source, bound_by_boot_token)
                VALUES (1, 1, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    active = 1,
                    session_id = excluded.session_id,
                    activated_at = excluded.activated_at,
                    last_updated = excluded.last_updated,
                    source = excluded.source,
                    bound_by_boot_token = excluded.bound_by_boot_token
                """,
                (session_id, activated_at, now, source, boot_token),
            )
        return self.get(project_root)

    def clear(self, project_root: Path) -> dict[str, Any]:
        with self.session(project_root) as conn:
            conn.execute("DELETE FROM aidocs_managed WHERE id = 1")
        return self.get(project_root)

    # ── First-ever bootstrap timestamp (2026-04-30) ──
    # Operator doctrine: managed-mode bootstrap on a project where
    # bootstrap_completed_at IS NULL = first-ever bootstrap; run
    # full _sync_bootstrap_indexes. Otherwise the per-launch path
    # is light. The signal is independent of code-file count
    # (a project with zero source files still completes a bootstrap).

    def get_bootstrap_completed_at(
        self,
        project_root: Path,
    ) -> str | None:
        """Return ISO-ish timestamp of last full bootstrap, or None
        if bootstrap has never completed for this project.

        Self-bootstraps the schema via init_db() so a fresh DB
        returns None cleanly (correct: a project that's never been
        bootstrapped has no stamp). Without this, the read would
        raise OperationalError on a missing table and the broad
        except mask would lose the legitimate-NULL signal.
        """
        self.init_db(project_root)
        try:
            with self.session(project_root) as conn:
                row = conn.execute(
                    "SELECT bootstrap_completed_at FROM aidocs_managed WHERE id = 1",
                ).fetchone()
        except Exception:
            return None
        if row is None:
            return None
        try:
            value = row["bootstrap_completed_at"]
        except (KeyError, IndexError):
            return None
        return str(value) if value else None

    def stamp_bootstrap_completed(
        self,
        project_root: Path,
        *,
        when: str,
    ) -> None:
        """Stamp bootstrap_completed_at on the singleton row.
        Idempotent — overwrite is fine; the column is informational.
        Inserts a row if no managed-mode bind exists yet (first-ever
        bootstrap can run before any session is bound).

        Self-bootstraps the schema via init_db() so direct callers
        (without going through ManagedModeService) don't crash on a
        fresh DB. The init is idempotent — running it twice is a
        no-op when the table already exists.
        """
        self.init_db(project_root)
        with self.session(project_root) as conn:
            conn.execute(
                "INSERT INTO aidocs_managed "
                "(id, active, bootstrap_completed_at) "
                "VALUES (1, 0, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "bootstrap_completed_at = excluded.bootstrap_completed_at",
                (when,),
            )

    # ── Per-conductor mapping (#58, canonical 2026-04-26) ──
    # Authoritative source for conductor-bound state. The singleton
    # methods above (get/set/clear) remain as deprecated fallback for
    # callers that don't yet plumb cli_session_id. New code MUST use
    # the per-conductor methods.

    def get_per_conductor(
        self,
        project_root: Path,
        *,
        cli_session_id: str,
    ) -> dict[str, Any] | None:
        """Return the mapping row for `cli_session_id` or None when
        no binding exists for this conductor on this project.
        """
        if not cli_session_id:
            return None
        self.init_db(project_root)
        with self.session(project_root) as conn:
            row = conn.execute(
                "SELECT cli_session_id, session_id, activated_at, "
                "last_updated, source, bound_by_boot_token "
                "FROM aidocs_managed_per_conductor "
                "WHERE cli_session_id = ?",
                (cli_session_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "active": True,
            "cli_session_id": row["cli_session_id"],
            "session_id": row["session_id"],
            "activated_at": row["activated_at"],
            "last_updated": row["last_updated"],
            "source": row["source"],
            "bound_by_boot_token": row["bound_by_boot_token"],
        }

    def list_conductors_readonly(self, project_root: Path) -> list[dict[str, Any]]:
        """Per-conductor bindings as a PURE READ — no ``init_db``, ever.

        `list_conductors` calls `init_db`, and `init_db` INGESTS AND DELETES the
        legacy `.MEMORY/config/aidocs-managed.json`. `enforcement.py` documents
        why that matters: its authority checks run WHILE A FILE OPERATION IS IN
        FLIGHT, so a read that ingests could delete the very file being edited.
        That module therefore restricts itself to pure reads, and needed one of
        these to answer "which conductors are bound here" (#892).

        A missing table is not an error and not an empty registry either — it is
        "nothing has been recorded", which reads the same as no rows for every
        caller of this method. Returns [] rather than raising, so an authority
        resolver degrades to "no session operator" instead of throwing inside a
        file op.
        """
        import sqlite3 as _sqlite3

        try:
            with self.session(project_root) as conn:
                rows = conn.execute(
                    "SELECT cli_session_id, session_id, activated_at, "
                    "last_updated, source, bound_by_boot_token "
                    "FROM aidocs_managed_per_conductor "
                    "ORDER BY last_updated DESC",
                ).fetchall()
        except _sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return []
            raise
        return [
            {
                "cli_session_id": r["cli_session_id"],
                "session_id": r["session_id"],
                "activated_at": r["activated_at"],
                "last_updated": r["last_updated"],
                "source": r["source"],
                "bound_by_boot_token": r["bound_by_boot_token"],
            }
            for r in rows
        ]

    def list_conductors(self, project_root: Path) -> list[dict[str, Any]]:
        """All per-conductor bindings on this project -- every connected agent
        keyed by cli_session_id (= host_session_id). Read-only; the source for
        the conductor-level agent audit. Newest-bound first.
        """
        self.init_db(project_root)
        with self.session(project_root) as conn:
            rows = conn.execute(
                "SELECT cli_session_id, session_id, activated_at, "
                "last_updated, source, bound_by_boot_token "
                "FROM aidocs_managed_per_conductor "
                "ORDER BY last_updated DESC",
            ).fetchall()
        return [
            {
                "cli_session_id": r["cli_session_id"],
                "session_id": r["session_id"],
                "activated_at": r["activated_at"],
                "last_updated": r["last_updated"],
                "source": r["source"],
                "bound_by_boot_token": r["bound_by_boot_token"],
            }
            for r in rows
        ]

    def set_per_conductor(
        self,
        project_root: Path,
        *,
        cli_session_id: str,
        session_id: str,
        source: str = "/aidocs",
        boot_token: str | None = None,
    ) -> dict[str, Any]:
        """Upsert the per-conductor mapping. activated_at resets when
        the session identity changes for this conductor; rebinding
        the same session refreshes last_updated only.
        """
        now = self._timestamp()
        existing = self.get_per_conductor(
            project_root,
            cli_session_id=cli_session_id,
        )
        session_changed = existing is None or existing.get("session_id") != session_id
        activated_at = (
            now
            if session_changed or not (existing and existing.get("activated_at"))
            else existing["activated_at"]
        )
        with self.session(project_root) as conn:
            conn.execute(
                """
                INSERT INTO aidocs_managed_per_conductor
                    (cli_session_id, session_id, activated_at,
                     last_updated, source, bound_by_boot_token)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(cli_session_id) DO UPDATE SET
                    session_id = excluded.session_id,
                    activated_at = excluded.activated_at,
                    last_updated = excluded.last_updated,
                    source = excluded.source,
                    bound_by_boot_token = excluded.bound_by_boot_token
                """,
                (cli_session_id, session_id, activated_at, now, source, boot_token),
            )
        result = self.get_per_conductor(
            project_root,
            cli_session_id=cli_session_id,
        )
        return result or {}

    def delete_per_conductor(
        self,
        project_root: Path,
        host_session_id: str,
    ) -> bool:
        """Delete ONE conductor's binding row (#438). Row existence is
        the binding, so this IS the unbind. Returns True when a row was
        actually removed, False when none existed.
        """
        sid = (host_session_id or "").strip()
        if not sid:
            return False
        self.init_db(project_root)
        with self.session(project_root) as conn:
            removed = conn.execute(
                "DELETE FROM aidocs_managed_per_conductor WHERE cli_session_id = ?",
                (sid,),
            ).rowcount
        return bool(removed and removed > 0)

    def delete_all_per_conductor(self, project_root: Path) -> int:
        """Delete EVERY conductor binding row on this project (#438).
        Returns the number of rows removed. Caller is responsible for
        the admin gate — this is the mechanical store operation.
        """
        self.init_db(project_root)
        with self.session(project_root) as conn:
            removed = conn.execute(
                "DELETE FROM aidocs_managed_per_conductor",
            ).rowcount
        return int(removed or 0)

    # ── #599 / #54-C1b: telling a PHANTOM from a live binding ──────────
    #
    # `prune_dead_conductor_bindings` (below) is RETIRED (#982). This comment
    # used to call it "sound but INERT", and the first half was wrong in a way
    # that took a live outage to see.
    #
    # The observation was right: `set_mode` stamps
    # `boot_token=_MCP_SERVER_BOOT_TOKEN` UNCONDITIONALLY, so a row records
    # WHICH DAEMON WROTE IT, never whose actor it belongs to. Measured on this
    # repo's own root: 51 bindings, all carrying the one RUNNING daemon's pid,
    # so the pid prune removed zero and `correlate_host_session` — which refuses
    # on >=2 live bindings — refused essentially always.
    #
    # WHAT "INERT" MISSED: it prunes zero only while the stamping daemon is
    # still alive. The moment that generation exits — a deploy, a restart — the
    # SAME predicate deletes EVERY row it wrote, which is every live
    # conversation. A predicate is not harmless because it is quiet on a
    # steady-state machine; this one was measured quiet and was one restart
    # away from unbinding everybody, which is exactly what it did.
    # The writer is fixed (#54-C1); the wreckage was not — and the wreckage
    # included this reading of it.
    #
    # The proof that DOES discriminate was already in the building: the #464
    # AUTHENTICATED host-id chain (`session_query_gate.host_session_id_chain`),
    # whose entries "only ever come from authenticated hook stamps for THIS
    # session's row, so a foreign session's uuid can never enter the chain".
    # It is durable, server-written, host-agnostic and TRANSPORT-IMMUNE — a
    # per-request FastMCP token is minted inside the daemon and never reaches
    # a hook, so it can never appear there. Cross-validated on the live root
    # against the host's own transcript files: every id in a chain is a real
    # host conversation; 0 of the 51 bindings are.
    #
    # NO EIGHTH IDENTITY AXIS. This reads an existing durable ledger keyed on
    # SESSION and HOST_SESSION — nothing new is observed or invented.

    def classify_conductor_bindings(
        self,
        project_root: Path,
        *,
        is_live=None,
    ) -> dict[str, list[dict[str, object]]]:
        """Sort every binding into LIVE / DEAD / UNPROVABLE. Read-only.

        UNPROVABLE IS A REAL THIRD STATE and must not collapse into either
        neighbour. Here the house rule cuts in the UNUSUAL direction: pruning a
        live conductor's binding is an OUTAGE, so a row that cannot be PROVEN
        dead is kept — and a row that cannot be proven LIVE is still not
        evidence of death. The bucket is reported so the ambiguity is visible
        instead of being silently resolved one way or the other.

        Ladder (#982 removed the pid rung that used to sit above all of these):
          1. ``is_live(row)`` returns True                 -> LIVE.
          2. ``is_live(row)`` returns False, AND the oracle has proven something
             about this session                           -> DEAD. The oracle
             READ its store and does not carry this id.
          3. anything else — None, no oracle, a raise, or a False about a
             session the oracle has never confirmed        -> UNPROVABLE -> KEPT.

        THE ONLY DEATH EVIDENCE IS THE LEASE/LIVENESS ORACLE, which is evidence
        ABOUT THE ACTOR. `bound_by_boot_token` is FORENSIC PROVENANCE ONLY — it
        records which daemon wrote the row and may never authorize a deletion
        (#982, operator ruling 2026-08-30).

        THE CHAIN WAS RETIRED FROM HERE (#892; operator ruling 2026-08-23,
        "retire the chain and the slot, use the lease"). Rungs 4 and 5 used to
        read the #464 authenticated chain: present meant LIVE, absent from a
        NON-EMPTY chain meant DEAD. That second half is chain-membership used
        as an authority predicate — THE EXACT PREDICATE #880 MEASURED AND
        REFUTED ("DO NOT BUILD THAT MIGRATION. It is now refuted, before anyone
        wrote it") — and here it did not merely mislabel, because the DEAD
        bucket is DELETED by ``prune_phantom_conductor_bindings`` at MCP boot.

        The chain is cap-16 with FIFO eviction, so a busy session evicts a LIVE
        window's id (#880: ~4 slots burned in one evening). That window was
        graded DEAD, its binding deleted, and every gated tool then answered
        managed_mode_not_active — with no way back, because the PreToolUse
        writer that would re-add it to the chain never runs: the call is
        refused BEFORE it. Refused for not being in a list it can only join by
        not being refused.

        WHY A FALSE FROM THE ORACLE MAY KILL WHERE THE CHAIN MAY NOT. Rung 5
        did two jobs with one predicate: it correctly pruned phantom bindings
        minted by rotating request ids (#599 — without that, correlation
        refuses on >=2 live bindings, the #787 lockout) and it incorrectly
        killed a LIVE window the chain had evicted (#892). The lease separates
        them because IT DOES NOT EVICT: a live window the chain dropped still
        has its SessionStart row and answers True, while a phantom id never had
        one and answers False. Absence from a cap-16 FIFO list proves nothing;
        absence from an append-only-per-window table is evidence.

        `False` is therefore a POSITIVE claim the oracle must only make after
        actually reading its store. `None` — unreadable, unknown shape, or a
        raise — is NOT that claim and grades UNPROVABLE, because this bucket is
        deleted and an unreadable store must never look like a denial.

        WHY THE ORACLE IS INJECTED rather than imported: this module is one of
        the six that ``test_the_window_never_reaches_an_authority_predicate``
        text-scans for the window axis, and that guard is deliberate (#880
        item 4 — the bind must stay reachable by an UNBOUND window). Taking the
        verdict as a parameter keeps this store's vocabulary its own and lets
        the wiring live where the window axis is already lawful. Same shape as
        ``run_pytest=`` and ``emit=`` elsewhere in this codebase.

        WITH NO ORACLE (``is_live=None``) this is strictly safer than the
        version it replaces: only pid-death can prune. Stale rows then
        accumulate, which is the lesser evil #880 item 2 explicitly chooses
        over stranding live work.
        """
        self.init_db(project_root)
        live: list[dict[str, object]] = []
        dead: list[dict[str, object]] = []
        unprovable: list[dict[str, object]] = []

        rows = [dict(r) for r in self.list_conductors(project_root)]

        def _ask(entry: dict) -> bool | None:
            if is_live is None:
                return None
            try:
                return is_live(entry)
            except Exception:
                # An oracle that raised has PROVEN NOTHING. Fail toward
                # UNPROVABLE — never toward the bucket that gets deleted.
                return None

        # FIRST PASS: which SESSIONS has the oracle proven anything about?
        #
        # PER-SESSION COMPLETENESS, and it is the retired rung's own rule: "the
        # session has NO authenticated chain yet -> UNPROVABLE. Absence of a
        # ledger is not a verdict. A session's first bind legitimately happens
        # before any hook has stamped anything." The same holds for the lease —
        # a window can bind before its SessionStart row exists — so a False
        # about a session the oracle has never confirmed anything for is NOT
        # admissible as death. Measured while wiring this: without the guard, a
        # fresh session's only binding was graded DEAD and deleted at boot.
        verdicts: list[bool | None] = []
        proven_sessions: set[str] = set()
        for entry in rows:
            verdict = _ask(entry)
            verdicts.append(verdict)
            if verdict is True:
                proven_sessions.add(str(entry.get("session_id") or "").strip())

        for entry, attested in zip(rows, verdicts, strict=False):
            # THE `boot_stamp_pid_dead -> DEAD` RUNG IS GONE (#982, operator
            # ruling 2026-08-30: "Remove daemon-PID death as an actor-death
            # predicate everywhere ... `bound_by_boot_token` is FORENSIC
            # PROVENANCE ONLY; it must not authorize deletion").
            #
            # DAEMON LIFETIME AND ACTOR LIFETIME ARE DIFFERENT AXES. The pid in
            # that stamp is the MCP SERVER's own, written unconditionally by
            # set_mode, so it records WHICH DAEMON WROTE THE ROW and says
            # nothing about whose actor it belongs to. A dead pid proves only
            # that a process died.
            #
            # AND IT RAN FIRST, ahead of the oracle — so a window the lease
            # positively attested as LIVE was still graded DEAD and deleted the
            # moment its writer daemon went away. Removing the residual
            # `prune_dead_conductor_bindings` call alone would NOT have fixed
            # that: `prune_phantom_conductor_bindings` deletes this `dead`
            # bucket, so the same bad verdict reached the same DELETE by a
            # second route.
            #
            # MEASURED 2026-08-30 on the live gate: a bound WebMCP conversation
            # (web-b512…) lost its row while another (web-a186…, stamped by the
            # running daemon) survived — one row deleted, one kept, by a
            # predicate about neither actor.
            session = str(entry.get("session_id") or "").strip()
            if attested is True:
                entry["reason"] = "lease_attested"
                live.append(entry)
            elif attested is False and session in proven_sessions:
                # The oracle READ its store, has demonstrably seen this session,
                # and does not carry this id.
                entry["reason"] = "lease_denies_ownership"
                dead.append(entry)
            elif attested is False:
                entry["reason"] = "session_has_no_lease_evidence"
                unprovable.append(entry)
            else:
                entry["reason"] = (
                    "no_liveness_oracle" if is_live is None else "lease_unprovable"
                )
                unprovable.append(entry)
        return {"live": live, "dead": dead, "unprovable": unprovable}

    def prune_phantom_conductor_bindings(
        self,
        project_root: Path,
        *,
        is_live=None,
    ) -> dict[str, object]:
        """Delete ONLY the bindings ``classify_conductor_bindings`` proves dead.

        Idempotent. Deletes nothing from the UNPROVABLE bucket — ever — so a
        conductor can never lose its own binding to this call, and returns all
        three buckets so an operator can see what was left alone and why.

        ``is_live`` is forwarded verbatim (#892). WITHOUT IT this call can only
        prune provable pid-death: phantom bindings grade UNPROVABLE and survive,
        which is safe but leaves #599's symptom in place. The caller that owns
        the window axis supplies the oracle — see
        ``window_lease.conductor_liveness_oracle``.
        """
        verdict = self.classify_conductor_bindings(project_root, is_live=is_live)
        pruned: list[dict[str, object]] = []
        for entry in verdict["dead"]:
            sid = str(entry.get("cli_session_id") or "").strip()
            if sid and self.delete_per_conductor(project_root, sid):
                pruned.append(entry)
        return {
            "pruned": pruned,
            "kept_live": verdict["live"],
            "unprovable": verdict["unprovable"],
            "total_after": len(verdict["live"]) + len(verdict["unprovable"]),
        }

    def prune_dead_conductor_bindings(
        self,
        project_root: Path,
    ) -> dict[str, object]:
        """RETIRED (#982). Daemon-PID death is not actor death — it never was.

        This walked every per-conductor row, parsed the pid out of
        ``bound_by_boot_token``, and DELETED the row when that pid was not
        alive. The pid is the MCP SERVER's own, stamped unconditionally by
        ``set_mode``, so the predicate asked "did the process that WROTE this
        row die?" and then answered a question about the ACTOR with it.

        MEASURED 2026-08-30 on the live gate: a bound WebMCP conversation
        (web-b512…) lost its binding while another (web-a186…, stamped by the
        running daemon) survived — one row deleted, one kept, on evidence about
        neither actor. That caller then got ``managed_mode_inactive`` mid
        conversation, having done nothing but wait for our restart.

        OPERATOR RULING 2026-08-30: "Remove `prune_dead_conductor_bindings` from
        production cleanup. If the helper has no legitimate actor-semantic
        purpose afterwards, RETIRE IT rather than leave a dangerous unused
        primitive."

        It has none. Actor cleanup is ``prune_phantom_conductor_bindings``
        driven by the lease/liveness oracle — evidence ABOUT THE ACTOR, which
        keeps LIVE and UNPROVABLE alike and deletes only on positive proof.
        Structurally degenerate rows (an empty conductor id) are a SEPARATE
        hygiene concern needing their own narrow predicate; they must not ride a
        pid check, which is how this one justified its continued existence.

        RAISES rather than being deleted outright, so any surviving caller fails
        LOUDLY instead of quietly finding a name that no longer exists — and so
        the reasoning stays attached to the thing it retired.
        """
        raise NotImplementedError(
            "prune_dead_conductor_bindings is RETIRED (#982): daemon-PID death "
            "is not actor death. Actor cleanup is "
            "prune_phantom_conductor_bindings driven by the lease/liveness "
            "oracle, which judges the ACTOR."
        )

    def snapshot_prompt_submit_state(
        self,
        project_root: Path,
        *,
        host_session_id: str,
    ) -> dict[str, object]:
        from .prompt_submit_store_snapshot import capture_scoped_rows

        self.init_db(project_root)
        scopes = {
            "aidocs_managed": ("id = 1", ()),
            "aidocs_managed_per_conductor": (
                "cli_session_id = ?",
                (host_session_id,),
            ),
        }
        with self.session(project_root) as conn:
            return capture_scoped_rows(conn, scopes)

    def restore_prompt_submit_state(
        self,
        project_root: Path,
        snapshot: dict[str, object],
        *,
        host_session_id: str,
    ) -> None:
        from .prompt_submit_store_snapshot import restore_scoped_rows

        self.init_db(project_root)
        scopes = {
            "aidocs_managed": ("id = 1", ()),
            "aidocs_managed_per_conductor": (
                "cli_session_id = ?",
                (host_session_id,),
            ),
        }
        with self.session(project_root) as conn:
            restore_scoped_rows(conn, scopes, snapshot)
