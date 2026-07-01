"""RFC-4 Phase 2 — anchor write + read surface.

This is the AIDOCS-side ``hub.memory.register_anchor`` /
``list_anchors_for_unit`` / ``list_anchors_for_drawer`` API
implementation. Sits on top of the extended ``memory_symbol_anchors``
table from anchor_schema_migration.

Adopt by copying to ``aidocs_mcp/anchor_store.py``. Wire it via
``palace_hub_extension.register_palace_in_hub`` so PalaceService
can call ``hub.memory.register_anchor(...)`` etc.

Implementation notes:

- Anchors REQUIRE a route_id, because the existing
  edit_memory_gate.check_edit query joins through memory_routes.
  Palace anchors get their own route via ``_ensure_palace_route``.
- The route is created lazily on first anchor for a given source_file.
- Anchor identity is enforced by the UNIQUE constraint
  (route_id, symbol_name, file_path) on the existing table.
- Confidence validation happens at the application layer
  (anchor_schema_migration.validate_confidence).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path

from .anchor_schema_migration import (
    migrate_memory_symbol_anchors,
    validate_confidence,
    validate_source,
)


@dataclass(frozen=True)
class Anchor:
    """One row from memory_symbol_anchors, post-RFC-4 extension."""

    anchor_id: int
    route_id: int
    symbol_name: str
    file_path: str
    anchor_kind: str  # symbol | file | module | domain
    confidence: str  # exact_symbol | operator_pinned | file_anchor | semantic_guess
    drawer_id: str | None
    content_hash: str
    source: str
    created_at: str


class AnchorStore:
    """RFC-4 §3.5 anchor write + read surface.

    Lives on hub.memory.anchors (or similar). Wired by
    palace_hub_extension.register_palace_in_hub.
    """

    def __init__(
        self,
        *,
        db_path: Path | None = None,
        db_path_resolver: Callable[[], Path] | None = None,
    ):
        """Bind either a fixed db_path (legacy) or a resolver callable
        that returns the path lazily per call. The resolver lets one
        AnchorStore instance correctly serve multiple project roots in
        the same process (tests, project-switching workflows) — the
        same pattern PalaceService uses for palace_path_resolver.
        """
        if db_path is None and db_path_resolver is None:
            raise ValueError("AnchorStore requires either db_path or db_path_resolver")
        self._fixed_db_path: Path | None = Path(db_path) if db_path is not None else None
        self._db_path_resolver = db_path_resolver
        # Schema is migrated once per resolved db_path. With the
        # lazy-resolver pattern, the underlying path can change between
        # calls (tests, project switching), and each new path needs the
        # palace-anchor columns added. The set is keyed by absolute
        # path string so equivalent paths dedupe.
        self._migrated_paths: set[str] = set()

    @property
    def db_path(self) -> Path:
        """Current DB path — resolved fresh per access when a resolver
        is in use. Stays stable for legacy fixed-path consumers.
        """
        if self._db_path_resolver is not None:
            return Path(self._db_path_resolver())
        assert self._fixed_db_path is not None
        return self._fixed_db_path

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    def ensure_schema(self) -> dict:
        """Apply RFC-4 Phase 2.0 migration. Idempotent. Returns a
        report dict suitable for boot-time logging.
        """
        path = self.db_path
        with sqlite3.connect(str(path)) as conn:
            report = migrate_memory_symbol_anchors(conn)
        self._migrated_paths.add(str(path.resolve()))
        return report

    def _ensure_schema_for_current_path(self) -> None:
        """Apply migration lazily when the resolver yields a path we
        haven't migrated yet. Cheap fast-path when path is unchanged.
        """
        try:
            key = str(self.db_path.resolve())
        except (OSError, FileNotFoundError):
            key = str(self.db_path)
        if key in self._migrated_paths:
            return
        self.ensure_schema()

    # ------------------------------------------------------------------
    # Route management (internal — anchors must reference a route)
    # ------------------------------------------------------------------

    def _ensure_palace_route(
        self,
        conn: sqlite3.Connection,
        *,
        source_file: str,
        drawer_id: str,
    ) -> int:
        """Create or retrieve a memory_route row for a palace drawer.

        Palace anchors get one route per drawer. The route's
        ``target_path`` is encoded as ``drawer://<drawer_id>:<source_file>``
        — a stable URL-shape that lets us look up the existing route by
        exact-match without conflicting with non-palace routes that
        carry raw paths in target_path.

        The route is a thin compatibility shell so the existing
        ``memory_symbol_anchors.route_id FOREIGN KEY`` is satisfied.
        Edit-time gate consults of palace anchors should join via
        ``drawer_id`` (which we now index) — not via the route — but
        the route stays in place because legacy callers still join
        through it for non-palace anchors.

        If memory_routes is shaped differently in a given AIDOCS
        deployment (column names vary across versions), this method
        is the single point that needs adjustment.
        """
        target = f"drawer://{drawer_id}:{source_file}" if source_file else f"drawer://{drawer_id}"

        # Exact-match lookup — no LIKE, no ambiguity.
        existing = conn.execute(
            "SELECT route_id FROM memory_routes WHERE target_path = ? LIMIT 1",
            (target,),
        ).fetchone()
        if existing:
            return int(existing[0])

        # Insert a new route. Schema-tolerant: pull current columns
        # via PRAGMA and populate only the columns that exist.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(memory_routes)").fetchall()}
        col_names = ["target_path"]
        col_values: list = [target]

        if "severity" in cols:
            col_names.append("severity")
            # 'sovereign' makes the legacy AIDOCS memory_route query
            # (edit_memory_gate.query_anchored_memories) skip this row
            # — query filters WHERE mr.severity != 'sovereign'. That is
            # the correct behavior here: palace anchors compose into
            # the gate via the SEPARATE palace consult path
            # (check_edit_with_palace queries memory_symbol_anchors
            # directly, no memory_routes JOIN). Routing palace anchors
            # through the legacy memory_route query would double-count
            # them as doctrine blockers when they belong to the
            # palace-tier rail.
            col_values.append("sovereign")
        if "kind" in cols:
            col_names.append("kind")
            col_values.append("palace_anchor")
        # memory_routes.source is NOT NULL with a CHECK constraint
        # restricting it to toml-import|frontmatter-import|capture|manual.
        # Palace anchor routes are 'capture' — they originate from
        # palace-extractor capture of drawer content.
        if "source" in cols:
            col_names.append("source")
            col_values.append("capture")
        if "created_at" in cols:
            col_names.append("created_at")
            col_values.append(_now())

        placeholders = ", ".join("?" * len(col_names))
        col_list = ", ".join(col_names)
        cur = conn.execute(
            f"INSERT INTO memory_routes ({col_list}) VALUES ({placeholders})",
            col_values,
        )
        return int(cur.lastrowid or 0)

    # ------------------------------------------------------------------
    # Public write API — RFC-4 register_anchor
    # ------------------------------------------------------------------

    def register_anchor(
        self,
        *,
        symbol_name: str,
        file_path: str,
        drawer_id: str,
        content_hash: str,
        confidence: str,
        anchor_kind: str = "symbol",
        source: str = "palace_extractor",
    ) -> int:
        """Register a palace anchor.

        Per RFC-4 §3.5:
            confidence ∈ {exact_symbol, operator_pinned, file_anchor, semantic_guess}

        Returns the anchor_id. Idempotent on (route_id, symbol_name,
        file_path) — re-registering the same anchor just updates
        content_hash / confidence on the existing row.

        Raises ValueError on invalid confidence value.
        """
        validate_confidence(confidence)
        validate_source(source)
        if anchor_kind not in {"symbol", "file", "module", "domain"}:
            raise ValueError(
                f"invalid anchor_kind {anchor_kind!r}; must be one of symbol|file|module|domain",
            )
        if not drawer_id:
            raise ValueError("drawer_id is required for palace anchors")
        if not symbol_name and not file_path:
            raise ValueError("at least one of symbol_name or file_path is required")

        self._ensure_schema_for_current_path()
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            route_id = self._ensure_palace_route(
                conn,
                source_file=file_path or "",
                drawer_id=drawer_id,
            )

            # Try INSERT; on UNIQUE conflict, UPDATE the existing row.
            try:
                cur = conn.execute(
                    """INSERT INTO memory_symbol_anchors (
                           route_id, symbol_name, file_path, anchor_kind,
                           drawer_id, content_hash, confidence, source,
                           created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        route_id,
                        symbol_name,
                        file_path or "",
                        anchor_kind,
                        drawer_id,
                        content_hash,
                        confidence,
                        source,
                        _now(),
                    ),
                )
                anchor_id = int(cur.lastrowid or 0)
            except sqlite3.IntegrityError:
                # UNIQUE conflict — update the existing row.
                conn.execute(
                    """UPDATE memory_symbol_anchors
                          SET drawer_id = ?,
                              content_hash = ?,
                              confidence = ?,
                              source = ?,
                              anchor_kind = ?
                        WHERE route_id = ?
                          AND symbol_name = ?
                          AND file_path = ?""",
                    (
                        drawer_id,
                        content_hash,
                        confidence,
                        source,
                        anchor_kind,
                        route_id,
                        symbol_name,
                        file_path or "",
                    ),
                )
                row = conn.execute(
                    """SELECT anchor_id FROM memory_symbol_anchors
                       WHERE route_id = ?
                         AND symbol_name = ?
                         AND file_path = ?""",
                    (route_id, symbol_name, file_path or ""),
                ).fetchone()
                anchor_id = int(row[0]) if row else 0
            conn.commit()
            return anchor_id

    # ------------------------------------------------------------------
    # Public read API — list_anchors_for_*
    # ------------------------------------------------------------------

    def list_anchors_for_unit(
        self,
        *,
        symbol_name: str,
        file_path: str = "",
    ) -> list[Anchor]:
        """Return all anchors for a given (symbol_name, file_path).

        Used by edit_memory_gate to surface anchored memory before
        edits. Returns anchors of ALL confidence tiers; the caller
        (gate) applies confidence-tier gating per RFC-4 §3.5.
        """
        self._ensure_schema_for_current_path()
        # Matching parity with edit_memory_gate.query_anchored_memories
        # (the memory_route side). When the caller has both symbol and
        # file, match symbol-pinned-to-file OR symbol-pinned-anywhere OR
        # file-only anchors for that file. When only file is given (the
        # ai_replace mode='string' case — no symbol resolution), match
        # ALL anchors on the file (symbol-kind too). When only symbol
        # is given, match all symbol-anchored rows for that name.
        with sqlite3.connect(str(self.db_path)) as conn:
            cols = """SELECT anchor_id, route_id, symbol_name, file_path,
                              anchor_kind, confidence, drawer_id,
                              content_hash, source, created_at
                       FROM memory_symbol_anchors """
            if symbol_name and file_path:
                rows = conn.execute(
                    cols
                    + """WHERE
                        (anchor_kind = 'symbol' AND symbol_name = ?
                         AND (file_path = '' OR file_path = ?))
                        OR
                        (anchor_kind = 'file' AND file_path = ?)""",
                    (symbol_name, file_path, file_path),
                ).fetchall()
            elif file_path:
                rows = conn.execute(
                    cols + "WHERE file_path = ?",
                    (file_path,),
                ).fetchall()
            else:
                rows = conn.execute(
                    cols + "WHERE anchor_kind = 'symbol' AND symbol_name = ?",
                    (symbol_name,),
                ).fetchall()
        return [_row_to_anchor(r) for r in rows]

    def list_anchors_for_drawer(self, drawer_id: str) -> list[Anchor]:
        """Return all anchors pointing at a specific palace drawer."""
        with sqlite3.connect(str(self.db_path)) as conn:
            rows = conn.execute(
                """SELECT anchor_id, route_id, symbol_name, file_path,
                          anchor_kind, confidence, drawer_id,
                          content_hash, source, created_at
                   FROM memory_symbol_anchors
                   WHERE drawer_id = ?""",
                (drawer_id,),
            ).fetchall()
        return [_row_to_anchor(r) for r in rows]

    def list_anchors_blocking_edit(
        self,
        *,
        file_path: str,
    ) -> list[Anchor]:
        """Return only confidence tiers that BLOCK an edit per RFC-4 §3.5.

        Used by edit_memory_gate to enforce the gating rule:
          exact_symbol or operator_pinned → blocks (ack required)
          file_anchor → blocks only if config.file_anchor_strict
          semantic_guess → never blocks
        """
        with sqlite3.connect(str(self.db_path)) as conn:
            rows = conn.execute(
                """SELECT anchor_id, route_id, symbol_name, file_path,
                          anchor_kind, confidence, drawer_id,
                          content_hash, source, created_at
                   FROM memory_symbol_anchors
                   WHERE file_path = ?
                     AND confidence IN ('exact_symbol', 'operator_pinned')""",
                (file_path,),
            ).fetchall()
        return [_row_to_anchor(r) for r in rows]

    # ------------------------------------------------------------------
    # Stale-tracking support (Phase D)
    # ------------------------------------------------------------------

    def mark_stale(
        self,
        *,
        old_content_hash: str,
        new_content_hash: str,
    ) -> list[int]:
        """Mark anchors as stale when their snapshot no longer matches.

        Returns the list of anchor_ids whose content_hash mismatches.
        Used by ai_index_sync (Phase D) after a unit's content changes.

        This method does NOT mutate the anchor rows themselves —
        staleness is computed on the fly at read time by comparing
        content_hash. The return value lets callers emit StaleUnitEvents.
        """
        with sqlite3.connect(str(self.db_path)) as conn:
            rows = conn.execute(
                """SELECT anchor_id FROM memory_symbol_anchors
                   WHERE content_hash = ? AND content_hash != ''""",
                (old_content_hash,),
            ).fetchall()
        return [int(r[0]) for r in rows]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_anchor(row: tuple) -> Anchor:
    return Anchor(
        anchor_id=int(row[0]),
        route_id=int(row[1]),
        symbol_name=row[2] or "",
        file_path=row[3] or "",
        anchor_kind=row[4] or "symbol",
        confidence=row[5] or "operator_pinned",
        drawer_id=row[6] if row[6] else None,
        content_hash=row[7] or "",
        source=row[8] or "memory_store",
        created_at=row[9] or "",
    )


def _now() -> str:
    from datetime import datetime

    return datetime.now(tz=UTC).isoformat()
