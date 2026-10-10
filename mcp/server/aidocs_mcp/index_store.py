from __future__ import annotations

from pathlib import Path

from ._sqlite_index_store_base import SQLiteIndexStoreBase
from .session_store import SessionStore


class IndexStore(SQLiteIndexStoreBase):
    """Derived SQLite index for memory and session metadata."""

    def __init__(self, session_store: SessionStore) -> None:
        self.session_store = session_store

    def init_db(self, project_root: Path) -> None:
        with self.connect(project_root) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    path TEXT NOT NULL,
                    title TEXT,
                    status TEXT,
                    owner TEXT,
                    goal TEXT,
                    last_updated TEXT
                );

                CREATE TABLE IF NOT EXISTS memory_files (
                    path TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    title TEXT,
                    content_text TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memory_links (
                    source_path TEXT NOT NULL,
                    target_path TEXT NOT NULL,
                    PRIMARY KEY (source_path, target_path)
                );

                CREATE TABLE IF NOT EXISTS memory_routes (
                    route_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_path TEXT NOT NULL UNIQUE,
                    trigger TEXT NOT NULL DEFAULT 'topic'
                        CHECK (trigger IN ('topic', 'action')),
                    priority TEXT NOT NULL DEFAULT 'normal'
                        CHECK (priority IN ('high', 'normal', 'low')),
                    severity TEXT NOT NULL DEFAULT 'normal'
                        CHECK (severity IN ('normal', 'high', 'critical', 'sovereign')),
                    injection_mode TEXT NOT NULL DEFAULT 'pointer'
                        CHECK (injection_mode IN ('pointer', 'skip')),
                    sovereign_owner TEXT,
                    source TEXT NOT NULL
                        CHECK (source IN ('toml-import', 'frontmatter-import', 'capture', 'manual')),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS memory_route_keywords (
                    keyword TEXT NOT NULL,
                    locale TEXT NOT NULL DEFAULT '*',
                    route_id INTEGER NOT NULL,
                    PRIMARY KEY (keyword, locale, route_id),
                    FOREIGN KEY (route_id) REFERENCES memory_routes(route_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_memory_route_keywords_keyword
                    ON memory_route_keywords(keyword);

                -- #688: keyword lemmas, computed ONCE at route-registration
                -- time and matched by set intersection on the prompt path.
                -- model_stamp says WHAT PRODUCED the row: a row made by a
                -- superseded spaCy model is stale and is never served, so a
                -- model upgrade cannot silently stop a route from matching.
                -- Keyed per (keyword, language) — a keyword's lemma differs
                -- per language pack (#680 seeded Romanian).
                CREATE TABLE IF NOT EXISTS keyword_lemmas (
                    keyword     TEXT NOT NULL,
                    language    TEXT NOT NULL,
                    lemmas      TEXT NOT NULL DEFAULT '[]',
                    model_stamp TEXT NOT NULL DEFAULT '',
                    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (keyword, language)
                );

                CREATE TABLE IF NOT EXISTS memory_symbol_anchors (
                    anchor_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    route_id INTEGER NOT NULL,
                    symbol_name TEXT NOT NULL,
                    file_path TEXT NOT NULL DEFAULT '',
                    anchor_kind TEXT NOT NULL DEFAULT 'symbol'
                        CHECK (anchor_kind IN ('symbol', 'file', 'module', 'domain')),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (route_id) REFERENCES memory_routes(route_id) ON DELETE CASCADE,
                    UNIQUE (route_id, symbol_name, file_path)
                );

                CREATE INDEX IF NOT EXISTS idx_memory_symbol_anchors_symbol
                    ON memory_symbol_anchors(symbol_name);

                CREATE INDEX IF NOT EXISTS idx_memory_symbol_anchors_file
                    ON memory_symbol_anchors(file_path);
                """,
            )
            # memory_files is INERT compatibility schema (SQLite-only doctrine,
            # 2026-06): created so legacy DDL/migrations don't error, but it has
            # NO runtime readers or writers — canonical memory lives in
            # memory_index. Ensure the canonical table exists wherever the index
            # db is initialized so every retargeted reader finds it.
            # Memory-loop seal (2026-07-09): keyword provenance + weight.
            # 'explicit' terms come from the operator/agent at capture time
            # and outrank 'derived' terms (NLP/code-analyzer output). Weight
            # is a ranking hint within a provenance tier.
            self._ensure_column(
                conn, "memory_route_keywords", "provenance", "TEXT NOT NULL DEFAULT 'explicit'",
            )
            self._ensure_column(
                conn, "memory_route_keywords", "weight", "REAL NOT NULL DEFAULT 1.0",
            )
            self._ensure_column(conn, "memory_files", "title", "TEXT")
            self._ensure_column(conn, "memory_files", "content_text", "TEXT NOT NULL DEFAULT ''")
            try:
                from . import memory_sqlite_store as _msq

                _msq._ensure_table(conn)
            except Exception:
                pass
            # Phase 2 (2026-05-15): extend memory_symbol_anchors.anchor_kind
            # CHECK constraint with 'domain'. Pre-existing DBs created with
            # the old enum need a rebuild — sqlite cannot ALTER CHECK in
            # place. Detect via sqlite_master schema text, rebuild only when
            # 'domain' isn't already allowed. Idempotent.
            self._ensure_anchor_kind_domain(conn)

            # 2026-05-19 Phase-8 flip: ensure memory_index table +
            # taxonomy columns exist BEFORE the canonical_rows view is
            # installed (the view projects from memory_index now, so
            # the table must exist first). Idempotent: ALTER TABLE
            # ADD COLUMN is wrapped in try/except per column.
            try:
                from .memory_sqlite_store import _ensure_table as _ensure_memory_index

                _ensure_memory_index(conn)
            except Exception:
                pass

            # 2026-05-19: install the canonical_rows VIEW unifying every
            # canonical store under one taxonomy (stable_key / kind /
            # scope / source / status / version / revision / superseded_by
            # / read_access / created_at / updated_at). See
            # aidocs_mcp.canonical_taxonomy for the projection rules.
            # Idempotent — runs free on every connection. Safe to call
            # before all source tables exist (silently skips). Pinned by
            # test_canonical_taxonomy.
            try:
                from .canonical_taxonomy import install_canonical_view

                install_canonical_view(conn)
            except Exception:
                # Canonical view is a read surface; install failure must
                # NOT brick the core init path. Tests detect missing
                # view explicitly.
                pass

    def _ensure_anchor_kind_domain(self, conn) -> None:
        """Idempotent migration: ensure memory_symbol_anchors.anchor_kind
        CHECK constraint accepts 'domain'. Sqlite cannot ALTER CHECK in
        place, so for legacy DBs we rebuild the table.

        Phase 2 (2026-05-15) — adds domain anchors so memories can attach
        to a concept (workflow, build-discipline, auth-flow) rather than
        only a concrete symbol/file/module.
        """
        # #242: rebuild runs atomically (one BEGIN IMMEDIATE…COMMIT) — the
        # prior single executescript autocommitted per statement, leaving a
        # kill window between DROP and RENAME (the 2026-07-04 backlog-store
        # incident's exact shape). The recovery guard runs FIRST so a prior
        # interrupted rebuild (rows stranded in _new, table missing) heals
        # instead of being skipped by the row-is-None early return.
        # foreign_keys toggles outside the txn (sqlite ignores it inside one).
        from .store_migrations import atomic_rebuild, recover_interrupted_rename

        recover_interrupted_rename(conn, "memory_symbol_anchors", suffix="_new")
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_symbol_anchors'",
        ).fetchone()
        if row is None:
            return
        schema_sql = str(row[0] if not hasattr(row, "keys") else row["sql"])
        if "'domain'" in schema_sql:
            return  # already migrated (or recovery completed a migrated table)
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            atomic_rebuild(
                conn,
                [
                    """
                    CREATE TABLE memory_symbol_anchors_new (
                        anchor_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        route_id INTEGER NOT NULL,
                        symbol_name TEXT NOT NULL,
                        file_path TEXT NOT NULL DEFAULT '',
                        anchor_kind TEXT NOT NULL DEFAULT 'symbol'
                            CHECK (anchor_kind IN ('symbol', 'file', 'module', 'domain')),
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (route_id) REFERENCES memory_routes(route_id) ON DELETE CASCADE,
                        UNIQUE (route_id, symbol_name, file_path)
                    )
                    """,
                    """
                    INSERT INTO memory_symbol_anchors_new
                        (anchor_id, route_id, symbol_name, file_path, anchor_kind, created_at)
                    SELECT anchor_id, route_id, symbol_name, file_path, anchor_kind, created_at
                    FROM memory_symbol_anchors
                    """,
                    "DROP TABLE memory_symbol_anchors",
                    "ALTER TABLE memory_symbol_anchors_new RENAME TO memory_symbol_anchors",
                    ("CREATE INDEX IF NOT EXISTS idx_memory_symbol_anchors_symbol "
                    "ON memory_symbol_anchors(symbol_name)"),
                    ("CREATE INDEX IF NOT EXISTS idx_memory_symbol_anchors_file "
                    "ON memory_symbol_anchors(file_path)"),
                ],
            )
        finally:
            conn.execute("PRAGMA foreign_keys = ON")

    def status(self, project_root: Path) -> dict[str, object]:
        self.init_db(project_root)
        from . import memory_sqlite_store as _msq
        with self.connect(project_root) as conn:
            try:
                _msq._ensure_table(conn)  # canonical memory_index may not exist yet
            except Exception:
                pass
            session_count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            memory_count = conn.execute(
                "SELECT COUNT(*) FROM memory_index "
                "WHERE COALESCE(status,'active')='active' AND COALESCE(superseded_by,'')=''",
            ).fetchone()[0]
            # memory_links is RETIRED (no runtime reader); do not query it.
            route_count = conn.execute("SELECT COUNT(*) FROM memory_routes").fetchone()[0]
            keyword_count = conn.execute("SELECT COUNT(*) FROM memory_route_keywords").fetchone()[0]
        return {
            "db_path": str(self.db_path(project_root)),
            "sessions": int(session_count),
            # SQLite-only doctrine (2026-06): "memory_files" now reports the
            # canonical active memory_index count (the memory_files TABLE is inert
            # — never read/written at runtime). memory_links is RETIRED.
            "memory_files": int(memory_count),
            "memory_files_table_status": "inert_deprecated",
            "memory_links": 0,
            "memory_links_status": "retired_inert",
            "memory_routes": int(route_count),
            "memory_route_keywords": int(keyword_count),
            "freshness": self._memory_freshness(project_root),
            # Phase-8 lag visibility: surface recent route/anchor/export
            # lag so the dashboard (ai_index_status) shows when a capture
            # landed but its discovery wiring (keyword route / symbol
            # anchor) or markdown export lagged behind. Operators looking
            # the memory up by keyword/symbol may miss it until backfill;
            # the events were emitted to execution_events but never
            # surfaced in the health probe. Best-effort.
            "memory_lag": self._memory_lag_summary(project_root),
        }

    def routing_orphans(self, project_root: Path) -> dict[str, object]:
        """SQL/routing-graph orphan diagnostic (replaces the retired
        markdown-scanning rule_orphan_finder under the no-file-layer doctrine).

        SQLite-only doctrine (2026-06): reachability is the ``memory_routes``
        topic/action router ONLY. ``memory_links`` is RETIRED (never rebuilt from
        markdown, always empty) and is NO LONGER consulted — a rule/standards/
        system/domains canonical-active entry is an ORPHAN when it has NO
        ``memory_routes`` row. Read entirely from the sqlite memory_index.
        """
        self.init_db(project_root)
        candidate_prefixes = ("rules/", "standards/", "system/", "domains/")
        like = " OR ".join("mf.path LIKE ?" for _ in candidate_prefixes)
        params = [f"{p}%" for p in candidate_prefixes]
        orphans: list[dict[str, str]] = []
        total = 0
        from . import memory_sqlite_store as _msq
        active = "COALESCE(mf.status,'active')='active' AND COALESCE(mf.superseded_by,'')=''"
        with self.connect(project_root) as conn:
            try:
                _msq._ensure_table(conn)
            except Exception:
                pass
            total = conn.execute(
                f"SELECT COUNT(*) FROM memory_index mf WHERE ({like}) AND {active}",
                params,
            ).fetchone()[0]
            rows = conn.execute(
                f"""
                SELECT mf.path AS path, mf.kind AS kind, COALESCE(mf.title,'') AS title
                FROM memory_index mf
                WHERE ({like}) AND {active}
                  AND NOT EXISTS (
                      SELECT 1 FROM memory_routes mr
                      WHERE mr.target_path = mf.path
                  )
                ORDER BY mf.path
                """,
                params,
            ).fetchall()
        for row in rows:
            path = str(row["path"])
            orphans.append(
                {
                    "path": path,
                    "kind": str(row["kind"] or ""),
                    "title": str(row["title"] or ""),
                    "directory": path.split("/", 1)[0],
                },
            )
        return {
            "ok": True,
            "source": "memory_index",
            # Honest about the retired link layer: reachability is route-only now.
            "reachability": "memory_routes",
            "memory_links": "deprecated_inert",
            "orphan_count": len(orphans),
            "total_candidates": int(total or 0),
            "orphans": orphans,
        }

    @staticmethod
    def _memory_lag_summary(project_root: Path) -> dict[str, int]:
        """Recent route/anchor/export lag counts for dashboard visibility.

        Reads the execution_events-backed helpers in memory_sqlite_store
        (a different DB than the derived index, same project). Returns a
        counts dict; zeros on any error so the health probe never fails.
        """
        out = {"route_lag": 0, "anchor_lag": 0, "export_lag": 0}
        try:
            from . import memory_sqlite_store as _msq

            out["route_lag"] = len(_msq.recent_route_lag_events(project_root))
            out["anchor_lag"] = len(_msq.recent_anchor_lag_events(project_root))
            out["export_lag"] = len(_msq.recent_export_lag_events(project_root))
        except Exception:
            pass
        return out

    def sync_sessions(self, project_root: Path) -> int:
        self.init_db(project_root)
        sessions = self.session_store.list_sessions(project_root)
        with self.connect(project_root) as conn:
            conn.execute("DELETE FROM sessions")
            conn.executemany(
                """
                INSERT INTO sessions (session_id, path, title, status, owner, goal, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        s.session_id,
                        str(s.path),
                        s.title,
                        s.status,
                        s.owner,
                        s.goal,
                        s.last_updated,
                    )
                    for s in sessions
                ],
            )
        return len(sessions)

    def sync_memory_files(self, project_root: Path) -> int:
        """SQLite-only doctrine (2026-06): NO-OP indexer.

        Memory is canonical in sqlite (memory_index). This legacy indexer used to
        walk .MEMORY/*.md, SHA-hash each file, and (re)populate the memory_files
        cache + memory_links + silently seed-import unmigrated markdown into
        memory_index. ALL of that is removed:
          - no filesystem walk / SHA hashing,
          - memory_files is inert (never written),
          - memory_links is retired (never rebuilt from markdown),
          - markdown import is the EXPLICIT operator-only
            memory_sqlite_store.migrate_markdown_to_sqlite; runtime never invokes
            it implicitly.

        Returns the count of ACTIVE canonical memory_index rows so callers
        (cli/index_sync) keep a meaningful "Memory: N" figure without any disk I/O.
        """
        self.init_db(project_root)  # init_db ensures memory_index exists
        from . import memory_sqlite_store as _msq
        with self.connect(project_root) as conn:
            try:
                _msq._ensure_table(conn)
            except Exception:
                pass
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM memory_index "
                    "WHERE COALESCE(status,'active')='active' AND COALESCE(superseded_by,'')=''",
                ).fetchone()
            except Exception:
                return 0
        return int(row[0]) if row else 0

    def upsert_memory_route(
        self,
        project_root: Path,
        target_path: str,
        *,
        severity: str = "normal",
        trigger: str = "topic",
        priority: str = "normal",
        injection_mode: str = "pointer",
        sovereign_owner: str | None = None,
        source: str = "capture",
        keywords: tuple[str, ...] = (),
        derived_keywords: tuple[str, ...] = (),
        derived_weight: float = 0.5,
        locale: str = "*",
    ) -> int:
        """Upsert a memory route + its keywords. Returns route_id.

        CHECK constraints on severity/trigger/priority/injection_mode/source
        enforce valid values at the DB layer (120% enforceable). Keywords are
        registered atomically with the route — capture closes the discovery
        loop in one call, no hand-edit of frontmatter required.

        Provenance law (memory-loop seal, 2026-07-09): ``keywords`` are
        EXPLICIT (operator/agent-supplied, provenance='explicit', weight=1.0)
        and always win — re-capturing a term explicitly UPGRADES a prior
        derived row. ``derived_keywords`` (analyzer output) insert as
        provenance='derived' at ``derived_weight`` and NEVER overwrite an
        existing explicit row (INSERT OR IGNORE).
        """
        self.init_db(project_root)
        normalized_target = target_path.replace("\\", "/")
        with self.connect(project_root) as conn:
            row = conn.execute(
                "SELECT route_id FROM memory_routes WHERE target_path = ?",
                (normalized_target,),
            ).fetchone()
            if row is None:
                cur = conn.execute(
                    """
                    INSERT INTO memory_routes
                        (target_path, trigger, priority, severity,
                         injection_mode, sovereign_owner, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_target,
                        trigger,
                        priority,
                        severity,
                        injection_mode,
                        sovereign_owner,
                        source,
                    ),
                )
                route_id = int(cur.lastrowid)
            else:
                route_id = int(row["route_id"])
                conn.execute(
                    """
                    UPDATE memory_routes SET
                        trigger = ?, priority = ?, severity = ?,
                        injection_mode = ?, sovereign_owner = ?,
                        source = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE route_id = ?
                    """,
                    (
                        trigger,
                        priority,
                        severity,
                        injection_mode,
                        sovereign_owner,
                        source,
                        route_id,
                    ),
                )
            for kw in keywords:
                kw_normalized = str(kw).strip().lower()
                if not kw_normalized:
                    continue
                conn.execute(
                    """
                    INSERT OR IGNORE INTO memory_route_keywords
                        (keyword, locale, route_id, provenance, weight)
                    VALUES (?, ?, ?, 'explicit', 1.0)
                    """,
                    (kw_normalized, locale, route_id),
                )
                # Explicit outranks derived: an operator re-stating a term
                # the analyzer previously derived upgrades the row in place.
                conn.execute(
                    """
                    UPDATE memory_route_keywords
                       SET provenance = 'explicit', weight = 1.0
                     WHERE keyword = ? AND locale = ? AND route_id = ?
                    """,
                    (kw_normalized, locale, route_id),
                )
            explicit_set = {
                str(kw).strip().lower() for kw in keywords if str(kw).strip()
            }
            for kw in derived_keywords:
                kw_normalized = str(kw).strip().lower()
                if not kw_normalized or kw_normalized in explicit_set:
                    continue
                # OR IGNORE: never downgrades an existing explicit row.
                conn.execute(
                    """
                    INSERT OR IGNORE INTO memory_route_keywords
                        (keyword, locale, route_id, provenance, weight)
                    VALUES (?, ?, ?, 'derived', ?)
                    """,
                    (kw_normalized, locale, route_id, float(derived_weight)),
                )
        # #688 WRITE EDGE: lemmatise the keywords ONCE, here, where they change.
        # The prompt path then matches by set intersection with ZERO spaCy calls
        # instead of one full pipeline run per candidate keyword (~1022/prompt).
        # Outside the `with` block so it never nests a second connection inside
        # the route transaction; best-effort so NLP can never fail a capture.
        try:
            from . import keyword_lemma_lexicon

            keyword_lemma_lexicon.ensure_keywords(
                project_root,
                tuple(keywords) + tuple(derived_keywords),
                language=keyword_lemma_lexicon.language_for_locale(locale),
            )
        except Exception:
            pass
        return route_id

    def delete_memory_route(self, project_root: Path, target_path: str) -> bool:
        """Delete a route and its keywords atomically. Returns True if a row
        was removed. Manual cascade because PRAGMA foreign_keys is OFF in the
        shared connection base — local enforcement keeps the table clean
        regardless of the base-class setting.
        """
        self.init_db(project_root)
        normalized_target = target_path.replace("\\", "/")
        with self.connect(project_root) as conn:
            row = conn.execute(
                "SELECT route_id FROM memory_routes WHERE target_path = ?",
                (normalized_target,),
            ).fetchone()
            if row is None:
                return False
            route_id = int(row["route_id"])
            conn.execute(
                "DELETE FROM memory_route_keywords WHERE route_id = ?",
                (route_id,),
            )
            conn.execute(
                "DELETE FROM memory_routes WHERE route_id = ?",
                (route_id,),
            )
            conn.commit()
        return True

    def get_memory_route(self, project_root: Path, target_path: str) -> dict[str, object] | None:
        """Read a route by target_path. Returns None if absent."""
        self.init_db(project_root)
        normalized_target = target_path.replace("\\", "/")
        with self.connect(project_root) as conn:
            row = conn.execute(
                """
                SELECT route_id, target_path, trigger, priority, severity,
                       injection_mode, sovereign_owner, source,
                       created_at, updated_at
                FROM memory_routes WHERE target_path = ?
                """,
                (normalized_target,),
            ).fetchone()
            if row is None:
                return None
            keywords = [
                {"keyword": str(r["keyword"]), "locale": str(r["locale"])}
                for r in conn.execute(
                    """
                    SELECT keyword, locale FROM memory_route_keywords
                    WHERE route_id = ? ORDER BY locale, keyword
                    """,
                    (row["route_id"],),
                )
            ]
        return {
            "route_id": int(row["route_id"]),
            "target_path": str(row["target_path"]),
            "trigger": str(row["trigger"]),
            "priority": str(row["priority"]),
            "severity": str(row["severity"]),
            "injection_mode": str(row["injection_mode"]),
            "sovereign_owner": row["sovereign_owner"],
            "source": str(row["source"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "keywords": keywords,
        }

    def upsert_memory_anchor(
        self,
        project_root: Path,
        *,
        route_id: int,
        symbol_name: str,
        file_path: str = "",
        anchor_kind: str = "symbol",
        confidence: str = "operator_pinned",
        source: str = "memory_store",
        leaf_granularity: str = "",
    ) -> int:
        """Upsert a memory→symbol anchor. Returns anchor_id.

        ``leaf_granularity`` (#375 Phase 3): the smallest resolvable
        code-index leaf this anchor pins (file/module/family/class/
        function/method/symbol; '' for legacy/unresolved). Written only
        when the column exists (anchor_schema_migration adds it); the
        legacy anchor_kind CHECK set is untouched.

        ``confidence`` (memory-loop seal, 2026-07-09): explicit capture-time
        anchors default to 'operator_pinned' (edit-blocking tier); the
        capture analyzer registers auto-derived anchors at 'semantic_guess'
        (advisory — surfaces on read, NEVER blocks an edit). Written only
        when the RFC-4 columns exist (anchor_schema_migration); pre-migration
        DBs keep their legacy shape and legacy (blocking) semantics.

        Links a memory route to a code symbol (or file/module). Brick 1.5
        first half — capture-side anchoring. Discovery-side hop traversal
        (walk code_edges to find memories anchored on dependencies of a
        symbol the agent is touching) is the next half-brick.
        """
        self.init_db(project_root)
        sym = (symbol_name or "").strip()
        kind = (anchor_kind or "symbol").strip().lower() or "symbol"
        fp = (file_path or "").replace("\\", "/")
        if not sym and kind != "file":
            raise ValueError(
                "symbol_name is required for anchor_kind='symbol'; "
                "use anchor_kind='file' for file-only anchors",
            )
        if not sym and not fp:
            raise ValueError("file-only anchor requires file_path")

        conf = (confidence or "operator_pinned").strip().lower() or "operator_pinned"
        src = (source or "memory_store").strip() or "memory_store"
        leaf = (leaf_granularity or "").strip().lower()
        if leaf:
            from .anchor_schema_migration import validate_leaf_granularity

            validate_leaf_granularity(leaf)
        with self.connect(project_root) as conn:
            def _cols() -> set[str]:
                return {
                    row[1]
                    for row in conn.execute(
                        "PRAGMA table_info(memory_symbol_anchors)",
                    ).fetchall()
                }

            def _has_rfc4_cols() -> bool:
                return "confidence" in _cols()

            has_rfc4_cols = _has_rfc4_cols()
            if not has_rfc4_cols and conf == "semantic_guess":
                # A semantic_guess anchor written WITHOUT the confidence
                # column would read back as a legacy (blocking) row —
                # advisory silently promoted to law. Migrate first; if the
                # migration fails, refuse the write rather than block edits.
                try:
                    from .anchor_schema_migration import migrate_memory_symbol_anchors

                    migrate_memory_symbol_anchors(conn)
                    has_rfc4_cols = _has_rfc4_cols()
                except Exception:
                    has_rfc4_cols = False
                if not has_rfc4_cols:
                    raise ValueError(
                        "semantic_guess anchor refused: memory_symbol_anchors "
                        "lacks the confidence column and migration failed",
                    )
            # #375 Phase 3: the leaf column rides the same idempotent
            # migration. A requested granularity on a pre-migration table
            # migrates first; if that fails the anchor still lands, just
            # without the leaf marker (granularity is advisory metadata,
            # never worth losing the anchor over).
            has_leaf_col = "leaf_granularity" in _cols()
            if leaf and has_rfc4_cols and not has_leaf_col:
                try:
                    from .anchor_schema_migration import (
                        migrate_memory_symbol_anchors,
                    )

                    migrate_memory_symbol_anchors(conn)
                    has_leaf_col = "leaf_granularity" in _cols()
                except Exception:
                    has_leaf_col = False
            existing = conn.execute(
                "SELECT anchor_id FROM memory_symbol_anchors "
                "WHERE route_id = ? AND symbol_name = ? AND file_path = ?",
                (route_id, sym, fp),
            ).fetchone()
            if existing is not None:
                anchor_id = int(existing["anchor_id"])
                if has_rfc4_cols and has_leaf_col:
                    conn.execute(
                        "UPDATE memory_symbol_anchors "
                        "SET anchor_kind = ?, confidence = ?, source = ?, "
                        "leaf_granularity = ? "
                        "WHERE anchor_id = ?",
                        (kind, conf, src, leaf, anchor_id),
                    )
                elif has_rfc4_cols:
                    conn.execute(
                        "UPDATE memory_symbol_anchors "
                        "SET anchor_kind = ?, confidence = ?, source = ? "
                        "WHERE anchor_id = ?",
                        (kind, conf, src, anchor_id),
                    )
                else:
                    conn.execute(
                        "UPDATE memory_symbol_anchors SET anchor_kind = ? WHERE anchor_id = ?",
                        (kind, anchor_id),
                    )
                return anchor_id
            if has_rfc4_cols and has_leaf_col:
                cur = conn.execute(
                    """
                    INSERT INTO memory_symbol_anchors
                        (route_id, symbol_name, file_path, anchor_kind,
                         confidence, source, leaf_granularity)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (route_id, sym, fp, kind, conf, src, leaf),
                )
            elif has_rfc4_cols:
                cur = conn.execute(
                    """
                    INSERT INTO memory_symbol_anchors
                        (route_id, symbol_name, file_path, anchor_kind,
                         confidence, source)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (route_id, sym, fp, kind, conf, src),
                )
            else:
                cur = conn.execute(
                    """
                    INSERT INTO memory_symbol_anchors
                        (route_id, symbol_name, file_path, anchor_kind)
                    VALUES (?, ?, ?, ?)
                    """,
                    (route_id, sym, fp, kind),
                )
            return int(cur.lastrowid)

    def get_memory_anchors(
        self,
        project_root: Path,
        *,
        route_id: int | None = None,
        symbol_name: str | None = None,
    ) -> list[dict[str, object]]:
        """Read anchor rows; filter by route_id, symbol_name, both, or neither."""
        self.init_db(project_root)
        sql = (
            "SELECT anchor_id, route_id, symbol_name, file_path, "
            "anchor_kind, created_at FROM memory_symbol_anchors WHERE 1=1"
        )
        params: list[object] = []
        if route_id is not None:
            sql += " AND route_id = ?"
            params.append(route_id)
        if symbol_name:
            sql += " AND symbol_name = ?"
            params.append(symbol_name.strip())
        sql += " ORDER BY anchor_id"
        with self.connect(project_root) as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [
            {
                "anchor_id": int(r["anchor_id"]),
                "route_id": int(r["route_id"]),
                "symbol_name": str(r["symbol_name"]),
                "file_path": str(r["file_path"]),
                "anchor_kind": str(r["anchor_kind"]),
                "created_at": str(r["created_at"]),
            }
            for r in rows
        ]

    def sync_all(self, project_root: Path) -> dict[str, int]:
        return {
            "sessions": self.sync_sessions(project_root),
            "memory_files": self.sync_memory_files(project_root),
        }

    def search_memory(
        self,
        project_root: Path,
        query: str,
        limit: int = 10,
    ) -> list[dict[str, str]]:
        """Keyword search over CANONICAL active memory_index rows only
        (SQLite-only doctrine, 2026-06). No .MEMORY markdown, no memory_files
        cache: the sqlite memory_index is the single source of truth. Superseded
        / removed rows are never surfaced.
        """
        self.init_db(project_root)
        needle = query.strip()
        if not needle:
            return []
        # Multi-word queries are tokenized: every token must appear somewhere
        # in (path, title, content) — not the literal phrase. Pre-fix, a query
        # like "wiring probe memory" matched NOTHING unless the exact phrase
        # appeared verbatim; single keywords worked. Exact-phrase hits still
        # rank first.
        tokens = [t for t in needle.split() if t]
        pattern = f"%{needle}%"
        from . import memory_sqlite_store as _msq

        with self.connect(project_root) as conn:
            # Ensure the canonical table exists even before the first write.
            try:
                _msq._ensure_table(conn)
            except Exception:
                pass
            token_clause = " AND ".join(
                "(path LIKE ? OR COALESCE(title, '') LIKE ? OR content LIKE ?)"
                for _ in tokens
            )
            token_params: list[object] = []
            for tok in tokens:
                tp = f"%{tok}%"
                token_params.extend([tp, tp, tp])
            rows = conn.execute(
                f"""
                SELECT path, kind, COALESCE(title, '') AS title, content
                FROM memory_index
                WHERE COALESCE(status, 'active') = 'active'
                  AND COALESCE(superseded_by, '') = ''
                  AND ({token_clause})
                ORDER BY CASE
                    WHEN content LIKE ? OR COALESCE(title, '') LIKE ? THEN 0
                    WHEN COALESCE(title, '') LIKE ? THEN 1
                    WHEN path LIKE ? THEN 2
                    ELSE 3
                END, path ASC
                LIMIT ?
                """,
                (*token_params, pattern, pattern, f"%{tokens[0]}%", f"%{tokens[0]}%", limit),
            ).fetchall()
        return [
            {
                "path": row["path"],
                "kind": row["kind"],
                "title": row["title"] or "",
                "snippet": self._make_snippet(
                    row["content"],
                    self._best_snippet_anchor(row["content"], needle, tokens),
                ),
            }
            for row in rows
        ]

    def _kind_for(self, relative_path: str) -> str:
        if relative_path.startswith(".aidocs/"):
            return "aidocs"
        if relative_path.startswith("sessions/"):
            return "session"
        if relative_path.startswith("rules/"):
            return "rule"
        if relative_path.startswith("system/"):
            return "system"
        if relative_path.startswith("domains/"):
            return "domain"
        if relative_path.startswith("roadmaps/"):
            return "roadmap"
        if relative_path.startswith("specs/"):
            return "spec"
        if relative_path.startswith("config/"):
            return "config"
        if relative_path.startswith("related-projects/"):
            return "related_project"
        if relative_path.startswith("daily/"):
            return "daily"
        return "memory"


    def _extract_title(self, text: str) -> str | None:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                return stripped[2:].strip()
        return None

    def _best_snippet_anchor(self, content: str, needle: str, tokens: list[str]) -> str:
        """Pick the string the snippet should center on (backlog #220).

        Exact-phrase hit → the phrase itself. Otherwise a tokenized query
        used to anchor on the FIRST token occurrence, which on a
        consolidated memory file often lands on the container's header
        ('# Workflow') instead of the sub-rule the query actually matched.
        Fix: score each LINE by how many distinct query tokens it contains
        and anchor on the best line's first matching token, so the snippet
        shows the consolidated sub-rule, not the file header.
        """
        text = content or ""
        lower = text.lower()
        if needle and needle.lower() in lower:
            return needle
        toks = [t for t in tokens if t and t.lower() in lower]
        if not toks:
            return needle
        best_anchor = toks[0]
        best_score = 0
        for line in text.splitlines():
            line_l = line.lower()
            present = [t for t in toks if t.lower() in line_l]
            if len(present) > best_score and line.strip():
                best_score = len(present)
                # Anchor on the LINE (not the token): a token's first
                # occurrence elsewhere in the file would drag the window
                # away from the sub-rule this line actually matched.
                best_anchor = line.strip()
        return best_anchor

    def _make_snippet(self, text: str, needle: str, width: int = 120) -> str:
        lower = text.lower()
        index = lower.find(needle.lower())
        if index < 0:
            return text[:width].replace("\n", " ").strip()
        start = max(0, index - width // 3)
        end = min(len(text), index + width)
        return text[start:end].replace("\n", " ").strip()

    def _memory_freshness(self, project_root: Path) -> dict[str, object]:
        """SQLite-only doctrine (2026-06): canonical health, NO SHA walk.

        The prior implementation SHA-walked every .MEMORY/*.md file and diffed it
        against the memory_files cache (path_drift / content_drift). Under the
        no-scroll doctrine the canonical sqlite memory_index IS the source of
        truth that reads serve from, so there is nothing to diff: report a cheap
        canonical health state — missing (no active rows) / ready (active rows) —
        with zero filesystem reads or hashing. (MemPalace lag is a separate axis.)
        """
        from . import memory_sqlite_store as _msq
        active = 0
        reachable = True
        with self.connect(project_root) as conn:
            try:
                _msq._ensure_table(conn)
                row = conn.execute(
                    "SELECT COUNT(*) FROM memory_index "
                    "WHERE COALESCE(status,'active')='active' AND COALESCE(superseded_by,'')=''",
                ).fetchone()
                active = int(row[0]) if row else 0
            except Exception:
                reachable = False
        # An EMPTY memory index is a normal, healthy state (nothing captured yet)
        # — NOT a staleness gate. So 'ready' whenever the canonical store is
        # reachable; 'missing' only when the db/table genuinely can't be read.
        # (This must not force the combined index_status to missing/stale just
        # because a project hasn't captured memory.)
        state = "ready" if reachable else "missing"
        return {
            "state": state,
            "reasons": [] if reachable else ["missing_index_state"],
            "tracked_paths": active,
            "indexed_paths": active,
            "drifted_paths": [],
            "missing_paths": [],
            "extra_paths": [],
            "latest_source_mtime_ns": None,
        }
