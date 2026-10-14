"""Install-wide managed sealed application-pack version store.

This is the durable replacement for editing AIDOCS_APP_PACK_BUNDLES /
AIDOCS_APP_PACK_SEALED_V1 for every new contract version.

Import is an explicit LOCAL OPERATOR act. The caller must provide the exact
expected SHA-256. Only canonical RFC8785 bundle bytes whose parsed
contract_digest equals that same digest may be stored; DEMO bundles never
enter this authority. Rows are append-only by design.

The generation counter is bumped only for a NEW version. A live gate can poll
that one integer cheaply and load newly committed rows into its existing
in-process PackRegistry without restart.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .._sqlite_connect import Durability
from .._sqlite_connect import connect as _canonical_connect
from .bundle import BundleRefused, load_bundle

__all__ = [
    "ManagedPackSource",
    "ManagedPackStore",
    "ManagedPackVersion",
    "PackImportRefused",
    "PackImportResult",
    "managed_pack_db_path",
]

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_DB_NAME = "app_pack_registry.sqlite3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_pack_meta (
    singleton   INTEGER PRIMARY KEY CHECK (singleton = 1),
    generation  INTEGER NOT NULL CHECK (generation >= 0)
);
INSERT OR IGNORE INTO app_pack_meta (singleton, generation) VALUES (1, 0);

CREATE TABLE IF NOT EXISTS app_pack_versions (
    pack_id          TEXT NOT NULL,
    contract_digest  TEXT NOT NULL,
    bundle_bytes     BLOB NOT NULL,
    imported_at      INTEGER NOT NULL CHECK (imported_at >= 0),
    PRIMARY KEY (pack_id, contract_digest)
);

CREATE TRIGGER IF NOT EXISTS app_pack_versions_no_update
BEFORE UPDATE ON app_pack_versions
BEGIN
    SELECT RAISE(ABORT, 'app_pack_versions is append-only');
END;

CREATE TRIGGER IF NOT EXISTS app_pack_versions_no_delete
BEFORE DELETE ON app_pack_versions
BEGIN
    SELECT RAISE(ABORT, 'app_pack_versions is append-only');
END;

CREATE TRIGGER IF NOT EXISTS app_pack_meta_generation_step
BEFORE UPDATE OF generation ON app_pack_meta
WHEN NEW.generation != OLD.generation + 1
BEGIN
    SELECT RAISE(ABORT, 'app_pack generation must advance exactly one');
END;

CREATE TRIGGER IF NOT EXISTS app_pack_meta_no_delete
BEFORE DELETE ON app_pack_meta
BEGIN
    SELECT RAISE(ABORT, 'app_pack meta is durable authority');
END;
"""


