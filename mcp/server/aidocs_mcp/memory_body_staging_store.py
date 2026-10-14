"""Memory body staging ledger — #375 Phase 3 (A), the write-path flip.

EMPEROR RULING (2026-07-19): memories live in the PALACE; sqlite is
config/state/security + the code index; every memory is anchored to the
code index at the smallest leaf. This module is the durability half of
that flip:

  capture stages the body durably FIRST (a sqlite ledger row in the same
  aidocs.sqlite3 the pending_durable_writes pattern uses), then a
  BACKGROUND projector lands it in the project palace as the canonical
  drawer and writes a durability RECEIPT (sync_event_receipts-style row
  keyed to the drawer id). The staged row retires ONLY on a receipted,
  read-back-verified landing (verify-then-retire).

Settled constraints (constitution, non-negotiable):
  * capture NEVER stalls on chroma — nothing in this module imports
    chromadb/mempalace on the staging path (the 2026-06-30 wound stays
    healed); the projector runs on the existing background palace worker
    (AQ law: heavy work parks behind first-call completion).
  * no memory is ever lost to a dead chroma — a failed/unverified
    projection leaves the staged row INTACT and retriable; the canonical
    sqlite body remains readable the whole time.
  * receipt-before-retire — the staged row transitions to 'landed' only
    after the receipt row exists for a verified read-back.

State machine (per memory_index path):
  staged ──(ingest ok + read-back == checksum)──▶ receipt row written
         ──(receipt exists)──▶ body_home='palace' ──▶ staged row 'landed'
  staged ──(ingest/verify failure)──▶ staged (attempts++, last_error)
  staged ──(row retired / content drifted / config-denied)──▶ 'superseded'

Tombstone model: ledger rows transition status, never DELETE.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

# #756: the ONE canonical connect. All nine sites were
# `with sqlite3.connect(...) as conn:` -- sqlite3's TRANSACTION context
# manager, which commits/rolls back and NEVER closes the handle -- and none
# set a single pragma. DURABILITY: RUNTIME (the default) throughout. Every
# row here is a RETRIABLE STAGING/RECEIPT ledger over data whose canonical
# copy already lives durably in memory_index (sqlite, a separate store) --
# losing a staged-intent or receipt row to a power cut costs a re-stage /
# re-verify on the next pass, never the memory itself (staging-survives-
# dead-chroma is enforced by the state machine, not by fsync). None of these
# rows are evidence-of-record the way an audit/authority grant is, so none
# ask for Durability.AUDIT.
from ._sqlite_connect import connect as _canonical_connect

# Ledger stamp for the receipt id — receipt is keyed to the DRAWER id +
# the exact content checksum it verified, so a re-capture (new checksum)
# requires a NEW receipt before its staged row may retire.
_RECEIPT_STREAM = "memory_body_projection"


def _db_path(project_root: Path) -> Path:
    return Path(project_root) / ".MEMORY" / ".index" / "aidocs.sqlite3"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def body_checksum(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def init_db(project_root: Path) -> None:
    db = _db_path(project_root)
    db.parent.mkdir(parents=True, exist_ok=True)
    with _canonical_connect(str(db), row_factory=False) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS pending_palace_projections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL,
                drawer_id TEXT NOT NULL,
                content_checksum TEXT NOT NULL,
                staged_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                status TEXT NOT NULL DEFAULT 'staged'
                    CHECK (status IN ('staged','landed','superseded')),
                landed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_ppp_path_status
                ON pending_palace_projections(path, status);
            CREATE TABLE IF NOT EXISTS palace_projection_receipts (
                receipt_id TEXT PRIMARY KEY,
                stream TEXT NOT NULL,
                drawer_id TEXT NOT NULL,
                path TEXT NOT NULL,
                content_checksum TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ppr_drawer
                ON palace_projection_receipts(drawer_id);
            """
        )
        conn.commit()


