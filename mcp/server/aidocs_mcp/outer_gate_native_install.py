"""#1074 round 6 -- the per-install native credential registry (gate side).

Operator ruling (verbatim): "i want a real id, ogc_desktop is wrong ... every
aidocs local install on all machines uses that id?". A shared public OAuth
client id is not an identity. This store holds the REAL one: an install the
signed-in user enrolled through a fresh interactive desktop sign-in, bound to
a P-256 key whose private half never leaves that machine.

Tables (identity DB -- the SAME database as the OAuth store and the token
store, so enrollment + both token mints commit in ONE transaction):

``native_install``      install_id (server-issued ``ogni_*``), user_id,
                        tenant_id, client_id, created_at, last_seen_at,
                        revoked_at, revoked_by
``native_install_key``  key_id (server-issued ``ognk_*``), install_id,
                        jwk_thumbprint (UNIQUE), public_jwk_json,
                        created_at, revoked_at
``native_proof_jti``    (install_id, jti) PRIMARY KEY, exp -- the replay set,
                        kept through exp + 10 s then pruned

Reviewer 7aa06b3f-1ad amendments carried here:
  B1  the JKT is the enrollment-time binding only; the durable key id is the
      server-issued ``ognk_*``.
  B2  the same ACTIVE key + same user + same client re-authorizes the SAME
      install; a revoked install or key never resurrects; another user's JKT
      refuses. v1 has no rotation: replacement = revoke, then a new key and a
      new install.
  Q2  revoke = the install, its keys AND that install's token family (refresh
      + live access), never the user's other tokens.

Reads are STRICT: a missing table/column or a storage fault raises
:class:`NativeInstallUnavailable`; callers treat that as "not proven", never
as a pass.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ._sqlite_connect import Durability as _Durability
from ._sqlite_connect import connect as _canonical_connect

INSTALL_PREFIX = "ogni_"
KEY_PREFIX = "ognk_"
#: last_seen_at is TELEMETRY, written at most this often per install (O6).
LAST_SEEN_THROTTLE_S = 300

REQUIRED_COLUMNS = {
    "native_install": frozenset({"install_id", "user_id", "tenant_id", "client_id", "created_at",
                                 "last_seen_at", "revoked_at", "revoked_by"}),
    "native_install_key": frozenset({"key_id", "install_id", "jwk_thumbprint", "public_jwk_json",
                                     "created_at", "revoked_at"}),
    "native_proof_jti": frozenset({"install_id", "jti", "exp"}),
}


class NativeInstallUnavailable(RuntimeError):
    """The install registry cannot be read as designed (drift / storage fault)."""


class EnrollmentRefused(ValueError):
    """An enrollment is refused; ``reason`` is non-disclosing and stable."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class KeyRecord:
    key_id: str
    install_id: str
    jkt: str
    public_jwk: dict
    key_revoked: bool
    install_revoked: bool
    user_id: str
    client_id: str
    tenant_id: str

    @property
    def active(self) -> bool:
        return not self.key_revoked and not self.install_revoked


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class NativeInstallStore:
    """The install registry. Every write is audit-durable."""

    def db_path(self, project_root: Path) -> Path:
        from .identity_store import IdentityStore

        return IdentityStore().db_path(project_root)

    @staticmethod
    def ensure_schema(conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS native_install ("
            " install_id TEXT PRIMARY KEY,"
            " user_id TEXT NOT NULL,"
            " tenant_id TEXT NOT NULL DEFAULT '',"
            " client_id TEXT NOT NULL,"
            " created_at TEXT NOT NULL,"
            " last_seen_at TEXT NOT NULL DEFAULT '',"
            " revoked_at TEXT NOT NULL DEFAULT '',"
            " revoked_by TEXT NOT NULL DEFAULT '')"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_native_install_user ON native_install(user_id)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS native_install_key ("
            " key_id TEXT PRIMARY KEY,"
            " install_id TEXT NOT NULL,"
            " jwk_thumbprint TEXT NOT NULL UNIQUE,"
            " public_jwk_json TEXT NOT NULL,"
            " created_at TEXT NOT NULL,"
            " revoked_at TEXT NOT NULL DEFAULT '')"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS native_proof_jti ("
            " install_id TEXT NOT NULL,"
            " jti TEXT NOT NULL,"
            " exp INTEGER NOT NULL,"
            " PRIMARY KEY (install_id, jti))"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_native_proof_jti_exp ON native_proof_jti(exp)")

    def init_db(self, project_root: Path) -> None:
        from .identity_store import IdentityStore

        IdentityStore().init_db(project_root)
        with _canonical_connect(self.db_path(project_root), durability=_Durability.AUDIT,
                                row_factory=False) as conn:
            self.ensure_schema(conn)
            conn.commit()

    @staticmethod
    def _assert_schema(conn: sqlite3.Connection) -> None:
        for table, need in REQUIRED_COLUMNS.items():
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if not need <= cols:
                raise NativeInstallUnavailable(f"{table} schema drift")

    # -- enrollment (inside the CALLER's transaction) ----------------------

    def enroll_in_tx(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: str,
        client_id: str,
        tenant_id: str,
        jkt: str,
        public_jwk: dict,
    ) -> tuple[str, str, bool]:
        """Insert or REUSE the install for ``jkt`` on the caller's open
        transaction. Returns ``(install_id, key_id, reused)``. Never commits."""
        uid, cid = str(user_id or "").strip(), str(client_id or "").strip()
        if not uid or not cid or not jkt:
            raise EnrollmentRefused("enrollment_identity_missing")
        self.ensure_schema(conn)
        self._assert_schema(conn)
        row = conn.execute(
            "SELECT k.key_id, k.install_id, k.revoked_at AS key_revoked, i.user_id, i.client_id, "
            "i.revoked_at AS install_revoked FROM native_install_key k "
            "LEFT JOIN native_install i ON i.install_id = k.install_id WHERE k.jwk_thumbprint = ?",
            (jkt,),
        ).fetchone()
        if row is not None:
            key_id, install_id, key_rev, owner, owner_client, inst_rev = row
            if owner is None:
                raise EnrollmentRefused("install_record_missing")
            if key_rev or inst_rev:
                raise EnrollmentRefused("install_revoked")  # never resurrect
            if owner != uid or owner_client != cid:
                raise EnrollmentRefused("key_bound_to_another_principal")
            return str(install_id), str(key_id), True
        now = _iso(time.time())
        install_id = INSTALL_PREFIX + secrets.token_urlsafe(18)
        key_id = KEY_PREFIX + secrets.token_urlsafe(18)
        conn.execute(
            "INSERT INTO native_install (install_id, user_id, tenant_id, client_id, created_at) "
            "VALUES (?,?,?,?,?)",
            (install_id, uid, str(tenant_id or ""), cid, now),
        )
        conn.execute(
            "INSERT INTO native_install_key (key_id, install_id, jwk_thumbprint, public_jwk_json, "
            "created_at) VALUES (?,?,?,?,?)",
            (key_id, install_id, jkt, json.dumps(public_jwk, sort_keys=True), now),
        )
        return install_id, key_id, False

    # -- strict reads -------------------------------------------------------

    def key(self, project_root: Path, key_id: str) -> KeyRecord | None:
        kid = str(key_id or "").strip()
        if not kid.startswith(KEY_PREFIX):
            return None
        try:
            with _canonical_connect(self.db_path(project_root), durability=_Durability.AUDIT,
                                    row_factory=False) as conn:
                self._assert_schema(conn)
                row = conn.execute(
                    "SELECT k.key_id, k.install_id, k.jwk_thumbprint, k.public_jwk_json, k.revoked_at, "
                    "i.revoked_at, i.user_id, i.client_id, i.tenant_id FROM native_install_key k "
                    "JOIN native_install i ON i.install_id = k.install_id WHERE k.key_id = ?",
                    (kid,),
                ).fetchone()
        except NativeInstallUnavailable:
            raise
        except sqlite3.Error as exc:
            raise NativeInstallUnavailable(f"install registry unreadable: {type(exc).__name__}") from exc
        if row is None:
            return None
        try:
            jwk = json.loads(row[3])
        except ValueError as exc:
            raise NativeInstallUnavailable("stored JWK is corrupt") from exc
        return KeyRecord(
            key_id=str(row[0]), install_id=str(row[1]), jkt=str(row[2]), public_jwk=jwk,
            key_revoked=bool(row[4]), install_revoked=bool(row[5]), user_id=str(row[6]),
            client_id=str(row[7]), tenant_id=str(row[8] or ""),
        )

    # -- replay set ---------------------------------------------------------

    def consume_jti(self, project_root: Path, install_id: str, jti: str, exp: int, *,
                    now: int | None = None) -> bool:
        """Atomically record ``jti``. False = already seen (replay). Prunes
        entries past exp + skew in the same transaction."""
        from .native_proof import SKEW_S

        t = int(time.time()) if now is None else int(now)
        try:
            with _canonical_connect(self.db_path(project_root), durability=_Durability.AUDIT,
                                    row_factory=False) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute("DELETE FROM native_proof_jti WHERE exp + ? < ?", (SKEW_S, t))
                    conn.execute(
                        "INSERT INTO native_proof_jti (install_id, jti, exp) VALUES (?,?,?)",
                        (str(install_id), str(jti), int(exp)),
                    )
                except sqlite3.IntegrityError:
                    conn.rollback()
                    return False
                except BaseException:
                    conn.rollback()
                    raise
                conn.commit()
                return True
        except sqlite3.Error as exc:
            raise NativeInstallUnavailable(f"replay set unwritable: {type(exc).__name__}") from exc

    # -- telemetry ----------------------------------------------------------

    def _write_last_seen(self, project_root: Path, install_id: str, now: int) -> None:
        with _canonical_connect(self.db_path(project_root), durability=_Durability.AUDIT,
                                row_factory=False) as conn:
            conn.execute(
                "UPDATE native_install SET last_seen_at=? WHERE install_id=? "
                "AND (last_seen_at='' OR last_seen_at <= ?)",
                (_iso(now), str(install_id), _iso(now - LAST_SEEN_THROTTLE_S)),
            )
            conn.commit()

    def touch_last_seen(self, project_root: Path, install_id: str, *, now: int | None = None) -> None:
        """Best-effort, throttled. NEVER an admission condition (O6)."""
        try:
            self._write_last_seen(project_root, install_id, int(time.time()) if now is None else int(now))
        except Exception:  # noqa: BLE001 -- telemetry must never decide admission
            pass

    # -- owner surface ------------------------------------------------------

    def list_for_user(self, project_root: Path, user_id: str) -> list[dict]:
        uid = str(user_id or "").strip()
        if not uid:
            return []
        self.init_db(project_root)
        with _canonical_connect(self.db_path(project_root), durability=_Durability.AUDIT) as conn:
            rows = conn.execute(
                "SELECT install_id, client_id, created_at, last_seen_at, revoked_at FROM native_install "
                "WHERE user_id=? ORDER BY created_at",
                (uid,),
            ).fetchall()
            keys = conn.execute(
                "SELECT k.install_id, k.key_id FROM native_install_key k JOIN native_install i "
                "ON i.install_id = k.install_id WHERE i.user_id=?",
                (uid,),
            ).fetchall()
        by_install: dict[str, list[str]] = {}
        for k in keys:
            by_install.setdefault(str(k["install_id"]), []).append(str(k["key_id"]))
        return [
            {
                "install_id": str(r["install_id"]),
                "client_id": str(r["client_id"]),
                "created_at": str(r["created_at"]),
                "last_seen_at": str(r["last_seen_at"]),
                "revoked_at": str(r["revoked_at"]),
                "key_ids": sorted(by_install.get(str(r["install_id"]), [])),
            }
            for r in rows
        ]

    def revoke(self, project_root: Path, install_id: str, *, user_id: str) -> bool:
        """Owner-only. ONE transaction: the install, its keys, and its token
        FAMILY (refresh + live access). False = not found for this owner."""
        from .outer_gate_token_store import OuterGateTokenStore

        iid, uid = str(install_id or "").strip(), str(user_id or "").strip()
        if not iid or not uid:
            return False
        OuterGateTokenStore().init_db(project_root)
        self.init_db(project_root)
        now = _iso(time.time())
        with _canonical_connect(self.db_path(project_root), durability=_Durability.AUDIT,
                                row_factory=False) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    "UPDATE native_install SET revoked_at=?, revoked_by=? "
                    "WHERE install_id=? AND user_id=? AND revoked_at=''",
                    (now, uid, iid, uid),
                )
                if cur.rowcount == 0:
                    conn.rollback()
                    return False
                conn.execute(
                    "UPDATE native_install_key SET revoked_at=? WHERE install_id=? AND revoked_at=''",
                    (now, iid),
                )
                OuterGateTokenStore.revoke_family_in_tx(conn, iid, now=now)
            except BaseException:
                conn.rollback()
                raise
            conn.commit()
        return True


# -- the per-call proof (stage 3a) --------------------------------------------


def verify_native_request(
    project_root: Path,
    *,
    proof: str,
    method: str,
    raw_body: bytes,
    bearer: str,
    principal: dict,
    now: int | None = None,
    store: NativeInstallStore | None = None,
) -> tuple[bool, str]:
    """``(proven, reason)`` for a native ``POST /v1/mcp``.

    Order (reviewer ruling): size/JOSE -> registered key -> signature ->
    htm/htu/raw-body/ath/user/install/client/lifetime -> atomic jti consume.
    INTERSECTION: the bearer's validated principal must already be
    authenticated, and the install never grants anything -- it only answers
    "is this the native edge"."""
    from . import native_proof as npf

    p = principal if isinstance(principal, dict) else {}
    if not p.get("authenticated") or not str(p.get("user_id") or "").strip():
        return False, "unauthenticated"
    if not bearer:
        return False, "no_bearer"
    if not proof:
        return False, "no_proof"
    if str(method or "").upper() != "POST":
        return False, "method"
    st = store or NativeInstallStore()
    t = int(time.time()) if now is None else int(now)
    found: dict = {}

    def _key_for(kid: str):
        rec = st.key(project_root, kid)
        if rec is None or not rec.active:
            return None
        found["rec"] = rec
        return npf.public_key_from_jwk(rec.public_jwk)

    try:
        _header, claims = npf.verify(proof, expected_typ=npf.TYP_PROOF, key_for_kid=_key_for)
        rec: KeyRecord = found["rec"]
        if claims.get("htm") != "POST":
            return False, "htm"
        if claims.get("htu") != npf.canonical_mcp_url(npf.server_public_base()):
            return False, "htu"
        if claims.get("body_sha256") != npf.sha256_b64url(raw_body):
            return False, "body"
        if claims.get("ath") != npf.ath_for(bearer):
            return False, "ath"
        if claims.get("install_id") != rec.install_id:
            return False, "install_claim"
        if str(p.get("install_id") or "") != rec.install_id:
            return False, "token_install"
        if str(p.get("user_id") or "") != rec.user_id:
            return False, "user"
        if str(p.get("client_id") or "") != rec.client_id:
            return False, "client"
        npf.check_lifetime(claims, now=t)
        jti = npf.check_jti(claims)
    except npf.NativeProofError:
        return False, "proof_refused"
    except NativeInstallUnavailable:
        return False, "registry_unavailable"
    try:
        if not st.consume_jti(project_root, rec.install_id, jti, int(claims["exp"]), now=t):
            return False, "replay"
    except NativeInstallUnavailable:
        return False, "registry_unavailable"
    st.touch_last_seen(project_root, rec.install_id, now=t)
    return True, ""
