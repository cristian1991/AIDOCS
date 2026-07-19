"""#475 (3) — one-shot stamped SESSION.md compaction sweep.

Collapses blank-line padding runs in existing
``.MEMORY/sessions/*/SESSION.md`` files down to single-blank-line
separation. The padding writer itself is fixed in
``SessionStore._compact_section_values``; this sweep repairs files the
old writer already bloated (War AZ read a 10k-blank-line file).

DELIBERATE-RUN CONTRACT (mirrors ``memory_home_migrator``): built and
fixture-tested but NEVER invoked implicitly by the runtime. The
conductor runs it against the real .MEMORY. Idempotent — a re-run
finds nothing to compact and rewrites nothing.

Stamp discipline: every run writes a row into
``session_md_compactions`` (same aidocs.sqlite3) recording
stamp/stats/completion, so the sweep is a dated, auditable act rather
than an ambient behavior change.

Read-side tolerance is unchanged: ``SessionStore._parse_sections``
still accepts padded files; this sweep only removes redundant blank
lines and never drops a content line.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

COMPACTION_STAMP = "475-session-md-blank-compaction-v1"


def _db_path(project_root: Path) -> Path:
    return Path(project_root) / ".MEMORY" / ".index" / "aidocs.sqlite3"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ensure_stamp_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS session_md_compactions (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            stamp TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            scanned INTEGER NOT NULL DEFAULT 0,
            compacted INTEGER NOT NULL DEFAULT 0,
            lines_removed INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0,
            completed INTEGER NOT NULL DEFAULT 0,
            dry_run INTEGER NOT NULL DEFAULT 0
        )
        """,
    )


def last_completed_run(project_root: Path, *, stamp: str = COMPACTION_STAMP) -> dict | None:
    """Most recent COMPLETED non-dry run for ``stamp``; None if never."""
    db = _db_path(project_root)
    if not db.is_file():
        return None
    try:
        with sqlite3.connect(str(db)) as conn:
            conn.row_factory = sqlite3.Row
            _ensure_stamp_table(conn)
            row = conn.execute(
                "SELECT * FROM session_md_compactions "
                "WHERE stamp = ? AND completed = 1 AND dry_run = 0 "
                "ORDER BY run_id DESC LIMIT 1",
                (stamp,),
            ).fetchone()
            return dict(row) if row is not None else None
    except sqlite3.Error:
        return None


def compact_markdown_text(text: str) -> str:
    """Collapse every run of 2+ blank lines to a single blank line.

    Content lines pass through byte-identical; leading blank lines
    before the first content line are dropped; the file keeps a single
    trailing newline.
    """
    out: list[str] = []
    pending_blank = False
    for line in text.splitlines():
        if not line.strip():
            pending_blank = bool(out)
            continue
        if pending_blank:
            out.append("")
            pending_blank = False
        out.append(line)
    return "\n".join(out) + "\n"


def compact_session_markdown(
    project_root: Path,
    *,
    dry_run: bool = False,
    stamp: str = COMPACTION_STAMP,
) -> dict[str, Any]:
    """Sweep ``.MEMORY/sessions/*/SESSION.md`` collapsing blank padding.

    Returns ``{scanned, compacted, unchanged, would_compact,
    lines_removed, failed, completed, dry_run, run_id}``. A file is
    rewritten only when compaction actually changes it (idempotence —
    re-runs are free).
    """
    stats: dict[str, Any] = {
        "scanned": 0,
        "compacted": 0,
        "unchanged": 0,
        "would_compact": 0,
        "lines_removed": 0,
        "failed": 0,
        "completed": False,
        "dry_run": bool(dry_run),
        "run_id": None,
    }

    db = _db_path(project_root)
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db)) as conn:
        _ensure_stamp_table(conn)
        cur = conn.execute(
            "INSERT INTO session_md_compactions (stamp, started_at, dry_run) VALUES (?, ?, ?)",
            (stamp, _now(), 1 if dry_run else 0),
        )
        conn.commit()
        run_id = int(cur.lastrowid)
    stats["run_id"] = run_id

    sessions_root = Path(project_root) / ".MEMORY" / "sessions"
    if sessions_root.is_dir():
        for session_dir in sorted(sessions_root.iterdir()):
            session_md = session_dir / "SESSION.md"
            if not session_dir.is_dir() or not session_md.is_file():
                continue
            stats["scanned"] += 1
            try:
                original = session_md.read_text(encoding="utf-8")
                compacted = compact_markdown_text(original)
                if compacted == original:
                    stats["unchanged"] += 1
                    continue
                removed = len(original.splitlines()) - len(compacted.splitlines())
                if dry_run:
                    stats["would_compact"] += 1
                    continue
                session_md.write_text(compacted, encoding="utf-8")
                stats["compacted"] += 1
                stats["lines_removed"] += max(0, removed)
            except OSError:
                stats["failed"] += 1

    stats["completed"] = stats["failed"] == 0 and stats["would_compact"] == 0
    with sqlite3.connect(str(db)) as conn:
        _ensure_stamp_table(conn)
        conn.execute(
            "UPDATE session_md_compactions SET finished_at = ?, scanned = ?, "
            "compacted = ?, lines_removed = ?, failed = ?, completed = ? "
            "WHERE run_id = ?",
            (
                _now(),
                stats["scanned"],
                stats["compacted"],
                stats["lines_removed"],
                stats["failed"],
                1 if stats["completed"] else 0,
                run_id,
            ),
        )
        conn.commit()
    return stats