def stage_projection(project_root: Path, *, path: str) -> dict[str, Any]:
    """Durably stage the CURRENT canonical body of ``path`` for palace
    projection. Pure sqlite — no chroma/mempalace import, safe on the
    capture hot path. Idempotent: an existing staged row for the same
    (path, checksum) is reused; a staged row with a STALE checksum is
    superseded and replaced.

    Returns {"staged": bool, "row_id": int|None, "checksum": str,
    "reason": str}. A missing/inactive canonical row stages nothing.
    """
    from .memory_sqlite_store import memory_drawer_id, read_entry

    entry = read_entry(project_root, path)
    if entry is None:
        return {"staged": False, "row_id": None, "checksum": "", "reason": "no_active_row"}
    checksum = body_checksum(entry.content)
    init_db(project_root)
    with _canonical_connect(str(_db_path(project_root))) as conn:
        # row_factory defaults True in the canonical helper -- matches the
        # sqlite3.Row this site set by hand (existing["id"] / ["content_checksum"]).
        existing = conn.execute(
            "SELECT id, content_checksum FROM pending_palace_projections "
            "WHERE path = ? AND status = 'staged' ORDER BY id DESC LIMIT 1",
            (path,),
        ).fetchone()
        if existing is not None and str(existing["content_checksum"]) == checksum:
            return {
                "staged": True,
                "row_id": int(existing["id"]),
                "checksum": checksum,
                "reason": "already_staged",
            }
        if existing is not None:
            # Content drifted since the last stage — the old intent can
            # never verify; supersede it (tombstone) and stage fresh.
            conn.execute(
                "UPDATE pending_palace_projections SET status = 'superseded', "
                "last_error = 'content_drift' WHERE id = ?",
                (int(existing["id"]),),
            )
        cur = conn.execute(
            "INSERT INTO pending_palace_projections "
            "(path, drawer_id, content_checksum, staged_at) VALUES (?, ?, ?, ?)",
            (path, memory_drawer_id(path), checksum, _now()),
        )
        conn.commit()
        return {
            "staged": True,
            "row_id": int(cur.lastrowid),
            "checksum": checksum,
            "reason": "staged",
        }


def get_staged(project_root: Path, path: str) -> dict[str, Any] | None:
    init_db(project_root)
    db = _db_path(project_root)
    with _canonical_connect(str(db)) as conn:
        # row_factory defaults True -- required by dict(row) below.
        row = conn.execute(
            "SELECT * FROM pending_palace_projections "
            "WHERE path = ? AND status = 'staged' ORDER BY id DESC LIMIT 1",
            (path,),
        ).fetchone()
        return dict(row) if row is not None else None


def list_staged(project_root: Path, *, limit: int = 500) -> list[dict[str, Any]]:
    init_db(project_root)
    db = _db_path(project_root)
    with _canonical_connect(str(db)) as conn:
        # row_factory defaults True -- required by dict(r) below.
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM pending_palace_projections "
                "WHERE status = 'staged' ORDER BY id LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        ]


def _receipt_id(drawer_id: str, checksum: str) -> str:
    return f"{drawer_id}:{checksum}"


def record_landing_receipt(
    project_root: Path,
    *,
    path: str,
    drawer_id: str,
    content_checksum: str,
) -> str:
    """Write the durability receipt for a VERIFIED landing. Idempotent
    (INSERT OR IGNORE — same discipline as sync_event_receipts). The
    caller must only invoke this after the drawer read-back matched the
    staged checksum; the receipt is the authority for retirement."""
    init_db(project_root)
    rid = _receipt_id(drawer_id, content_checksum)
    with _canonical_connect(str(_db_path(project_root)), row_factory=False) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO palace_projection_receipts "
            "(receipt_id, stream, drawer_id, path, content_checksum, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (rid, _RECEIPT_STREAM, drawer_id, path, content_checksum, _now()),
        )
        conn.commit()
    return rid


def has_receipt(project_root: Path, *, drawer_id: str, content_checksum: str) -> bool:
    db = _db_path(project_root)
    if not db.is_file():
        return False
    try:
        with _canonical_connect(str(db), row_factory=False) as conn:
            row = conn.execute(
                "SELECT 1 FROM palace_projection_receipts WHERE receipt_id = ?",
                (_receipt_id(drawer_id, content_checksum),),
            ).fetchone()
            return row is not None
    except sqlite3.Error:
        return False


def _mark_attempt(project_root: Path, row_id: int, error: str) -> None:
    with _canonical_connect(str(_db_path(project_root)), row_factory=False) as conn:
        conn.execute(
            "UPDATE pending_palace_projections "
            "SET attempts = attempts + 1, last_error = ? WHERE id = ?",
            (error[:200], int(row_id)),
        )
        conn.commit()


def _mark_superseded(project_root: Path, row_id: int, reason: str) -> None:
    with _canonical_connect(str(_db_path(project_root)), row_factory=False) as conn:
        conn.execute(
            "UPDATE pending_palace_projections "
            "SET status = 'superseded', last_error = ? WHERE id = ?",
            (reason[:200], int(row_id)),
        )
        conn.commit()


def _retire_staged(project_root: Path, row_id: int) -> None:
    with _canonical_connect(str(_db_path(project_root)), row_factory=False) as conn:
        conn.execute(
            "UPDATE pending_palace_projections "
            "SET status = 'landed', landed_at = ? WHERE id = ?",
            (_now(), int(row_id)),
        )
        conn.commit()


