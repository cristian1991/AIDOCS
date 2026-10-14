"""Host-session operator binding store.

Per /goal 2026-05-20. When `/aidocs` runs in a host (Claude Code,
OpenCode, etc.) the host process has a per-session UUID
(host_session_id) but NO operator identity. This store implements
the pairing handshake that binds an authenticated dashboard operator
to that host session:

  1. Host (/aidocs) → ``create_pending(...)`` mints a pending
     binding + a short pairing code. The host chat displays ONLY
     the pairing code + status — never a password or token.
  2. Dashboard (authenticated operator) → ``list_pending(...)`` shows
     the pending bindings, then ``approve(...)`` atomically binds
     the operator's user_id to that host_session_id.
  3. Hooks → ``resolve_operator(host_session_id)`` returns the bound
     operator's user_id (when approved + not expired) so the hook can
     build an operator_context for the host session.

Security model:
  - Pairing code is stored HASHED (sha256). The plaintext is shown
    once in the host chat; approval matches the hash.
  - approve() is an atomic ``UPDATE ... WHERE status='pending'`` so
    two concurrent approvals can't both win (the second sees
    rowcount 0).
  - WAL + busy_timeout + short transactions so the host-write and
    dashboard-approve paths don't deadlock under contention.
  - Expired pending bindings are never approvable; resolve() ignores
    expired/revoked rows.

The store NEVER stores passwords or operator tokens — only the
operator_user_id once an authenticated operator approves.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3

from ._sqlite_connect import mark_schema_ensured as _mark_schema_ensured
from ._sqlite_connect import schema_already_ensured as _schema_already_ensured
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# #755: canonical connect — WAL, synchronous=NORMAL, busy_timeout and
# foreign_keys=ON, none of which raw sqlite3.connect() applies. This
# schema declares no FOREIGN KEYs, so enabling enforcement is inert here.
# row_factory stays ROW (the helper's default): this store read by name
# already, so the hand-set line it replaces is redundant.
from ._sqlite_connect import connect as _canonical_connect

_DEFAULT_PENDING_TTL_SECONDS = 600  # 10 min to approve a pairing


def _db_path(project_root: Path) -> Path:
    return project_root / ".MEMORY" / ".index" / "host_operator_bindings.sqlite3"


def _canon_sid(host_session_id: str) -> str:
    """Canonical host-session-id form for MATCHING (storage keeps the
    raw value). Hosts and dashboards disagree on UUID presentation —
    dashed vs dashless, upper vs lower — and a mismatch silently
    orphans an APPROVED binding (operator bug 2026-07-17). Comparing
    case-insensitively with dashes ignored makes both spellings of the
    same identifier resolve.
    """
    return str(host_session_id or "").strip().lower().replace("-", "")


def _canon_window(window_key: str) -> str:
    """A window key, or ``""`` — validated on the WRITE side and the READ side.

    IMPORTED, NOT RE-DECLARED. ``window_key`` owns the shape because it MINTS
    the key; a private copy here would drift from the minter one edit at a
    time, and the only symptom would be an approved binding whose key the
    resolver refuses — authority that exists and can never be used.
    """
    from .window_key import WINDOW_KEY_SHAPE

    candidate = str(window_key or "").strip()
    return candidate if WINDOW_KEY_SHAPE.fullmatch(candidate) else ""


def _canon_caller(caller_key: str, window_key: str = "") -> str:
    """The namespaced caller key to STORE, or ``""``.

    VALIDATED BY THE CLASS THAT OWNS THE SHAPE, never by a prefix check here.
    An explicit ``caller_key`` wins because the remote surface has no window to
    derive one from; otherwise a window becomes ``win:<window>`` so the local
    pairing path keeps working unchanged.

    Anything that does not parse is stored EMPTY, not raw. #880 records what
    skipping that rule cost once already: 'auth-truth-614', a synthetic test
    id, seated permanently in an authority structure. A key nobody minted that
    merely LOOKS well-formed is worse than no key, because every downstream
    check reads it as an attestation.
    """
    from .caller_attestation import os_window, parse_key

    explicit = str(caller_key or "").strip()
    if explicit:
        attestation, _reason = parse_key(explicit)
        return attestation.key if attestation else ""
    attestation, _reason = os_window(window_key)
    return attestation.key if attestation else ""


def _hash_code(code: str) -> str:
    return hashlib.sha256((code or "").encode("utf-8")).hexdigest()


def _iso_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ",
    )


@dataclass(frozen=True)
class HostBinding:
    binding_id: str
    host_kind: str
    host_session_id: str
    #: THE IDENTITY KEY. host_session_id above is the conversation this pairing
    #: was FILED under -- provenance, and a value that rotates. This one does
    #: not rotate, and it is what resolution reads (operator ruling 2026-09-04).
    window_key: str
    project_root: str
    aidocs_session_id: str
    requested_identity: str
    status: str  # pending | approved | revoked | expired
    operator_user_id: str
    created_at: str
    expires_at: str
    approved_at: str
    approved_by_role: str


# ── Foreign-writer audit (#421 forged-token cleanup, 2026-07-16) ──
#
# During the identity war an EXTERNAL (non-store) writer inserted the row
# bind_31f919c08cb602f5 directly into host_operator_bindings: non-``hb_``
# id, backslash project_root, dashless host_session_id, and a ~10-year
# expires_at no store code path can produce. ``foreign_format_flags``
# detects that format drift so `aidocs bindings --audit` can SURFACE such
# rows for operator review — it never revokes anything by itself.

_STORE_BINDING_ID_RE = re.compile(r"hb_[0-9a-f]{20}\Z")
_DASHLESS_HEX32_RE = re.compile(r"[0-9a-f]{32}\Z")
# The store's pending TTL is minutes; anything beyond a week between
# created_at and expires_at cannot have come from create_pending.
_MAX_PLAUSIBLE_TTL_SECONDS = 7 * 86400


def _parse_iso(ts: str) -> float | None:
    try:
        return datetime.strptime(str(ts), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()
    except (ValueError, TypeError):
        return None


def foreign_format_flags(binding: "HostBinding") -> list[str]:
    """Format-drift flags for a binding row NOT written by this store.
    Empty list == shape-consistent with store-written rows."""
    flags: list[str] = []
    if not _STORE_BINDING_ID_RE.fullmatch(str(binding.binding_id or "")):
        flags.append("foreign_binding_id_format")
    if "\\" in str(binding.project_root or ""):
        flags.append("project_root_backslash_drift")
    created = _parse_iso(binding.created_at)
    expires = _parse_iso(binding.expires_at)
    if created is None or expires is None:
        flags.append("timestamp_format_drift")
    elif (expires - created) > _MAX_PLAUSIBLE_TTL_SECONDS:
        flags.append("expiry_horizon_drift")
    sid = str(binding.host_session_id or "").strip().lower()
    if binding.host_kind == "claude_code" and _DASHLESS_HEX32_RE.fullmatch(sid):
        flags.append("dashless_host_session_id")
    return flags


class HostOperatorBindingStore:
    """SQLite-backed pairing store. WAL + busy_timeout + atomic
    pending→approved. Stateless across calls.
    """

    def db_path(self, project_root: Path) -> Path:
        return _db_path(project_root)

    def _connect(self, project_root: Path) -> sqlite3.Connection:
        path = _db_path(project_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        # busy_timeout (ms) so concurrent host-write + dashboard-
        # approve serialize instead of raising "database is locked".
        conn = _canonical_connect(str(path), timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def init_db(self, project_root: Path) -> None:
        # ONE schema creation per process per file (#756) -- this ran on every
        # hook event. The memo re-verifies the file exists, so a deleted DB is
        # rebuilt rather than assumed.
        _db = self.db_path(project_root)
        # The memo key carries the SHAPE, not just the table name: a box that
        # ran an earlier build has the row shape from that build, and a memo
        # keyed on the bare name would skip the window_key migration forever.
        if _schema_already_ensured(_db, "host_operator_bindings_v3_caller_key"):
            return
        with self._connect(project_root) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS host_operator_bindings (
                    binding_id TEXT PRIMARY KEY,
                    host_kind TEXT NOT NULL,
                    host_session_id TEXT NOT NULL,
                    project_root TEXT NOT NULL,
                    aidocs_session_id TEXT NOT NULL DEFAULT '',
                    pairing_code_hash TEXT NOT NULL,
                    requested_identity TEXT NOT NULL DEFAULT '',
                    operator_user_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    approved_at TEXT NOT NULL DEFAULT '',
                    approved_by_role TEXT NOT NULL DEFAULT ''
                )
                """,
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_hob_host_session "
                "ON host_operator_bindings(host_session_id)",
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_hob_status ON host_operator_bindings(status)",
            )
            # ── THE IDENTITY KEY (operator ruling 2026-09-04) ───────────────
            #
            # host_session_id is a CONVERSATION and a conversation ROTATES:
            # measured on the operator's own box, Claude Code rotated it three
            # times in one day and each rotation orphaned a signed-in
            # super_admin's approved binding -- 'unauthenticated_host_session'
            # for a user whose context resolved perfectly under the PREVIOUS
            # id. Same root cause as #1001/#1009/#1011: a CALLER fact used as
            # an IDENTITY key.
            #
            # window_key ('<host pid>:<host creation filetime>') does not
            # rotate: measured across two /resume, one /clear and one /mcp
            # reconnect (#880). It becomes the key; host_session_id stays as
            # PROVENANCE -- what the pairing was filed under, never what it is
            # looked up by.
            #
            # Added by ALTER on an existing table: these rows ARE the
            # operators' live authority, and a rebuild would sign everybody out.
            self._add_window_key_column(conn)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_hob_window "
                "ON host_operator_bindings(window_key)",
            )
            # ── THE CALLER KEY, NAMESPACED (2026-09-05) ─────────────────────
            #
            # window_key answers WHICH CALLER on exactly ONE surface. A remote
            # OAuth caller has no process on this box, so it has no window --
            # and the gate had been substituting "ogh_" + token_id, which is
            # THE TOKEN RESTATED: two browser tabs on one token read as one
            # caller, and a token refresh reads one caller as two.
            #
            # caller_key carries the CLASS with the value ('win:<pid>:<ft>' /
            # 'rconv:web-<digest>') so the surfaces cannot be confused for one
            # another. A bare value column could not do this: a remote id
            # shaped like a window would compare equal to that window.
            #
            # BACKFILLED, NOT REQUIRED. Existing rows ARE live operator
            # authority; leaving them to be re-paired would sign everybody out,
            # so every row that already has a window becomes 'win:<window>'
            # in the same idempotent migration.
            self._add_caller_key_column(conn)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_hob_caller "
                "ON host_operator_bindings(caller_key)",
            )
            conn.commit()
        _mark_schema_ensured(_db, "host_operator_bindings_v3_caller_key")

    def _add_caller_key_column(self, conn: sqlite3.Connection) -> None:
        """Add ``caller_key`` and backfill it from ``window_key``. Idempotent."""
        try:
            have = {
                str(r[1])
                for r in conn.execute("PRAGMA table_info(host_operator_bindings)")
            }
        except Exception:
            return
        if "caller_key" not in have:
            try:
                conn.execute(
                    "ALTER TABLE host_operator_bindings "
                    "ADD COLUMN caller_key TEXT NOT NULL DEFAULT ''",
                )
            except Exception:
                # A racing process already added it.
                pass
        # The backfill runs even when the column already existed: a row that
        # gained a window AFTER an earlier migration would otherwise keep an
        # empty caller_key forever and silently stop resolving.
        try:
            conn.execute(
                "UPDATE host_operator_bindings SET caller_key = 'win:' || window_key "
                "WHERE caller_key = '' AND window_key <> ''",
            )
        except Exception:
            pass

    def _add_window_key_column(self, conn: sqlite3.Connection) -> None:
        """Add ``window_key`` when this DB predates it. Idempotent."""
        try:
            have = {
                str(r[1])
                for r in conn.execute("PRAGMA table_info(host_operator_bindings)")
            }
        except Exception:
            return
        if "window_key" in have:
            return
        try:
            conn.execute(
                "ALTER TABLE host_operator_bindings "
                "ADD COLUMN window_key TEXT NOT NULL DEFAULT ''",
            )
        except Exception:
            # A racing process already added it; the PRAGMA above will see it.
            pass

    # ------------------------------------------------------------------
    # Host side: create a pending binding (/aidocs)
    # ------------------------------------------------------------------

    def create_pending(
        self,
        project_root: Path,
        *,
        host_kind: str,
        host_session_id: str,
        aidocs_session_id: str = "",
        requested_identity: str = "",
        window_key: str = "",
        caller_key: str = "",
        ttl_seconds: int = _DEFAULT_PENDING_TTL_SECONDS,
    ) -> tuple[str, str]:
        """Create a pending binding. Returns ``(binding_id,
        pairing_code)``. The pairing_code is plaintext (shown in the
        host chat once); only its hash is persisted.

        Idempotent-ish: if an un-expired pending binding already
        exists for this host_session_id, it is superseded (marked
        expired) and a fresh one minted, so a re-run of /aidocs
        doesn't accrete duplicate pending rows.
        """
        self.init_db(project_root)
        now = time.time()
        binding_id = "hb_" + secrets.token_hex(10)
        # 6-digit pairing code — easy to read aloud / type, low entropy
        # is fine because approval also requires an authenticated
        # operator AND a short TTL.
        pairing_code = f"{secrets.randbelow(1_000_000):06d}"
        with self._connect(project_root) as conn:
            # Supersede any live pending rows for this host session.
            conn.execute(
                "UPDATE host_operator_bindings SET status='expired' "
                "WHERE replace(lower(host_session_id),'-','')=? "
                "AND status='pending'",
                (_canon_sid(host_session_id),),
            )
            conn.execute(
                """
                INSERT INTO host_operator_bindings
                    (binding_id, host_kind, host_session_id,
                     project_root, aidocs_session_id, pairing_code_hash,
                     requested_identity, window_key, caller_key, status,
                     created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    binding_id,
                    host_kind,
                    host_session_id,
                    str(project_root).replace("\\", "/"),
                    aidocs_session_id,
                    _hash_code(pairing_code),
                    requested_identity,
                    # A key that is not a key is not stored -- the same write-
                    # side rule window_binding_store enforces. #880 records what
                    # skipping it cost: 'auth-truth-614', a synthetic test id,
                    # seated permanently in an authority structure.
                    _canon_window(window_key),
                    # THE CALLER KEY, VALIDATED BY ITS OWN CLASS. An explicit
                    # caller_key wins (the remote surface has no window at all);
                    # otherwise it is derived from the window so the local
                    # pairing path needs no change. A key that does not parse
                    # is stored EMPTY rather than raw -- the same write-side
                    # rule as the window above, and for the same reason: a
                    # well-formed-LOOKING key nobody minted is worse than none.
                    _canon_caller(caller_key, window_key),
                    _iso(now),
                    _iso(now + max(60, int(ttl_seconds))),
                ),
            )
            conn.commit()
        return binding_id, pairing_code

    # ------------------------------------------------------------------
    # Dashboard side: list + approve
    # ------------------------------------------------------------------

    def _expire_stale(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "UPDATE host_operator_bindings SET status='expired' "
            "WHERE status='pending' AND expires_at <= ?",
            (_iso_now(),),
        )

    def list_pending(self, project_root: Path) -> list[HostBinding]:
        """List currently-pending (not expired) bindings for the
        dashboard's approval queue. Lazily expires stale rows first.
        """
        self.init_db(project_root)
        with self._connect(project_root) as conn:
            self._expire_stale(conn)
            conn.commit()
            rows = conn.execute(
                "SELECT * FROM host_operator_bindings WHERE status='pending' ORDER BY created_at",
            ).fetchall()
        return [self._row(r) for r in rows]

    def list_bindings(
        self,
        project_root: Path,
        statuses: tuple[str, ...] = ("pending", "approved"),
    ) -> list[HostBinding]:
        """List bindings in the given statuses for this project — the
        `aidocs bindings` one-glance surface (pending + approved by
        default). Lazily expires stale pending rows first, same as
        ``list_pending``.
        """
        self.init_db(project_root)
        wanted = tuple(str(s) for s in statuses) or ("pending", "approved")
        marks = ",".join("?" for _ in wanted)
        with self._connect(project_root) as conn:
            self._expire_stale(conn)
            conn.commit()
            rows = conn.execute(
                "SELECT * FROM host_operator_bindings "
                f"WHERE status IN ({marks}) ORDER BY created_at",
                wanted,
            ).fetchall()
        return [self._row(r) for r in rows]

    def approve(
        self,
        project_root: Path,
        *,
        binding_id: str,
        operator_user_id: str,
        approved_by_role: str,
        pairing_code: str | None = None,
    ) -> tuple[bool, str]:
        """Atomically bind operator_user_id to the host session.

        Returns ``(ok, reason)``. The UPDATE is guarded by
        ``status='pending' AND expires_at > now`` so:
          - a second concurrent approve sees rowcount 0 → loses the
            race cleanly (reason='already_resolved')
          - an expired pending can't be approved (reason='expired')
          - a wrong pairing code (when provided) is rejected BEFORE
            the update (reason='bad_code')

        ``pairing_code`` is optional: the dashboard "Bind to me"
        button approves by binding_id (the operator sees the queue);
        a stricter flow can also require the code typed from the host.
        """
        if not operator_user_id:
            return False, "no_operator"
        self.init_db(project_root)
        now_iso = _iso_now()
        with self._connect(project_root) as conn:
            row = conn.execute(
                "SELECT status, expires_at, pairing_code_hash "
                "FROM host_operator_bindings WHERE binding_id=?",
                (binding_id,),
            ).fetchone()
            if row is None:
                return False, "not_found"
            if row["status"] != "pending":
                return False, "already_resolved"
            if str(row["expires_at"]) <= now_iso:
                # Mark it expired so the queue self-cleans.
                conn.execute(
                    "UPDATE host_operator_bindings SET status='expired' "
                    "WHERE binding_id=? AND status='pending'",
                    (binding_id,),
                )
                conn.commit()
                return False, "expired"
            if pairing_code is not None:
                if _hash_code(pairing_code) != row["pairing_code_hash"]:
                    return False, "bad_code"
            # Atomic pending→approved. The WHERE clause re-checks
            # status so a racing approve can't double-bind.
            cur = conn.execute(
                "UPDATE host_operator_bindings "
                "SET status='approved', operator_user_id=?, "
                "    approved_at=?, approved_by_role=? "
                "WHERE binding_id=? AND status='pending' "
                "      AND expires_at > ?",
                (
                    operator_user_id,
                    now_iso,
                    approved_by_role,
                    binding_id,
                    now_iso,
                ),
            )
            conn.commit()
            if (cur.rowcount or 0) == 0:
                return False, "already_resolved"
        return True, "approved"

    def revoke(
        self,
        project_root: Path,
        *,
        binding_id: str,
    ) -> bool:
        """Revoke a binding (approved or pending), no ownership check.

        Low-level primitive — callers that need owner-or-admin
        enforcement use ``revoke_with_owner_check``. Returns True iff
        a row transitioned to revoked.
        """
        self.init_db(project_root)
        with self._connect(project_root) as conn:
            cur = conn.execute(
                "UPDATE host_operator_bindings SET status='revoked' "
                "WHERE binding_id=? AND status IN ('pending','approved')",
                (binding_id,),
            )
            conn.commit()
        return (cur.rowcount or 0) > 0

    def revoke_with_owner_check(
        self,
        project_root: Path,
        *,
        binding_id: str,
        requester_user_id: str,
        is_admin: bool,
    ) -> tuple[bool, str]:
        """Owner-or-admin revoke. Returns ``(ok, reason)``.

        Authorization matrix:
          - approved binding, operator_user_id == requester  → owner,
            allowed.
          - approved binding, operator_user_id != requester  → allowed
            ONLY when is_admin (admin override), else 'not_owner'.
          - pending binding (operator_user_id == '')          → unowned;
            no operator has claimed it, so only an admin may decline
            it ('pending_requires_admin' otherwise). This is the
            explicit rule for unowned-pending revoke.
          - missing                                           → 'not_found'.
          - already revoked / expired                         →
            'already_resolved'.

        The actual transition is atomic (UPDATE WHERE status IN
        (pending,approved)) so a concurrent revoke can't double-count.
        """
        if not requester_user_id and not is_admin:
            return False, "unauthenticated"
        self.init_db(project_root)
        with self._connect(project_root) as conn:
            row = conn.execute(
                "SELECT status, operator_user_id FROM host_operator_bindings WHERE binding_id=?",
                (binding_id,),
            ).fetchone()
            if row is None:
                return False, "not_found"
            status = str(row["status"])
            owner = str(row["operator_user_id"] or "")
            if status not in ("pending", "approved"):
                return False, "already_resolved"
            # Authorization decision.
            if owner and owner == requester_user_id:
                pass  # owner revoke
            elif is_admin:
                pass  # admin override (owned or unowned)
            elif owner == "":
                return False, "pending_requires_admin"
            else:
                return False, "not_owner"
            cur = conn.execute(
                "UPDATE host_operator_bindings SET status='revoked' "
                "WHERE binding_id=? AND status IN ('pending','approved')",
                (binding_id,),
            )
            conn.commit()
            if (cur.rowcount or 0) == 0:
                return False, "already_resolved"
        return True, "revoked"

    # ------------------------------------------------------------------
    # Hook side: resolve the bound operator for a host session
    # ------------------------------------------------------------------

    def resolve_operator(
        self,
        project_root: Path,
        host_session_id: str,
    ) -> str | None:
        """Return the operator_user_id bound to host_session_id, or
        None when no approved+live binding exists. Used by hooks to
        build an operator_context from the host's session UUID.

        Only an ``approved`` row that has not expired resolves. A
        revoked binding returns None (the hook falls back to
        unauthenticated).
        """
        if not host_session_id:
            return None
        self.init_db(project_root)
        with self._connect(project_root) as conn:
            row = conn.execute(
                "SELECT operator_user_id, expires_at, status "
                "FROM host_operator_bindings "
                "WHERE replace(lower(host_session_id),'-','')=? "
                "AND status='approved' "
                "ORDER BY approved_at DESC LIMIT 1",
                (_canon_sid(host_session_id),),
            ).fetchone()
        if row is None:
            return None
        # Approved bindings outlive the pending TTL — once bound, the
        # operator stays bound until revoked. (expires_at governs the
        # PENDING window only; we keep the column for audit but do not
        # expire an approved binding on it.)
        uid = str(row["operator_user_id"] or "")
        return uid or None

    def resolve_operator_by_caller(
        self,
        project_root: Path,
        caller_key: str,
    ) -> str | None:
        """The operator bound to THIS CALLER, or ``None``. THE identity lookup.

        Keyed on the NAMESPACED caller key and nothing else -- not the
        conversation, not the managed session, not the token, and not "the
        newest approved row we have for this project". A caller with no row of
        its own resolves ``None`` even when the store holds an approved binding
        for every other caller in the project: SESSION-SCOPING WHO WOULD GRANT
        THE OPERATOR'S super_admin TO EVERY CALLER IN THE SESSION, which is the
        negative this work exists to make impossible.

        THE CLASS TRAVELS IN THE KEY, so the two attestation classes cannot
        answer for one another. A remote conversation whose value happened to
        read like ``13336:1343...`` is stored as ``rconv:...`` and can never
        match the OS window ``win:13336:1343...``. A bare value column would
        have made that collision reachable.

        No OR clause, and NO RECENCY TIEBREAK. An earlier revision carried a
        docstring promising the absence of ``ORDER BY ... LIMIT 1`` directly
        above a query that used it, so the guarantee read as kept while the
        code broke it.

        AMBIGUITY IS NOT A TIE TO BREAK. A caller carrying approved rows for
        more than one operator resolves ``None``, not "whoever approved last".
        Two people can pair from one physical window with no bug at all --
        ``create_pending`` takes the caller key directly, so a sign-out and a
        sign-in is enough. Ordering by ``approved_at`` there does not RECOVER
        who is present, it GUESSES, and a guess that lands on a super_admin is
        exactly the substitution this spine exists to forbid. Measured before
        this guard: two approved operators on one window answered with one of
        them, on an arbitrary tiebreak.
        """
        from .caller_attestation import parse_key

        attestation, _reason = parse_key(caller_key)
        if attestation is None:
            return None
        self.init_db(project_root)
        with self._connect(project_root) as conn:
            rows = conn.execute(
                "SELECT DISTINCT operator_user_id FROM host_operator_bindings "
                "WHERE caller_key = ? AND status='approved'",
                (attestation.key,),
            ).fetchall()
        users = {str(r["operator_user_id"] or "").strip() for r in rows}
        users.discard("")
        if len(users) != 1:
            # 0 -> unbound; >1 -> ambiguous. Both fail closed, identically.
            return None
        return users.pop()


    def adopt_window(
        self,
        project_root: Path,
        *,
        window_key: str,
        host_session_ids: tuple[str, ...],
    ) -> str | None:
        """AT MOST ONCE: claim a pre-window binding for ``window_key``.

        THE MIGRATION PATH, and nothing more. Bindings approved before the
        window column existed are filed under a conversation alone; without
        this they stay orphaned the moment that id rotates, and every operator
        on every box would have to re-pair.

        TWO HOST-ATTESTED FACTS MEET, and no third is invented. The lease says
        this window declared these conversations (written by SessionStart from
        the host's own payload); the binding row says an authenticated operator
        approved one of them. The caller passes ONLY conversations that THIS
        window's own lease row names — never a conversation it merely shares a
        session with.

        ``window_key = ''`` in the UPDATE's WHERE clause is what makes this
        once-only: an adopted row is owned, and a second window presenting the
        same conversation matches nothing. That is the inheritance negative
        enforced in SQL rather than promised in prose.
        """
        window = _canon_window(window_key)
        candidates = [_canon_sid(s) for s in host_session_ids if _canon_sid(s)]
        if not window or not candidates:
            return None
        self.init_db(project_root)
        marks = ",".join("?" for _ in candidates)
        with self._connect(project_root) as conn:
            # AMBIGUITY IS CHECKED BEFORE THE WRITE, not after it. The UPDATE
            # below is deliberately unbounded -- a window's lease legitimately
            # names several conversations, and all of that window's own rows
            # should be claimed together. That is only safe while they are ONE
            # operator's rows. If the candidate set spans two operators, or the
            # window already holds an approved row for a different one, there
            # is no fact here that says which of them is present, and stamping
            # both would hand the answer to a recency tiebreak downstream.
            # Refuse, leaving the rows orphaned and resolvable only by an
            # explicit `restamp_window` naming both ids.
            owners = {
                str(r["operator_user_id"] or "").strip()
                for r in conn.execute(
                    "SELECT DISTINCT operator_user_id FROM host_operator_bindings "
                    f"WHERE status='approved' AND (window_key = ? OR (window_key = '' "
                    f"AND replace(lower(host_session_id),'-','') IN ({marks})))",
                    (window, *candidates),
                ).fetchall()
            }
            owners.discard("")
            if len(owners) > 1:
                return None
            # BOTH COLUMNS, ONE STATEMENT. caller_key is what resolution reads;
            # a row stamped with only a window would look adopted and resolve
            # to nothing, which is the quietest possible way to lock an
            # operator out.
            cur = conn.execute(
                "UPDATE host_operator_bindings SET window_key = ?, caller_key = ? "
                "WHERE window_key = '' AND status='approved' "
                f"AND replace(lower(host_session_id),'-','') IN ({marks})",
                (window, _canon_caller("", window), *candidates),
            )
            conn.commit()
            if (cur.rowcount or 0) == 0:
                return None
            # ANSWERS FROM THE SAME RULE THE RESOLVER USES, not from a
            # neighbour's invariant. This read used to be
            # `ORDER BY approved_at DESC LIMIT 1` -- the exact shape that made
            # resolve_operator_by_window answer WHO by recency. It was safe
            # here ONLY because the guard above had already excluded a second
            # owner: correct because of a fact enforced twenty lines away.
            # That is how the original defect was built, so it does not stay.
            # Exactly one owner, or nothing.
            rows = conn.execute(
                "SELECT DISTINCT operator_user_id FROM host_operator_bindings "
                "WHERE window_key = ? AND status='approved'",
                (window,),
            ).fetchall()
        owners = {str(r["operator_user_id"] or "").strip() for r in rows}
        owners.discard("")
        if len(owners) != 1:
            return None
        return owners.pop()

    def restamp_window(
        self,
        project_root: Path,
        *,
        binding_id: str,
        window_key: str,
    ) -> tuple[bool, str]:
        """Point ONE named approved binding at ONE named window. ``(ok, reason)``.

        THE MIGRATION COMMAND, for the bindings ``adopt_window`` cannot reach.
        Measured on the operator's box 2026-09-04: their approved super_admin
        binding sits under conversation ``74a03862``, while their live window
        ``30948:134330251516104858`` holds ``f4c093aa`` with an EMPTY
        ``previous_host_session_id`` — the rotation happened before the lease
        table existed, so no attested fact connects the two and automatic
        adoption correctly refuses. Without this they stay locked out.

        WHY THIS IS NOT THE FALLBACK IT LOOKS LIKE. It infers nothing. Both the
        binding and the window are named EXPLICITLY by an operator who can see
        both; widening adoption to "any approved row for this user" would have
        healed it silently and would also have handed any window the newest
        binding in the store, which is the substitution this programme exists
        to remove. A human naming two ids is evidence; a resolver guessing
        between them is the bug.

        Refuses rather than overwrites when the row is already window-keyed:
        re-pointing a live window binding is a different, more dangerous act
        than migrating an unkeyed one, and it does not happen by accident here.
        """
        window = _canon_window(window_key)
        if not window:
            return False, "not_a_window_key"
        if not str(binding_id or "").strip():
            return False, "no_binding_id"
        self.init_db(project_root)
        with self._connect(project_root) as conn:
            row = conn.execute(
                "SELECT status, window_key FROM host_operator_bindings "
                "WHERE binding_id=?",
                (binding_id,),
            ).fetchone()
            if row is None:
                return False, "not_found"
            if row["status"] != "approved":
                return False, f"not_approved:{row['status']}"
            existing = self._row_window_key(row)
            if existing and existing != window:
                return False, f"already_bound_to_window:{existing}"
            conn.execute(
                # Both columns, for the reason adopt_window states: caller_key
                # is what resolution reads, so a window-only restamp would
                # report success and still resolve to nothing.
                "UPDATE host_operator_bindings SET window_key=?, caller_key=? "
                "WHERE binding_id=? AND status='approved'",
                (window, _canon_caller("", window), binding_id),
            )
            conn.commit()
        return True, "restamped"

    def get(
        self,
        project_root: Path,
        binding_id: str,
    ) -> HostBinding | None:
        self.init_db(project_root)
        with self._connect(project_root) as conn:
            row = conn.execute(
                "SELECT * FROM host_operator_bindings WHERE binding_id=?",
                (binding_id,),
            ).fetchone()
        return self._row(row) if row else None

    def _row_window_key(self, r: sqlite3.Row) -> str:
        """``window_key`` off a row, ``""`` on a row that predates the column.

        ``SELECT *`` against a DB whose migration has not run yet yields a row
        without the key. Reported as the honest empty — which is exactly the
        "not yet adopted" state ``adopt_window`` looks for.
        """
        try:
            return str(r["window_key"] or "")
        except (IndexError, KeyError):
            return ""

    def _row(self, r: sqlite3.Row) -> HostBinding:
        return HostBinding(
            binding_id=r["binding_id"],
            host_kind=r["host_kind"],
            host_session_id=r["host_session_id"],
            window_key=self._row_window_key(r),
            project_root=r["project_root"],
            aidocs_session_id=r["aidocs_session_id"],
            requested_identity=r["requested_identity"],
            status=r["status"],
            operator_user_id=r["operator_user_id"],
            created_at=r["created_at"],
            expires_at=r["expires_at"],
            approved_at=r["approved_at"],
            approved_by_role=r["approved_by_role"],
        )