class PackImportRefused(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(code + (f": {detail}" if detail else ""))


@dataclass(frozen=True)
class ManagedPackVersion:
    pack_id: str
    contract_digest: str
    bundle_bytes: bytes
    imported_at: int


@dataclass(frozen=True)
class PackImportResult:
    pack_id: str
    contract_digest: str
    imported: bool
    imported_at: int


def managed_pack_db_path(gate_root: Any) -> Path:
    return Path(gate_root) / _DB_NAME


class ManagedPackSource:
    """Lazy read source used by a running gate during migration.

    A legacy env-only gate has no managed DB yet. Absence therefore means an
    empty managed overlay, not permission to create a new authority file.
    Once the operator import CLI creates the DB, the same source opens it and
    keeps that store object for subsequent generation checks.
    """

    def __init__(self, db_path: Any) -> None:
        self.db_path = Path(db_path)
        self._store: ManagedPackStore | None = None
        self._identity: tuple[int, int] | None = None
        self._lock = threading.RLock()

    def _path_identity(self) -> tuple[int, int] | None:
        try:
            info = self.db_path.stat()
        except FileNotFoundError:
            return None
        except OSError:
            raise PackImportRefused("store_unavailable") from None
        if not stat.S_ISREG(info.st_mode):
            raise PackImportRefused("store_unavailable")
        return (int(info.st_dev), int(info.st_ino))

    def _current(self) -> ManagedPackStore | None:
        with self._lock:
            identity = self._path_identity()
            if self._store is not None:
                if identity is None or identity != self._identity:
                    raise PackImportRefused("store_unavailable")
                return self._store
            if identity is None:
                return None
            self._store = ManagedPackStore(self.db_path)
            # Opening must not race an atomic file replacement.
            after = self._path_identity()
            if after is None or after != identity:
                self._store = None
                raise PackImportRefused("store_unavailable")
            self._identity = after
            return self._store

    def generation(self) -> int:
        store = self._current()
        return 0 if store is None else store.generation()

    def versions(self) -> tuple[ManagedPackVersion, ...]:
        store = self._current()
        return () if store is None else store.versions()


class ManagedPackStore:
    """One install-wide append-only SQLite authority for sealed pack versions."""

    def __init__(self, db_path: Any) -> None:
        self.db_path = Path(db_path)
        if not self.db_path.parent.is_dir():
            raise PackImportRefused("store_unavailable", str(self.db_path.parent))
        self._lock = threading.RLock()
        try:
            # ONE long-lived handle, opened through the canonical connector (#755:
            # WAL, synchronous=FULL, busy_timeout, foreign_keys=ON). It must stay
            # open: ManagedPackSource attests the file's inode once, and every later
            # operation is bound to THAT file through this handle, never to whatever
            # sits at the pathname now (r0b2 257cf1d2). check_same_thread=False is
            # safe because every use is serialised under self._lock.
            self._conn = _canonical_connect(
                self.db_path,
                durability=Durability.AUDIT,
                timeout=5.0,
                busy_timeout_ms=5000,
                row_factory=False,
                check_same_thread=False,
            )
            self._conn.isolation_level = None  # explicit transactions only
            self._conn.executescript(_SCHEMA)
        except sqlite3.Error as exc:
            raise PackImportRefused("store_unavailable", type(exc).__name__) from None

    def generation(self) -> int:
        with self._lock:
            try:
                row = self._conn.execute(
                    "SELECT generation FROM app_pack_meta WHERE singleton = 1"
                ).fetchone()
            except sqlite3.Error:
                raise PackImportRefused("store_unavailable") from None
        if row is None or type(row[0]) is not int or row[0] < 0:
            raise PackImportRefused("store_corrupt")
        return row[0]

    def versions(self) -> tuple[ManagedPackVersion, ...]:
        with self._lock:
            try:
                rows = self._conn.execute(
                    """
                    SELECT pack_id, contract_digest, bundle_bytes, imported_at
                    FROM app_pack_versions
                    ORDER BY pack_id, contract_digest
                    """
                ).fetchall()
            except sqlite3.Error:
                raise PackImportRefused("store_unavailable") from None
        out: list[ManagedPackVersion] = []
        for pack_id, digest, bundle_bytes, imported_at in rows:
            if (
                type(pack_id) is not str
                or not pack_id
                or type(digest) is not str
                or not _HEX64.fullmatch(digest)
                or type(bundle_bytes) is not bytes
                or type(imported_at) is not int
                or imported_at < 0
            ):
                raise PackImportRefused("store_corrupt")
            out.append(ManagedPackVersion(pack_id, digest, bundle_bytes, imported_at))
        return tuple(out)

    def import_bundle(
        self,
        data: bytes,
        *,
        expected_sha256: str,
        now: int,
    ) -> PackImportResult:
        if type(data) is not bytes:
            raise PackImportRefused("bundle_invalid")
        if type(expected_sha256) is not str or not _HEX64.fullmatch(expected_sha256):
            raise PackImportRefused("import_digest_invalid")
        if type(now) is not int or now < 0:
            raise PackImportRefused("import_time_invalid")

        actual = hashlib.sha256(data).hexdigest()
        if not hmac_compare(actual, expected_sha256):
            raise PackImportRefused("import_digest_mismatch")

        try:
            pack = load_bundle(data)
        except BundleRefused as exc:
            raise PackImportRefused(exc.code) from None
        except Exception:
            raise PackImportRefused("bundle_invalid") from None

        if pack.is_demo:
            raise PackImportRefused("demo_not_sealed_v1")
        # The old gate.env path required raw-file SHA == the operator pin while
        # load_bundle derives contract_digest from canonical bytes. Requiring
        # BOTH here preserves exactly that law and therefore requires the
        # imported file itself to already be canonical.
        if pack.canonical_bytes != data:
            raise PackImportRefused("bundle_not_canonical")
        if pack.contract_digest != expected_sha256:
            raise PackImportRefused("import_digest_mismatch")

        with self._lock:
            conn = self._conn
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    """
                    SELECT bundle_bytes, imported_at
                    FROM app_pack_versions
                    WHERE pack_id = ? AND contract_digest = ?
                    """,
                    (pack.pack_id, pack.contract_digest),
                ).fetchone()
                if row is not None:
                    existing, imported_at = row
                    if type(existing) is not bytes or existing != data or type(imported_at) is not int:
                        conn.execute("ROLLBACK")
                        raise PackImportRefused("store_conflict")
                    conn.execute("COMMIT")
                    return PackImportResult(
                        pack.pack_id,
                        pack.contract_digest,
                        False,
                        imported_at,
                    )
                conn.execute(
                    """
                    INSERT INTO app_pack_versions
                        (pack_id, contract_digest, bundle_bytes, imported_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (pack.pack_id, pack.contract_digest, data, now),
                )
                conn.execute(
                    "UPDATE app_pack_meta SET generation = generation + 1 WHERE singleton = 1"
                )
                conn.execute("COMMIT")
            except PackImportRefused:
                raise
            except sqlite3.Error:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise PackImportRefused("store_unavailable") from None

        return PackImportResult(pack.pack_id, pack.contract_digest, True, now)


def hmac_compare(left: str, right: str) -> bool:
    """Constant-time digest spelling comparison without importing auth policy."""
    import hmac

    return hmac.compare_digest(left.encode("ascii"), right.encode("ascii"))
