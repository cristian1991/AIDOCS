"""Identity store — Layer 9 corporate-ready deliverable C-1.

First Layer 9 foundation: user identities + session tokens + role
enumeration. No SSO/SAML yet (that's C-3) and no project ACLs (C-4)
— this module just owns the primitive types and storage so the
rest of Layer 9 can compose against a stable shape.

Storage: sqlite table `identity_users` per project. Password hashes
use bcrypt when available; fall back to a clearly-flagged SHA256+salt
path for environments that can't install bcrypt (dev machines) so
tests can run without the native extension.

Role is stored as free-text. Validation/hierarchy lives in
rbac_store (C-2) which owns the dynamic role catalog + permission
matrix. This module only persists the string tag so identity rows
remain valid across role renames.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path

# ── Roles ──

ROLE_ADMIN = "admin"
ROLE_CONDUCTOR = "conductor"
ROLE_OBSERVER = "observer"

VALID_ROLES: frozenset[str] = frozenset(
    {
        ROLE_ADMIN,
        ROLE_CONDUCTOR,
        ROLE_OBSERVER,
    },
)


# ── Data types ──


@dataclass(frozen=True)
class User:
    """One identity row.

    user_id: stable UUID-ish identifier; never reused.
    email: canonical login. Lowercased + trimmed on write.
    role: must be in VALID_ROLES.
    created_at: ISO-8601 UTC timestamp.
    disabled: admin can revoke without deleting history.
    """

    user_id: str
    email: str
    role: str
    created_at: str
    disabled: bool = False


@dataclass(frozen=True)
class SessionToken:
    """Opaque bearer token tying a session to a user.

    token: high-entropy random string; hashed at rest.
    user_id: owning user.
    issued_at / expires_at: ISO-8601 UTC timestamps. Tokens are
        short-lived by default (12h) — refresh is a separate C-3
        concern; this store only issues+validates.
    """

    token: str
    user_id: str
    issued_at: str
    expires_at: str


# ── Hashing ──

_DEFAULT_TOKEN_TTL_SECONDS = 12 * 3600


def _hash_password(password: str) -> str:
    """Return a hash-string suitable for storage.

    Tries bcrypt first (industry standard), falls back to a salted
    SHA256 with a clear scheme marker so the verifier can tell which
    family it got. The fallback is only for test/dev environments
    that haven't installed bcrypt — production installs must include
    it.
    """
    try:
        import bcrypt  # type: ignore[import-not-found]
    except ImportError:
        salt = secrets.token_hex(16)
        digest = hashlib.sha256(
            (salt + password).encode("utf-8"),
        ).hexdigest()
        return f"sha256${salt}${digest}"
    else:
        hashed = bcrypt.hashpw(
            password.encode("utf-8"),
            bcrypt.gensalt(rounds=12),
        )
        return "bcrypt$" + hashed.decode("utf-8")


def _verify_password(password: str, stored: str) -> bool:
    """Check a plaintext password against a stored hash."""
    if not password or not stored:
        return False
    if stored.startswith("bcrypt$"):
        try:
            import bcrypt  # type: ignore[import-not-found]
        except ImportError:
            return False
        return bool(
            bcrypt.checkpw(
                password.encode("utf-8"),
                stored.removeprefix("bcrypt$").encode("utf-8"),
            ),
        )
    if stored.startswith("sha256$"):
        try:
            _, salt, digest = stored.split("$", 2)
        except ValueError:
            return False
        expected = hashlib.sha256(
            (salt + password).encode("utf-8"),
        ).hexdigest()
        return hmac.compare_digest(expected, digest)
    return False


def _hash_token(token: str) -> str:
    """Tokens are also hashed at rest — stolen DB doesn't grant
    bearer access. Uses SHA256 (fast + adequate; tokens have full
    entropy so bcrypt's work-factor protection is wasted).
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ── Store ──


class IdentityStore:
    """Per-project user/session sqlite store."""

    def db_path(self, project_root: Path) -> Path:
        return project_root / ".MEMORY" / ".index" / "aidocs_identity.sqlite3"

    def init_db(self, project_root: Path) -> None:
        path = self.db_path(project_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(path)) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS identity_users (
                    user_id TEXT PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE,
                    role TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    disabled INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS identity_tokens (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    issued_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES identity_users(user_id)
                );
                CREATE INDEX IF NOT EXISTS idx_identity_tokens_user
                    ON identity_tokens(user_id);
            """)
            conn.commit()

    def create_user(
        self,
        project_root: Path,
        email: str,
        password: str,
        role: str,
    ) -> User:
        """Register a new user. Raises ValueError on invalid role / empty
        email / duplicate email.
        """
        email = str(email or "").strip().lower()
        if not email or "@" not in email:
            raise ValueError(f"invalid email: {email!r}")
        role = str(role or "").strip()
        if not role:
            raise ValueError("role is required")
        if role not in VALID_ROLES:
            raise ValueError(f"invalid role: {role!r} (allowed: {sorted(VALID_ROLES)})")
        if not password or len(password) < 8:
            raise ValueError("password must be ≥ 8 characters")
        self.init_db(project_root)
        user_id = "u_" + secrets.token_hex(12)
        now = _iso_now()
        hashed = _hash_password(password)
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            try:
                conn.execute(
                    "INSERT INTO identity_users (user_id, email, role, "
                    "password_hash, created_at, disabled) "
                    "VALUES (?, ?, ?, ?, ?, 0)",
                    (user_id, email, role, hashed, now),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"email already registered: {email}") from exc
        return User(
            user_id=user_id,
            email=email,
            role=role,
            created_at=now,
            disabled=False,
        )

    def get_user_by_email(
        self,
        project_root: Path,
        email: str,
    ) -> User | None:
        self.init_db(project_root)
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT user_id, email, role, created_at, disabled "
                "FROM identity_users WHERE email = ?",
                (str(email).strip().lower(),),
            ).fetchone()
        if row is None:
            return None
        return User(
            user_id=row["user_id"],
            email=row["email"],
            role=row["role"],
            created_at=row["created_at"],
            disabled=bool(row["disabled"]),
        )

    def get_user_by_id(
        self,
        project_root: Path,
        user_id: str,
    ) -> User | None:
        """Resolve a user row by user_id. Returns None when absent.

        Used by host-session binding resolution so a bound
        OperatorContext carries email/role straight from the user
        row instead of re-deriving them piecemeal.
        """
        if not user_id:
            return None
        self.init_db(project_root)
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT user_id, email, role, created_at, disabled "
                "FROM identity_users WHERE user_id = ?",
                (str(user_id).strip(),),
            ).fetchone()
        if row is None:
            return None
        return User(
            user_id=row["user_id"],
            email=row["email"],
            role=row["role"],
            created_at=row["created_at"],
            disabled=bool(row["disabled"]),
        )

    def authenticate(
        self,
        project_root: Path,
        email: str,
        password: str,
    ) -> User | None:
        """Return the User on correct password, None otherwise.

        Constant-time-ish: always hashes a dummy password on
        user-not-found to avoid leaking email existence via
        timing. Disabled users never authenticate regardless of
        password correctness.
        """
        self.init_db(project_root)
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT user_id, email, role, password_hash, created_at, disabled "
                "FROM identity_users WHERE email = ?",
                (str(email).strip().lower(),),
            ).fetchone()
        if row is None:
            # Prevent timing-based email enumeration.
            _verify_password(password, "sha256$deadbeef$0" * 2)
            return None
        if row["disabled"]:
            _verify_password(password, row["password_hash"])
            return None
        if not _verify_password(password, row["password_hash"]):
            return None
        return User(
            user_id=row["user_id"],
            email=row["email"],
            role=row["role"],
            created_at=row["created_at"],
            disabled=False,
        )

    def issue_token(
        self,
        project_root: Path,
        user_id: str,
        ttl_seconds: int = _DEFAULT_TOKEN_TTL_SECONDS,
    ) -> SessionToken:
        """Mint a fresh bearer token for user_id. Caller stores the
        plaintext token (returned) and presents it on subsequent
        requests; only the hash is persisted.
        """
        self.init_db(project_root)
        token = secrets.token_urlsafe(32)
        issued = time.time()
        expires = issued + max(60, int(ttl_seconds))
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            conn.execute(
                "INSERT INTO identity_tokens "
                "(token_hash, user_id, issued_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    _hash_token(token),
                    user_id,
                    _iso(issued),
                    _iso(expires),
                ),
            )
            conn.commit()
        return SessionToken(
            token=token,
            user_id=user_id,
            issued_at=_iso(issued),
            expires_at=_iso(expires),
        )

    def validate_token(
        self,
        project_root: Path,
        token: str,
    ) -> User | None:
        """Resolve a bearer token to its owning user. Returns None on
        unknown / expired / disabled-user tokens.
        """
        if not token:
            return None
        self.init_db(project_root)
        token_hash = _hash_token(token)
        now_iso = _iso_now()
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT u.user_id, u.email, u.role, u.created_at, u.disabled, "
                "t.expires_at "
                "FROM identity_tokens t "
                "JOIN identity_users u ON u.user_id = t.user_id "
                "WHERE t.token_hash = ?",
                (token_hash,),
            ).fetchone()
        if row is None:
            return None
        if str(row["expires_at"]) <= now_iso:
            return None
        if row["disabled"]:
            return None
        return User(
            user_id=row["user_id"],
            email=row["email"],
            role=row["role"],
            created_at=row["created_at"],
            disabled=False,
        )

    def revoke_token(self, project_root: Path, token: str) -> bool:
        """Drop a single token. Returns True iff present. Operators
        wipe stolen tokens with this; full-user logout uses
        revoke_all_tokens().
        """
        self.init_db(project_root)
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            cur = conn.execute(
                "DELETE FROM identity_tokens WHERE token_hash = ?",
                (_hash_token(token),),
            )
            conn.commit()
        return cur.rowcount > 0

    def revoke_all_tokens(self, project_root: Path, user_id: str) -> int:
        """Drop every token for a user. Returns count. Call after
        password change / role change / admin lockout.
        """
        self.init_db(project_root)
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            cur = conn.execute(
                "DELETE FROM identity_tokens WHERE user_id = ?",
                (user_id,),
            )
            conn.commit()
        return cur.rowcount

    def purge_expired_tokens(self, project_root: Path) -> int:
        """Delete every expired token row. Returns count purged.

        Token-lifecycle GC (2026-05-20): without this, every
        ``issue_token`` accretes a row that never leaves the table —
        the dashboard minting a token per admin command grew
        identity_tokens unbounded. Callers (mint path, logout, app
        exit) run this so the table stays bounded by the number of
        currently-live sessions.
        """
        self.init_db(project_root)
        now_iso = _iso_now()
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            cur = conn.execute(
                "DELETE FROM identity_tokens WHERE expires_at <= ?",
                (now_iso,),
            )
            conn.commit()
        return cur.rowcount

    def count_tokens(
        self,
        project_root: Path,
        user_id: str | None = None,
    ) -> int:
        """Count token rows (optionally for one user). Used by tests
        + diagnostics to assert the table doesn't grow unbounded.
        """
        self.init_db(project_root)
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            if user_id:
                row = conn.execute(
                    "SELECT COUNT(*) FROM identity_tokens WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) FROM identity_tokens",
                ).fetchone()
        return int(row[0]) if row else 0

    def set_disabled(
        self,
        project_root: Path,
        user_id: str,
        disabled: bool,
    ) -> bool:
        """Flip the disabled flag. Returns True iff the user exists."""
        self.init_db(project_root)
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            cur = conn.execute(
                "UPDATE identity_users SET disabled = ? WHERE user_id = ?",
                (1 if disabled else 0, user_id),
            )
            conn.commit()
        return cur.rowcount > 0

    def list_users(self, project_root: Path) -> list[User]:
        """Every user row, disabled or not."""
        self.init_db(project_root)
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT user_id, email, role, created_at, disabled "
                "FROM identity_users ORDER BY created_at ASC",
            ).fetchall()
        return [
            User(
                user_id=r["user_id"],
                email=r["email"],
                role=r["role"],
                created_at=r["created_at"],
                disabled=bool(r["disabled"]),
            )
            for r in rows
        ]


# ── helpers ──


def _iso_now() -> str:
    return _iso(time.time())


def _iso(epoch: float) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(
        epoch,
        tz=UTC,
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
