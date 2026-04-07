from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .language_descriptors import descriptor_for_language


class CodeIndexSyncService:
    def __init__(self, store: Any) -> None:
        self.store = store

    def sync_code_manifest(self, project_root: Path, include_tests: bool = False) -> int:
        self.store.init_db(project_root)
        manifest_rows: list[tuple[str, str, str | None, str | None, str, int, int]] = []
        seen_paths: set[str] = set()

        for path in self.store._walk_source_files(project_root, include_tests=include_tests):
            if not path.is_file():
                continue
            rel = path.relative_to(project_root).as_posix()
            code_language = self.store._language_for(path, project_root=project_root)
            if code_language is None:
                continue
            descriptor = descriptor_for_language(project_root, rel, path.suffix.lower())
            language_tier = descriptor.tier if descriptor else None
            language_source = descriptor.source if descriptor else None
            stat = path.stat()
            seen_paths.add(rel)
            role = self.store._infer_code_role(project_root, rel, code_language, [])
            manifest_rows.append((rel, code_language, language_tier, language_source, role, int(stat.st_size), int(stat.st_mtime_ns)))

        with self.store.connect(project_root) as conn:
            existing_paths = {row["path"] for row in conn.execute("SELECT path FROM code_files")}
            stale_paths = existing_paths - seen_paths
            for stale in stale_paths:
                conn.execute("DELETE FROM code_files WHERE path = ?", (stale,))
                conn.execute("DELETE FROM code_outlines WHERE path = ?", (stale,))
                conn.execute("DELETE FROM code_edges WHERE source_path = ?", (stale,))

            for rel, language, language_tier, language_source, role, size_bytes, mtime_ns in manifest_rows:
                current = conn.execute(
                    "SELECT size_bytes, mtime_ns FROM code_files WHERE path = ? LIMIT 1",
                    (rel,),
                ).fetchone()
                parsed = 0
                if current and int(current["size_bytes"] or 0) == size_bytes and int(current["mtime_ns"] or 0) == mtime_ns:
                    parsed_row = conn.execute("SELECT parsed FROM code_files WHERE path = ? LIMIT 1", (rel,)).fetchone()
                    parsed = int(parsed_row["parsed"]) if parsed_row else 0
                conn.execute(
                    """
                    INSERT INTO code_files (path, language, language_tier, language_source, checksum, line_count, summary, role, size_bytes, mtime_ns, parsed)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                      language=excluded.language,
                      language_tier=excluded.language_tier,
                      language_source=excluded.language_source,
                      role=excluded.role,
                      size_bytes=excluded.size_bytes,
                      mtime_ns=excluded.mtime_ns,
                      parsed=CASE
                        WHEN code_files.size_bytes = excluded.size_bytes AND code_files.mtime_ns = excluded.mtime_ns THEN code_files.parsed
                        ELSE 0
                      END
                    """,
                    (rel, language, language_tier, language_source, "", 0, "", role, size_bytes, mtime_ns, parsed),
                )
        return len(manifest_rows)

    def sync_code_files(
        self,
        project_root: Path,
        paths: list[str] | None = None,
        include_tests: bool = False,
    ) -> int:
        # Config-level include_tests overrides the default
        from .config import INDEX_INCLUDE_TESTS
        if INDEX_INCLUDE_TESTS:
            include_tests = True
        self.store.init_db(project_root)
        self.sync_code_manifest(project_root, include_tests=include_tests)
        rows: list[tuple[str, str, str | None, str | None, str, int, str, str | None, int, int, int]] = []
        outline_rows: list[tuple[str, str, str, int, str | None, int]] = []
        edge_rows: list[tuple[str, str, str]] = []

        scoped_paths = None
        if paths is not None:
            scoped_paths = {item.replace("\\", "/") for item in paths if str(item).strip()}

        existing_meta = {}
        with self.store.connect(project_root) as conn:
            for row in conn.execute("SELECT path, checksum, size_bytes, mtime_ns, language, language_tier, language_source, line_count, summary, role, parsed FROM code_files"):
                existing_meta[row["path"]] = dict(row)

        seen_paths: set[str] = set()
        for path in self.store._walk_source_files(project_root, include_tests=include_tests):
            if not path.is_file():
                continue
            rel = path.relative_to(project_root).as_posix()
            if scoped_paths is not None and rel not in scoped_paths:
                continue
            seen_paths.add(rel)
            code_language = self.store._language_for(path, project_root=project_root)
            if code_language is None:
                continue
            descriptor = descriptor_for_language(project_root, rel, path.suffix.lower())
            language_tier = descriptor.tier if descriptor else None
            language_source = descriptor.source if descriptor else None
            stat = path.stat()
            size_bytes = int(stat.st_size)
            mtime_ns = int(stat.st_mtime_ns)

            existing = existing_meta.get(rel)
            if existing and existing.get("size_bytes") == size_bytes and existing.get("mtime_ns") == mtime_ns:
                parsed = int(existing.get("parsed") or 0)
                if parsed == 1:
                    rows.append(
                        (
                            rel,
                            str(existing["language"]),
                            existing.get("language_tier"),
                            existing.get("language_source"),
                            str(existing["checksum"]),
                            int(existing["line_count"]),
                            str(existing["summary"]),
                            existing.get("role"),
                            size_bytes,
                            mtime_ns,
                            1,
                        )
                    )
                    with self.store.connect(project_root) as conn:
                        for row in conn.execute(
                            "SELECT symbol, kind, line_number, container, is_partial FROM code_outlines WHERE path = ?",
                            (rel,),
                        ):
                            outline_rows.append((rel, row["symbol"], row["kind"], int(row["line_number"]), row["container"], int(row["is_partial"])))
                        for row in conn.execute(
                            "SELECT target, kind FROM code_edges WHERE source_path = ?",
                            (rel,),
                        ):
                            edge_rows.append((rel, row["target"], row["kind"]))
                    continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            checksum = hashlib.sha256(text.encode("utf-8")).hexdigest()
            line_count = len(text.splitlines())
            summary = self.store._summarize(text, path.name)
            outlines = self.store._extract_outline(project_root, text, code_language)
            role = self.store._infer_code_role(project_root, rel, code_language, outlines)
            rows.append((rel, code_language, language_tier, language_source, checksum, line_count, summary, role, size_bytes, mtime_ns))
            rows[-1] = (*rows[-1], 1)
            outline_rows.extend(
                (rel, symbol, kind, line_number, container, 1 if is_partial else 0)
                for symbol, kind, line_number, container, is_partial in outlines
            )
            edge_rows.extend((rel, target, kind) for target, kind in self.store._extract_edges(text, code_language))

        with self.store.connect(project_root) as conn:
            targets_to_replace = scoped_paths if scoped_paths is not None else seen_paths
            for rel in targets_to_replace:
                conn.execute("DELETE FROM code_outlines WHERE path = ?", (rel,))
                conn.execute("DELETE FROM code_edges WHERE source_path = ?", (rel,))
            outline_rows = list(dict.fromkeys(outline_rows))
            edge_rows = list(dict.fromkeys(edge_rows))
            conn.executemany(
                "INSERT OR REPLACE INTO code_files (path, language, language_tier, language_source, checksum, line_count, summary, role, size_bytes, mtime_ns, parsed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.executemany(
                "INSERT INTO code_outlines (path, symbol, kind, line_number, container, is_partial) VALUES (?, ?, ?, ?, ?, ?)",
                outline_rows,
            )
            conn.executemany(
                "INSERT INTO code_edges (source_path, target, kind) VALUES (?, ?, ?)",
                edge_rows,
            )
        return len(rows)

    def sync_session_code(self, project_root: Path, session_id: str, include_tests: bool = False) -> int:
        if self.store.session_store is None:
            raise RuntimeError("SessionStore is required for session-guided code sync")
        paths = self.store.session_store.session_code_targets(project_root, session_id)
        return self.store.sync_code_files(project_root, paths=paths, include_tests=include_tests)

    def code_status(self, project_root: Path) -> dict[str, object]:
        self.store.init_db(project_root)
        with self.store.connect(project_root) as conn:
            code_count = conn.execute("SELECT COUNT(*) FROM code_files").fetchone()[0]
            parsed_count = conn.execute("SELECT COUNT(*) FROM code_files WHERE parsed = 1").fetchone()[0]
            outline_count = conn.execute("SELECT COUNT(*) FROM code_outlines").fetchone()[0]
            partial_count = conn.execute("SELECT COUNT(*) FROM code_outlines WHERE is_partial = 1").fetchone()[0]
            edge_count = conn.execute("SELECT COUNT(*) FROM code_edges").fetchone()[0]
            role_rows = conn.execute(
                "SELECT COALESCE(role, 'unknown') AS role, COUNT(*) AS count FROM code_files GROUP BY COALESCE(role, 'unknown') ORDER BY count DESC, role ASC"
            ).fetchall()
            tier_rows = conn.execute(
                "SELECT COALESCE(language_tier, 'unknown') AS tier, COUNT(*) AS count FROM code_files GROUP BY COALESCE(language_tier, 'unknown') ORDER BY count DESC, tier ASC"
            ).fetchall()
            source_rows = conn.execute(
                "SELECT COALESCE(language_source, 'unknown') AS source, COUNT(*) AS count FROM code_files GROUP BY COALESCE(language_source, 'unknown') ORDER BY count DESC, source ASC"
            ).fetchall()
        roles = {row["role"]: int(row["count"]) for row in role_rows}
        tiers = {row["tier"]: int(row["count"]) for row in tier_rows}
        sources = {row["source"]: int(row["count"]) for row in source_rows}
        role_groups: dict[str, int] = {}
        for role, count in roles.items():
            group = self.store._role_group(role)
            role_groups[group] = role_groups.get(group, 0) + count
        return {
            "db_path": str(self.store.db_path(project_root)),
            "code_files": int(code_count),
            "parsed_code_files": int(parsed_count),
            "code_outlines": int(outline_count),
            "partial_symbols": int(partial_count),
            "code_edges": int(edge_count),
            "roles": roles,
            "role_groups": role_groups,
            "language_tiers": tiers,
            "language_sources": sources,
            "freshness": self._code_freshness(project_root),
        }

    def _code_freshness(self, project_root: Path) -> dict[str, object]:
        indexed_rows: dict[str, sqlite3.Row] = {}
        with self.store.connect(project_root) as conn:
            for row in conn.execute("SELECT path, checksum, mtime_ns, parsed FROM code_files ORDER BY path"):
                indexed_rows[str(row["path"])] = row

            include_tests = any(self.store._path_looks_like_test(path) for path in indexed_rows)
        tracked_paths: dict[str, dict[str, int | str]] = {}
        for path in self.store._walk_source_files(project_root, include_tests=include_tests):
            if not path.is_file():
                continue
            rel = path.relative_to(project_root).as_posix()
            code_language = self.store._language_for(path, project_root=project_root)
            if code_language is None:
                continue
            stat = path.stat()
            checksum = hashlib.sha256(path.read_text(encoding="utf-8", errors="ignore").encode("utf-8")).hexdigest()
            tracked_paths[rel] = {
                "mtime_ns": int(stat.st_mtime_ns),
                "checksum": checksum,
            }

        indexed_path_set = set(indexed_rows)
        tracked_path_set = set(tracked_paths)
        missing_paths = sorted(tracked_path_set - indexed_path_set)
        extra_paths = sorted(indexed_path_set - tracked_path_set)
        drifted_paths = sorted(
            path
            for path in tracked_path_set & indexed_path_set
            if str(indexed_rows[path]["checksum"] or "") != str(tracked_paths[path]["checksum"])
        )
        unparsed_paths = sorted(path for path, row in indexed_rows.items() if int(row["parsed"] or 0) != 1)
        reasons: list[str] = []
        if missing_paths or extra_paths:
            reasons.append("path_drift")
        if drifted_paths:
            reasons.append("content_drift")
        if unparsed_paths or (tracked_paths and not indexed_rows):
            reasons.append("missing_index_state")
        if tracked_paths and not indexed_rows:
            state = "missing"
        elif reasons:
            state = "stale"
        else:
            state = "ready"
        latest_source_mtime_ns = max((int(meta["mtime_ns"]) for meta in tracked_paths.values()), default=None)
        latest_indexed_mtime_ns = max((int(row["mtime_ns"] or 0) for row in indexed_rows.values()), default=None)
        return {
            "state": state,
            "reasons": reasons,
            "tracked_paths": len(tracked_paths),
            "indexed_paths": len(indexed_rows),
            "drifted_paths": sorted(dict.fromkeys(missing_paths + drifted_paths + extra_paths)),
            "missing_paths": missing_paths,
            "extra_paths": extra_paths,
            "unparsed_paths": unparsed_paths,
            "latest_source_mtime_ns": latest_source_mtime_ns,
            "latest_indexed_mtime_ns": latest_indexed_mtime_ns,
        }