def _verify_drawer_readback(
    project_root: Path,
    *,
    path: str,
    content: str,
    drawer_reader: Callable[..., str | None],
) -> bool:
    """Read the projected drawer(s) back and compare byte-for-byte with
    the staged canonical body. Chunked bodies verify every child drawer
    against the deterministic re-chunking (same splitter the ingest used).
    """
    from .memory_sqlite_store import _chunk_memory_content, memory_drawer_id

    parent = memory_drawer_id(path)
    chunks = _chunk_memory_content(content)
    if len(chunks) == 1:
        got = drawer_reader(drawer_id=parent)
        return got == content
    for i, chunk in enumerate(chunks):
        got = drawer_reader(drawer_id=f"{parent}#chunk{i:04d}")
        if got != chunk:
            return False
    return True


def project_staged_entry(
    project_root: Path,
    palace_service,
    path: str,
    *,
    hub_ctx=None,
    drawer_reader: Callable[..., str | None] | None = None,
) -> dict[str, Any]:
    """The projector leg — runs on the BACKGROUND palace worker, never on
    the capture path (this is the only function here that reaches
    chroma, and only via the mempalace public seam).

    verify-then-retire: ingest → read the drawer back → checksum match →
    receipt → body_home='palace' → staged row retires ('landed'). Any
    failure leaves the staged row intact (attempts++/last_error) so a
    dead or cold chroma can never lose a memory.

    ``drawer_reader`` is injectable for tests; production defaults to
    PalaceDrawerReader over the mempalace public get_collection seam.
    """
    from .memory_sqlite_store import (
        PalaceDrawerReader,
        _config_memory_ingest_allowed,
        memory_drawer_id,
        palace_ingest_entry,
        read_entry,
        set_body_home,
    )

    staged = get_staged(project_root, path)
    if staged is None:
        result = stage_projection(project_root, path=path)
        if not result["staged"]:
            return {"landed": False, "reason": result["reason"]}
        staged = get_staged(project_root, path)
        if staged is None:  # pragma: no cover — defensive
            return {"landed": False, "reason": "stage_race"}

    entry = read_entry(project_root, path)
    if entry is None:
        _mark_superseded(project_root, staged["id"], "canonical_row_inactive")
        return {"landed": False, "reason": "canonical_row_inactive"}

    checksum = body_checksum(entry.content)
    if checksum != str(staged["content_checksum"]):
        # Body drifted after staging — restage the current truth; the
        # projector will land it on the next drain pass.
        _mark_superseded(project_root, staged["id"], "content_drift")
        stage_projection(project_root, path=path)
        return {"landed": False, "reason": "content_drift_restaged"}

    if not _config_memory_ingest_allowed(path):
        # Config-policy veto: this memory is INTENTIONALLY never a palace
        # drawer. sqlite stays its body home; the intent row closes.
        _mark_superseded(project_root, staged["id"], "config_policy_denied")
        return {"landed": False, "reason": "config_policy_denied"}

    ok = palace_ingest_entry(project_root, palace_service, path, hub_ctx=hub_ctx)
    if not ok:
        _mark_attempt(project_root, staged["id"], "ingest_failed")
        return {"landed": False, "reason": "ingest_failed", "retriable": True}

    reader = drawer_reader
    if reader is None:
        reader = PalaceDrawerReader(project_root).get_drawer_content
    try:
        verified = _verify_drawer_readback(
            project_root, path=path, content=entry.content, drawer_reader=reader
        )
    except Exception:
        verified = False
    if not verified:
        # Ingest claimed success but the read-back does not prove the
        # landing (dead/cold chroma, partial write). NO receipt, NO
        # retirement — the staged row stays retriable.
        _mark_attempt(project_root, staged["id"], "verify_failed")
        return {"landed": False, "reason": "verify_failed", "retriable": True}

    drawer_id = memory_drawer_id(path)
    receipt_id = record_landing_receipt(
        project_root, path=path, drawer_id=drawer_id, content_checksum=checksum
    )
    # Receipt exists → the palace copy is proven durable → the body home
    # flips and the staged row retires. Strictly in this order.
    set_body_home(project_root, path, "palace")
    _retire_staged(project_root, staged["id"])
    return {
        "landed": True,
        "verified": True,
        "receipt_id": receipt_id,
        "drawer_id": drawer_id,
        "checksum": checksum,
    }


def drain_staged(
    project_root: Path,
    palace_service,
    *,
    hub_ctx=None,
    drawer_reader: Callable[..., str | None] | None = None,
    limit: int = 200,
) -> dict[str, int]:
    """Retry every staged projection (background/maintenance surface).
    Bounded, best-effort per row; failures stay staged and retriable."""
    stats = {"scanned": 0, "landed": 0, "failed": 0}
    for row in list_staged(project_root, limit=limit):
        stats["scanned"] += 1
        try:
            out = project_staged_entry(
                project_root,
                palace_service,
                str(row["path"]),
                hub_ctx=hub_ctx,
                drawer_reader=drawer_reader,
            )
        except Exception:
            out = {"landed": False}
        if out.get("landed"):
            stats["landed"] += 1
        else:
            stats["failed"] += 1
    return stats
