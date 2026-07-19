"""#375 Phase 3 (C) — one-shot stamped body-home migrator (groundwork).

Projects EXISTING memory_index bodies into palace drawers with per-row
verify-then-mark: a row's ``body_home`` flips to 'palace' ONLY on a
receipted read-back landing (the War AR receipt discipline, executed by
``memory_body_staging_store.project_staged_entry``). Rows whose landing
cannot be verified stay 'staged' — retriable, never lost.

DELIBERATE-RUN CONTRACT: this migrator is built and fixture-tested but is
NEVER invoked implicitly by the runtime. The conductor runs it against the
real .MEMORY post-verification. Idempotent — a re-run skips rows already
palace-home with a receipt, retries the remainder, and refuses nothing.

Stamp discipline: every run writes a row into ``memory_home_migrations``
(same aidocs.sqlite3) recording stamp/stats/completion, so the flip is a
dated, auditable act rather than an ambient behavior change.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

MIGRATION_STAMP = "phase3-body-home-flip-v1"


def _db_path(project_root: Path) -> Path:
    return Path(project_root) / ".MEMORY" / ".index" / "aidocs.sqlite3"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ensure_stamp_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_home_migrations (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            stamp TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            scanned INTEGER NOT NULL DEFAULT 0,
            landed INTEGER NOT NULL DEFAULT 0,
            skipped INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0,
            completed INTEGER NOT NULL DEFAULT 0,
            dry_run INTEGER NOT NULL DEFAULT 0
        )
        """
    )


def last_completed_run(project_root: Path, *, stamp: str = MIGRATION_STAMP) -> dict | None:
    """The most recent COMPLETED (all rows home or intentionally sqlite-home)
    run for ``stamp``; None when the migration has never fully completed."""
    db = _db_path(project_root)
    if not db.is_file():
        return None
    try:
        with sqlite3.connect(str(db)) as conn:
            conn.row_factory = sqlite3.Row
            _ensure_stamp_table(conn)
            row = conn.execute(
                "SELECT * FROM memory_home_migrations "
                "WHERE stamp = ? AND completed = 1 AND dry_run = 0 "
                "ORDER BY run_id DESC LIMIT 1",
                (stamp,),
            ).fetchone()
            return dict(row) if row is not None else None
    except sqlite3.Error:
        return None


def migrate_bodies_to_palace(
    project_root: Path,
    palace_service,
    *,
    hub_ctx=None,
    drawer_reader: Callable[..., str | None] | None = None,
    dry_run: bool = False,
    limit: int = 0,
    stamp: str = MIGRATION_STAMP,
) -> dict[str, Any]:
    """Project every active memory_index body into its palace drawer with
    per-row verify-then-mark. Returns
    ``{scanned, landed, already_home, skipped_config, would_project,
    failed, completed, dry_run, run_id}``.

    Per row:
      * body_home already 'palace' AND the landing receipt exists → skip
        (idempotence — re-runs are free);
      * config-policy-denied paths → skipped_config (sqlite stays their
        body home BY DESIGN, they never become drawers);
      * otherwise stage (durable intent) + project_staged_entry — the
        body_home flip happens INSIDE the projector, only after the
        receipted read-back (verify-then-mark). A dead chroma leaves the
        row staged and counted ``failed`` (retriable), never marked.

    ``dry_run=True`` counts what would happen without writing anything
    (no staging, no ingest, no stamp mutation beyond the dry-run row).
    """
    from .memory_body_staging_store import (
        body_checksum,
        has_receipt,
        project_staged_entry,
        stage_projection,
    )
    from .memory_sqlite_store import (
        _config_memory_ingest_allowed,
        list_entries,
        memory_drawer_id,
        read_entry,
    )

    stats: dict[str, Any] = {
        "scanned": 0,
        "landed": 0,
        "already_home": 0,
        "skipped_config": 0,
        "would_project": 0,
        "failed": 0,
        "completed": False,
        "dry_run": bool(dry_run),
        "run_id": None,
    }

    db = _db_path(project_root)
    if not db.is_file():
        return stats

    with sqlite3.connect(str(db)) as conn:
        _ensure_stamp_table(conn)
        cur = conn.execute(
            "INSERT INTO memory_home_migrations (stamp, started_at, dry_run) "
            "VALUES (?, ?, ?)",
            (stamp, _now(), 1 if dry_run else 0),
        )
        conn.commit()
        run_id = int(cur.lastrowid)
    stats["run_id"] = run_id

    entries = list_entries(project_root)
    if limit:
        entries = entries[: max(1, int(limit))]

    for listed in entries:
        stats["scanned"] += 1
        path = listed.path
        # list_entries does not project body_home — re-read the full row.
        entry = read_entry(project_root, path)
        if entry is None:
            continue
        if not _config_memory_ingest_allowed(path):
            # Intentionally sqlite-home forever (config policy) — counted,
            # never blocks completion.
            stats["skipped_config"] += 1
            continue
        checksum = body_checksum(entry.content)
        if entry.body_home == "palace" and has_receipt(
            project_root,
            drawer_id=memory_drawer_id(path),
            content_checksum=checksum,
        ):
            stats["already_home"] += 1
            continue
        if dry_run:
            stats["would_project"] += 1
            continue
        try:
            stage_projection(project_root, path=path)
            out = project_staged_entry(
                project_root,
                palace_service,
                path,
                hub_ctx=hub_ctx,
                drawer_reader=drawer_reader,
            )
        except Exception:
            out = {"landed": False}
        if out.get("landed"):
            stats["landed"] += 1
        else:
            stats["failed"] += 1

    stats["completed"] = stats["failed"] == 0 and stats["would_project"] == 0
    with sqlite3.connect(str(db)) as conn:
        _ensure_stamp_table(conn)
        conn.execute(
            "UPDATE memory_home_migrations SET finished_at = ?, scanned = ?, "
            "landed = ?, skipped = ?, failed = ?, completed = ? WHERE run_id = ?",
            (
                _now(),
                stats["scanned"],
                stats["landed"],
                stats["already_home"] + stats["skipped_config"],
                stats["failed"],
                1 if stats["completed"] else 0,
                run_id,
            ),
        )
        conn.commit()
    return stats
