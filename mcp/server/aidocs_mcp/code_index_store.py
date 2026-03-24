from __future__ import annotations

import ast
import hashlib
import re
import sqlite3
from pathlib import Path

from .frontend_ast import FrontendAstExtractor
from .session_store import SessionStore


class CodeIndexStore:
    """Derived SQLite index for repository code files and lightweight summaries."""

    INDEX_VERSION = "code-index-v2"

    def __init__(self, session_store: SessionStore | None = None) -> None:
        self.session_store = session_store
        self.frontend_ast = FrontendAstExtractor()
        self._indexing_hint_cache: dict[str, dict[str, list[str]]] = {}

    def index_root(self, project_root: Path) -> Path:
        return project_root / ".MEMORY" / ".index"

    def db_path(self, project_root: Path) -> Path:
        return self.index_root(project_root) / "aidocs.sqlite3"

    def connect(self, project_root: Path) -> sqlite3.Connection:
        db_path = self.db_path(project_root)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self, project_root: Path) -> None:
        with self.connect(project_root) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS index_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS code_files (
                    path TEXT PRIMARY KEY,
                    language TEXT,
                    checksum TEXT NOT NULL,
                    line_count INTEGER NOT NULL,
                    summary TEXT NOT NULL,
                    role TEXT,
                    size_bytes INTEGER,
                    mtime_ns INTEGER,
                    parsed INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS code_outlines (
                    path TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    line_number INTEGER NOT NULL,
                    container TEXT,
                    is_partial INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (path, symbol, kind, line_number)
                );

                CREATE TABLE IF NOT EXISTS code_edges (
                    source_path TEXT NOT NULL,
                    target TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    PRIMARY KEY (source_path, target, kind)
                );

                """
            )
            self._ensure_column(conn, "code_files", "role", "TEXT")
            self._ensure_column(conn, "code_files", "size_bytes", "INTEGER")
            self._ensure_column(conn, "code_files", "mtime_ns", "INTEGER")
            self._ensure_column(conn, "code_files", "parsed", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "code_outlines", "container", "TEXT")
            self._ensure_column(conn, "code_outlines", "is_partial", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_index_version(conn)

    def _ensure_index_version(self, conn: sqlite3.Connection) -> None:
        row = conn.execute("SELECT value FROM index_meta WHERE key = 'code_index_version'").fetchone()
        current = row["value"] if row else None
        if current == self.INDEX_VERSION:
            return

        conn.execute("DELETE FROM code_files")
        conn.execute("DELETE FROM code_outlines")
        conn.execute("DELETE FROM code_edges")
        conn.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES ('code_index_version', ?)",
            (self.INDEX_VERSION,),
        )

    def sync_code_manifest(self, project_root: Path, include_tests: bool = False) -> int:
        self.init_db(project_root)
        manifest_rows: list[tuple[str, str, int, int]] = []
        seen_paths: set[str] = set()

        for path in sorted(project_root.rglob("*")):
            if not path.is_file():
                continue
            if self._should_skip(project_root, path, include_tests=include_tests):
                continue
            rel = path.relative_to(project_root).as_posix()
            language = self._language_for(path)
            if language is None:
                continue
            stat = path.stat()
            seen_paths.add(rel)
            role = self._infer_code_role(project_root, rel, language, [])
            manifest_rows.append((rel, language, role, int(stat.st_size), int(stat.st_mtime_ns)))

        with self.connect(project_root) as conn:
            existing_paths = {row["path"] for row in conn.execute("SELECT path FROM code_files")}
            stale_paths = existing_paths - seen_paths
            for stale in stale_paths:
                conn.execute("DELETE FROM code_files WHERE path = ?", (stale,))
                conn.execute("DELETE FROM code_outlines WHERE path = ?", (stale,))
                conn.execute("DELETE FROM code_edges WHERE source_path = ?", (stale,))

            for rel, language, role, size_bytes, mtime_ns in manifest_rows:
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
                    INSERT INTO code_files (path, language, checksum, line_count, summary, role, size_bytes, mtime_ns, parsed)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                      language=excluded.language,
                      role=excluded.role,
                      size_bytes=excluded.size_bytes,
                      mtime_ns=excluded.mtime_ns,
                      parsed=CASE
                        WHEN code_files.size_bytes = excluded.size_bytes AND code_files.mtime_ns = excluded.mtime_ns THEN code_files.parsed
                        ELSE 0
                      END
                    """,
                    (rel, language, "", 0, "", role, size_bytes, mtime_ns, parsed),
                )
        return len(manifest_rows)

    def sync_code_files(
        self,
        project_root: Path,
        paths: list[str] | None = None,
        include_tests: bool = False,
    ) -> int:
        self.init_db(project_root)
        self.sync_code_manifest(project_root, include_tests=include_tests)
        rows: list[tuple[str, str, str, int, str, str | None, int, int, int]] = []
        outline_rows: list[tuple[str, str, str, int, str | None, int]] = []
        edge_rows: list[tuple[str, str, str]] = []

        scoped_paths = None
        if paths is not None:
            scoped_paths = {item.replace("\\", "/") for item in paths if str(item).strip()}

        existing_meta = {}
        with self.connect(project_root) as conn:
            for row in conn.execute("SELECT path, checksum, size_bytes, mtime_ns, language, line_count, summary, role, parsed FROM code_files"):
                existing_meta[row["path"]] = dict(row)

        seen_paths: set[str] = set()
        for path in sorted(project_root.rglob("*")):
            if not path.is_file():
                continue
            if self._should_skip(project_root, path, include_tests=include_tests):
                continue
            rel = path.relative_to(project_root).as_posix()
            if scoped_paths is not None and rel not in scoped_paths:
                continue
            seen_paths.add(rel)
            language = self._language_for(path)
            if language is None:
                continue
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
                            str(existing["checksum"]),
                            int(existing["line_count"]),
                            str(existing["summary"]),
                            existing.get("role"),
                            size_bytes,
                            mtime_ns,
                            1,
                        )
                    )
                    with self.connect(project_root) as conn:
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
            summary = self._summarize(text, path.name)
            outlines = self._extract_outline(text, language)
            role = self._infer_code_role(project_root, rel, language, outlines)
            rows.append((rel, language, checksum, line_count, summary, role, size_bytes, mtime_ns))
            rows[-1] = (*rows[-1], 1)
            outline_rows.extend(
                (rel, symbol, kind, line_number, container, 1 if is_partial else 0)
                for symbol, kind, line_number, container, is_partial in outlines
            )
            edge_rows.extend((rel, target, kind) for target, kind in self._extract_edges(text, language))

        with self.connect(project_root) as conn:
            targets_to_replace = scoped_paths if scoped_paths is not None else seen_paths
            for rel in targets_to_replace:
                conn.execute("DELETE FROM code_outlines WHERE path = ?", (rel,))
                conn.execute("DELETE FROM code_edges WHERE source_path = ?", (rel,))
            outline_rows = list(dict.fromkeys(outline_rows))
            edge_rows = list(dict.fromkeys(edge_rows))
            conn.executemany(
                "INSERT OR REPLACE INTO code_files (path, language, checksum, line_count, summary, role, size_bytes, mtime_ns, parsed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
        if self.session_store is None:
            raise RuntimeError("SessionStore is required for session-guided code sync")
        paths = self.session_store.session_code_targets(project_root, session_id)
        return self.sync_code_files(project_root, paths=paths, include_tests=include_tests)

    def code_status(self, project_root: Path) -> dict[str, int | str]:
        self.init_db(project_root)
        with self.connect(project_root) as conn:
            code_count = conn.execute("SELECT COUNT(*) FROM code_files").fetchone()[0]
            parsed_count = conn.execute("SELECT COUNT(*) FROM code_files WHERE parsed = 1").fetchone()[0]
            outline_count = conn.execute("SELECT COUNT(*) FROM code_outlines").fetchone()[0]
            partial_count = conn.execute("SELECT COUNT(*) FROM code_outlines WHERE is_partial = 1").fetchone()[0]
            edge_count = conn.execute("SELECT COUNT(*) FROM code_edges").fetchone()[0]
            role_rows = conn.execute(
                "SELECT COALESCE(role, 'unknown') AS role, COUNT(*) AS count FROM code_files GROUP BY COALESCE(role, 'unknown') ORDER BY count DESC, role ASC"
            ).fetchall()
        roles = {row["role"]: int(row["count"]) for row in role_rows}
        role_groups: dict[str, int] = {}
        for role, count in roles.items():
            group = self._role_group(role)
            role_groups[group] = role_groups.get(group, 0) + count
        return {
            "db_path": str(self.db_path(project_root)),
            "code_files": int(code_count),
            "parsed_code_files": int(parsed_count),
            "code_outlines": int(outline_count),
            "partial_symbols": int(partial_count),
            "code_edges": int(edge_count),
            "roles": roles,
            "role_groups": role_groups,
        }

    def search_code(self, project_root: Path, query: str, limit: int = 10) -> list[dict[str, str | int]]:
        self.init_db(project_root)
        needle = query.strip()
        if not needle:
            return []
        pattern = f"%{needle}%"
        with self.connect(project_root) as conn:
            rows = conn.execute(
                """
                SELECT path, language, line_count, summary, role
                FROM code_files
                WHERE path LIKE ? OR summary LIKE ?
                LIMIT 250
                """,
                (pattern, pattern),
            ).fetchall()
        ranked = []
        for row in rows:
            score = 0
            reasons: list[str] = []
            score += self._score_text_match(needle, row["path"], exact=120, prefix=90, contains=60, reasons=reasons, label="path")
            score += self._score_text_match(needle, row["summary"], exact=40, prefix=25, contains=15, reasons=reasons, label="summary")
            path_weight = self._path_weight(project_root, str(row["path"]))
            score += path_weight
            if path_weight:
                reasons.append(f"path_weight:{path_weight}")
            score -= row["path"].count("/")
            ranked.append((score, row, reasons))
        ranked.sort(key=lambda item: (-item[0], item[1]["path"]))
        return [
            {
                "path": row["path"],
                "language": row["language"],
                "line_count": int(row["line_count"]),
                "summary": row["summary"],
                "role": row["role"] or "unknown",
                "why": reasons,
            }
            for _, row, reasons in ranked[:limit]
        ]

    def search_symbols(self, project_root: Path, query: str, limit: int = 25) -> list[dict[str, str | int | bool | None]]:
        self.init_db(project_root)
        needle = query.strip()
        if not needle:
            return []
        self._ensure_parsed_candidates(project_root, needle, limit=limit * 4)
        variants = self._concept_variants(needle)
        clauses = " OR ".join(["symbol LIKE ? OR COALESCE(container, '') LIKE ?" for _ in variants])
        params = []
        for variant in variants:
            pattern = f"%{variant}%"
            params.extend([pattern, pattern])
        with self.connect(project_root) as conn:
            rows = conn.execute(
                f"""
                SELECT path, symbol, kind, line_number, container, is_partial
                FROM code_outlines
                WHERE {clauses}
                LIMIT 500
                """,
                params,
            ).fetchall()
        ranked = []
        kind_weight = {
            "class": 30,
            "record": 28,
            "struct": 26,
            "interface": 24,
            "enum": 22,
            "function": 20,
            "component": 20,
            "hook": 18,
            "initializer": 18,
            "method": 14,
            "property": 12,
            "field": 10,
            "enum_member": 8,
        }
        for row in rows:
            score = 0
            reasons: list[str] = []
            score += self._score_text_match(needle, row["symbol"], exact=140, prefix=100, contains=70, reasons=reasons, label="symbol")
            score += self._score_text_match(needle, row["container"] or "", exact=35, prefix=20, contains=10, reasons=reasons, label="container")
            score += kind_weight.get(row["kind"], 0)
            if kind_weight.get(row["kind"], 0):
                reasons.append(f"kind_weight:{kind_weight.get(row['kind'], 0)}")
            score += 5 if row["is_partial"] else 0
            if row["is_partial"]:
                reasons.append("partial_bonus:5")
            path_weight = self._path_weight(project_root, str(row["path"]))
            score += path_weight
            if path_weight:
                reasons.append(f"path_weight:{path_weight}")
            score -= row["path"].count("/")
            ranked.append((score, row, reasons))
        ranked.sort(key=lambda item: (-item[0], item[1]["path"], int(item[1]["line_number"])))
        return [
            {
                "path": row["path"],
                "symbol": row["symbol"],
                "kind": row["kind"],
                "line_number": int(row["line_number"]),
                "container": row["container"],
                "is_partial": bool(row["is_partial"]),
                "why": reasons,
            }
            for _, row, reasons in ranked[:limit]
        ]

    def find_references(self, project_root: Path, symbol: str, limit: int = 100) -> dict[str, object]:
        self.init_db(project_root)
        needle = symbol.strip()
        if not needle:
            return {"symbol": symbol, "matches": []}

        pattern = re.compile(rf"\b{re.escape(needle)}\b")
        matches: list[dict[str, object]] = []

        with self.connect(project_root) as conn:
            rows = conn.execute("SELECT path, language FROM code_files ORDER BY path").fetchall()

        for row in rows:
            path = str(row["path"])
            abs_path = project_root / path
            text = abs_path.read_text(encoding="utf-8", errors="ignore")
            for line_number, line in enumerate(text.splitlines(), start=1):
                if not pattern.search(line):
                    continue
                matches.append(
                    {
                        "path": path,
                        "language": row["language"],
                        "line_number": line_number,
                        "line": line.strip(),
                        "layer": self._infer_layer_from_path(path),
                    }
                )

        ranked = []
        lower_symbol = needle.lower()
        for item in matches:
            score = 0
            line_lower = str(item["line"]).lower()
            path_lower = str(item["path"]).lower()
            if re.search(rf"\b{re.escape(lower_symbol)}\b", line_lower):
                score += 120
            if path_lower.endswith(f"{lower_symbol.lower()}.cs") or path_lower.endswith(f"{lower_symbol.lower()}.ts") or path_lower.endswith(f"{lower_symbol.lower()}.tsx"):
                score += 40
            score -= str(item["path"]).count("/")
            ranked.append((score, item))

        ranked.sort(key=lambda pair: (-pair[0], self._layer_rank(str(pair[1]["layer"])), str(pair[1]["path"]), int(pair[1]["line_number"])))
        return {
            "symbol": symbol,
            "matches": [item for _, item in ranked[:limit]],
        }

    def trace_field_flow(self, project_root: Path, field_name: str, limit: int = 50) -> dict[str, object]:
        self.init_db(project_root)
        needle = field_name.strip()
        if not needle:
            return {"field": field_name, "matches": []}
        self._ensure_parsed_candidates(project_root, needle, limit=limit * 4)
        variants = self._concept_variants(needle)

        symbol_matches = self.search_symbols(project_root, query=needle, limit=limit)
        code_matches = self.search_code(project_root, query=needle, limit=limit)
        schema_fields = []
        try:
            from .schema_index_store import SchemaIndexStore

            schema_fields = SchemaIndexStore().find_schema_field(project_root, needle, limit=limit)
        except Exception:
            schema_fields = []

        merged: list[dict[str, object]] = []
        seen: set[tuple[str, str, int | None]] = set()

        for item in symbol_matches:
            path = str(item["path"])
            snippet = None
            try:
                snippet = self.get_symbol_snippet(
                    project_root,
                    path=path,
                    symbol=str(item["symbol"]),
                    kind=str(item["kind"]),
                    line_number=int(item["line_number"]),
                )
            except FileNotFoundError:
                snippet = None

            layer = self._infer_layer_from_path(path)
            entry = {
                "path": path,
                "layer": layer,
                "symbol": item["symbol"],
                "kind": item["kind"],
                "line_number": item["line_number"],
                "container": item["container"],
                "snippet": snippet["snippet"] if snippet else None,
            }
            key = (path, str(item["symbol"]), int(item["line_number"]))
            if key not in seen:
                seen.add(key)
                merged.append(entry)

        for item in code_matches:
            path = str(item["path"])
            key = (path, "", None)
            if key in seen:
                continue
            lower_path = path.lower()
            lower_summary = str(item["summary"] or "").lower()
            if not any(v.lower() in lower_path or v.lower() in lower_summary for v in variants):
                continue
            seen.add(key)
            merged.append(
                {
                    "path": path,
                    "layer": self._infer_layer_from_path(path),
                    "symbol": None,
                    "kind": "file_match",
                    "line_number": None,
                    "container": None,
                    "snippet": item["summary"],
                }
            )

        for item in schema_fields:
            path = str(item["path"])
            line_number = int(item["line_number"]) if item["line_number"] is not None else None
            key = (path, str(item["field_name"]), line_number)
            if key in seen:
                continue
            if not any(v.lower() in str(item["field_name"]).lower() for v in variants):
                continue
            seen.add(key)
            merged.append(
                {
                    "path": path,
                    "layer": self._infer_layer_from_path(path),
                    "symbol": item["field_name"],
                    "kind": item["field_kind"],
                    "line_number": line_number,
                    "container": item["entity_name"],
                    "snippet": item["field_type"],
                    "source": "schema",
                }
            )

        merged.sort(key=lambda item: (self._layer_rank(str(item["layer"])), str(item["path"]), item["line_number"] or 0))
        return {
            "field": field_name,
            "matches": merged[:limit],
            "confidence": self._trace_confidence(merged),
            "why": self._trace_summary(merged),
        }

    def trace_setting_usage(self, project_root: Path, setting_name: str, limit: int = 50) -> dict[str, object]:
        self.init_db(project_root)
        needle = setting_name.strip()
        if not needle:
            return {"setting": setting_name, "matches": []}
        self._ensure_parsed_candidates(project_root, needle, limit=limit * 4)
        variants = self._concept_variants(needle)

        symbol_matches = self.search_symbols(project_root, query=needle, limit=limit)
        code_matches = self.search_code(project_root, query=needle, limit=limit)
        lower = needle.lower()

        merged: list[dict[str, object]] = []
        seen: set[tuple[str, str | None, int | None]] = set()

        setting_tokens = ("setting", "config", "option", "preference", "feature", "toggle")

        for item in symbol_matches:
            path = str(item["path"])
            symbol = str(item["symbol"])
            kind = str(item["kind"])
            lower_symbol = symbol.lower()

            score = self._score_text_match(needle, symbol, exact=130, prefix=95, contains=65)
            if any(token in lower_symbol for token in setting_tokens):
                score += 35
            if lower_symbol.startswith("is") or lower_symbol.startswith("has"):
                score += 10
            layer = self._infer_layer_from_path(path)
            if layer == "data":
                score += 18
            elif layer == "logic":
                score += 24
            elif layer == "api":
                score += 16
            elif layer == "ui":
                score += 12

            key = (path, symbol, int(item["line_number"]))
            if key in seen:
                continue
            seen.add(key)

            snippet = None
            try:
                snippet = self.get_symbol_snippet(
                    project_root,
                    path=path,
                    symbol=symbol,
                    kind=kind,
                    line_number=int(item["line_number"]),
                )
            except FileNotFoundError:
                snippet = None

            merged.append(
                {
                    "score": score,
                    "path": path,
                    "layer": layer,
                    "symbol": symbol,
                    "kind": kind,
                    "line_number": item["line_number"],
                    "container": item["container"],
                    "snippet": snippet["snippet"] if snippet else None,
                }
            )

        for item in code_matches:
            path = str(item["path"])
            key = (path, None, None)
            if key in seen:
                continue
            lower_path = path.lower()
            lower_summary = str(item["summary"] or "").lower()
            if not any(v.lower() in lower_path or v.lower() in lower_summary for v in variants):
                continue
            score = self._score_text_match(needle, path, exact=50, prefix=30, contains=20)
            if any(token in lower_path for token in setting_tokens):
                score += 20
            if score <= 0:
                continue
            seen.add(key)
            merged.append(
                {
                    "score": score,
                    "path": path,
                    "layer": self._infer_layer_from_path(path),
                    "symbol": None,
                    "kind": "file_match",
                    "line_number": None,
                    "container": None,
                    "snippet": item["summary"],
                }
            )

        merged.sort(key=lambda item: (-int(item["score"]), self._layer_rank(str(item["layer"])), str(item["path"]), item["line_number"] or 0))
        return {
            "setting": setting_name,
            "matches": merged[:limit],
            "confidence": self._trace_confidence(merged),
            "why": self._trace_summary(merged),
        }

    def trace_service_usage(self, project_root: Path, service_name: str, limit: int = 50) -> dict[str, object]:
        self.init_db(project_root)
        needle = service_name.strip()
        if not needle:
            return {"service": service_name, "matches": []}

        self._ensure_parsed_candidates(project_root, needle, limit=limit * 4)
        variants = self._concept_variants(needle)
        symbol_matches = self.search_symbols(project_root, query=needle, limit=limit)
        references = self.find_references(project_root, symbol=needle, limit=limit)["matches"]
        code_matches = self.search_code(project_root, query=needle, limit=limit)

        merged: list[dict[str, object]] = []
        seen: set[tuple[str, str | None, int | None]] = set()

        for item in symbol_matches:
            path = str(item["path"])
            symbol = str(item["symbol"])
            kind = str(item["kind"])
            lower_symbol = symbol.lower()
            score = self._score_text_match(needle, symbol, exact=140, prefix=100, contains=70)
            if lower_symbol.endswith("service"):
                score += 35
            layer = self._infer_layer_from_path(path)
            if layer == "logic":
                score += 25
            elif layer == "api":
                score += 15
            score += self._path_weight(project_root, path)
            key = (path, symbol, int(item["line_number"]))
            if key in seen:
                continue
            seen.add(key)
            merged.append(
                {
                    "score": score,
                    "source": "definition",
                    "path": path,
                    "layer": layer,
                    "symbol": symbol,
                    "kind": kind,
                    "line_number": item["line_number"],
                    "container": item["container"],
                }
            )

        for item in references:
            path = str(item["path"])
            key = (path, needle, int(item["line_number"]))
            if key in seen:
                continue
            if not any(v.lower() in str(item["line"]).lower() for v in variants):
                continue
            seen.add(key)
            merged.append(
                {
                    "score": 90 + self._path_weight(project_root, path),
                    "source": "reference",
                    "path": path,
                    "layer": item["layer"],
                    "symbol": needle,
                    "kind": "reference",
                    "line_number": item["line_number"],
                    "container": None,
                    "snippet": item["line"],
                }
            )

        for item in code_matches:
            path = str(item["path"])
            key = (path, None, None)
            if key in seen:
                continue
            lower_path = path.lower()
            lower_summary = str(item["summary"] or "").lower()
            if not any(v.lower() in lower_path or v.lower() in lower_summary for v in variants):
                continue
            seen.add(key)
            merged.append(
                {
                    "score": 40 + self._path_weight(project_root, path),
                    "source": "file_match",
                    "path": path,
                    "layer": self._infer_layer_from_path(path),
                    "symbol": None,
                    "kind": "file_match",
                    "line_number": None,
                    "container": None,
                    "snippet": item["summary"],
                }
            )

        merged.sort(key=lambda item: (-int(item["score"]), self._layer_rank(str(item["layer"])), str(item["path"]), item["line_number"] or 0))
        return {
            "service": service_name,
            "matches": merged[:limit],
            "confidence": self._trace_confidence(merged),
            "why": self._trace_summary(merged),
        }

    def trace_model_usage(self, project_root: Path, model_name: str, limit: int = 50) -> dict[str, object]:
        self.init_db(project_root)
        needle = model_name.strip()
        if not needle:
            return {"model": model_name, "definitions": [], "schema": {}, "references": []}

        self._ensure_parsed_candidates(project_root, needle, limit=limit * 4)
        variants = self._concept_variants(needle)
        symbol_bundle = self.get_symbol_bundle(project_root, symbol=needle, limit=limit)

        schema = {}
        try:
            from .schema_index_store import SchemaIndexStore

            schema_store = SchemaIndexStore()
            schema = schema_store.trace_entity_flow(project_root, entity_name=needle, limit=limit)
            if not schema.get("entities") and not schema.get("fields"):
                for variant in variants:
                    schema = schema_store.trace_entity_flow(project_root, entity_name=variant, limit=limit)
                    if schema.get("entities") or schema.get("fields"):
                        break
        except Exception:
            schema = {}

        match_count = len(symbol_bundle.get("definitions", [])) + len(symbol_bundle.get("references", [])) + len(schema.get("entities", [])) + len(schema.get("fields", []))
        return {
            "model": model_name,
            "definitions": symbol_bundle.get("definitions", []),
            "references": symbol_bundle.get("references", []),
            "schema": schema,
            "confidence": "high" if match_count >= 4 else "medium" if match_count >= 2 else "low",
            "why": [
                f"definitions:{len(symbol_bundle.get('definitions', []))}",
                f"references:{len(symbol_bundle.get('references', []))}",
                f"schema_entities:{len(schema.get('entities', []))}",
                f"schema_fields:{len(schema.get('fields', []))}",
            ],
        }

    def find_mutation_points(self, project_root: Path, concept: str, limit: int = 50) -> dict[str, object]:
        self.init_db(project_root)
        needle = concept.strip()
        if not needle:
            return {"concept": concept, "matches": []}

        self._ensure_parsed_candidates(project_root, needle, limit=limit * 4)
        symbol_matches = self.search_symbols(project_root, query=needle, limit=limit)
        references = self.find_references(project_root, symbol=needle, limit=limit)["matches"]
        code_matches = self.search_code(project_root, query=needle, limit=limit)

        mutation_tokens = ("set", "update", "save", "create", "delete", "remove", "toggle", "apply", "sync", "write", "assign")
        merged: list[dict[str, object]] = []
        seen: set[tuple[str, str | None, int | None]] = set()

        for item in symbol_matches:
            path = str(item["path"])
            symbol = str(item["symbol"])
            lower_symbol = symbol.lower()
            score = self._score_text_match(needle, symbol, exact=90, prefix=60, contains=35)
            token_bonus = 0
            for token in mutation_tokens:
                if token in lower_symbol:
                    token_bonus += 25
            if token_bonus == 0:
                continue
            score += token_bonus
            layer = self._infer_layer_from_path(path)
            score += self._path_weight(project_root, path)
            if layer in {"logic", "api", "ui"}:
                score += 10
            key = (path, symbol, int(item["line_number"]))
            if key in seen:
                continue
            seen.add(key)
            snippet = None
            try:
                snippet = self.get_symbol_snippet(project_root, path=path, symbol=symbol, kind=str(item["kind"]), line_number=int(item["line_number"]))
            except FileNotFoundError:
                snippet = None
            merged.append(
                {
                    "score": score,
                    "source": "symbol",
                    "path": path,
                    "layer": layer,
                    "symbol": symbol,
                    "kind": item["kind"],
                    "line_number": item["line_number"],
                    "container": item["container"],
                    "snippet": snippet["snippet"] if snippet else None,
                }
            )

        line_pattern = re.compile(rf"\b{re.escape(needle)}\b", re.IGNORECASE)
        for item in references:
            path = str(item["path"])
            line = str(item["line"])
            lower_line = line.lower()
            token_bonus = 0
            for token in mutation_tokens:
                if token in lower_line:
                    token_bonus += 18
            if token_bonus == 0 or not line_pattern.search(line):
                continue
            key = (path, None, int(item["line_number"]))
            if key in seen:
                continue
            seen.add(key)
            merged.append(
                {
                    "score": 70 + token_bonus + self._path_weight(project_root, path),
                    "source": "reference",
                    "path": path,
                    "layer": item["layer"],
                    "symbol": None,
                    "kind": "reference",
                    "line_number": item["line_number"],
                    "container": None,
                    "snippet": line,
                }
            )

        for item in code_matches:
            path = str(item["path"])
            lower_path = path.lower()
            if not any(token in lower_path for token in mutation_tokens):
                continue
            key = (path, None, None)
            if key in seen:
                continue
            seen.add(key)
            merged.append(
                {
                    "score": 35 + self._path_weight(project_root, path),
                    "source": "file_match",
                    "path": path,
                    "layer": self._infer_layer_from_path(path),
                    "symbol": None,
                    "kind": "file_match",
                    "line_number": None,
                    "container": None,
                    "snippet": item["summary"],
                }
            )

        merged.sort(key=lambda item: (-int(item["score"]), self._layer_rank(str(item["layer"])), str(item["path"]), item["line_number"] or 0))
        limited = merged[:limit]
        return {"concept": concept, "matches": limited, "confidence": self._trace_confidence(limited), "why": self._trace_summary(limited)}

    def find_validation_surfaces(self, project_root: Path, concept: str, limit: int = 50) -> dict[str, object]:
        self.init_db(project_root)
        needle = concept.strip()
        if not needle:
            return {"concept": concept, "matches": []}

        self._ensure_parsed_candidates(project_root, needle, limit=limit * 4)
        symbol_matches = self.search_symbols(project_root, query=needle, limit=limit)
        code_matches = self.search_code(project_root, query=needle, limit=limit)

        validation_tokens = ("validate", "validator", "validation", "required", "rule", "rules", "invalid", "error")
        merged: list[dict[str, object]] = []
        seen: set[tuple[str, str | None, int | None]] = set()

        for item in symbol_matches:
            path = str(item["path"])
            symbol = str(item["symbol"])
            lower_symbol = symbol.lower()
            lower_path = path.lower()
            score = self._score_text_match(needle, symbol, exact=100, prefix=70, contains=40)
            token_bonus = 0
            for token in validation_tokens:
                if token in lower_symbol:
                    token_bonus += 25
                if token in lower_path:
                    token_bonus += 15
            if token_bonus == 0 and score <= 0:
                continue
            score += token_bonus
            layer = self._infer_layer_from_path(path)
            if layer in {"logic", "api", "ui", "data"}:
                score += 10
            score += self._path_weight(project_root, path)
            key = (path, symbol, int(item["line_number"]))
            if key in seen:
                continue
            seen.add(key)
            snippet = None
            try:
                snippet = self.get_symbol_snippet(project_root, path=path, symbol=symbol, kind=str(item["kind"]), line_number=int(item["line_number"]))
            except FileNotFoundError:
                snippet = None
            merged.append(
                {
                    "score": score,
                    "path": path,
                    "layer": layer,
                    "symbol": symbol,
                    "kind": item["kind"],
                    "line_number": item["line_number"],
                    "container": item["container"],
                    "snippet": snippet["snippet"] if snippet else None,
                }
            )

        for item in code_matches:
            path = str(item["path"])
            lower_path = path.lower()
            score = self._score_text_match(needle, path, exact=50, prefix=30, contains=20)
            token_bonus = 0
            for token in validation_tokens:
                if token in lower_path or token in str(item["summary"]).lower():
                    token_bonus += 15
            if token_bonus == 0 and score <= 0:
                continue
            key = (path, None, None)
            if key in seen:
                continue
            seen.add(key)
            merged.append(
                {
                    "score": score + token_bonus + self._path_weight(project_root, path),
                    "path": path,
                    "layer": self._infer_layer_from_path(path),
                    "symbol": None,
                    "kind": "file_match",
                    "line_number": None,
                    "container": None,
                    "snippet": item["summary"],
                }
            )

        merged.sort(key=lambda item: (-int(item["score"]), self._layer_rank(str(item["layer"])), str(item["path"]), item["line_number"] or 0))
        limited = merged[:limit]
        return {"concept": concept, "matches": limited, "confidence": self._trace_confidence(limited), "why": self._trace_summary(limited)}

    def find_async_boundaries(self, project_root: Path, concept: str | None = None, limit: int = 50) -> dict[str, object]:
        self.init_db(project_root)
        needle = (concept or "").strip()
        if needle:
            self._ensure_parsed_candidates(project_root, needle, limit=limit * 4)

        symbol_matches = self.search_symbols(project_root, needle or "async", limit=max(limit * 2, 100)) if needle else self.search_symbols(project_root, "async", limit=max(limit * 2, 100))
        code_matches = self.search_code(project_root, needle or "task", limit=max(limit * 2, 100)) if needle else self.search_code(project_root, "task", limit=max(limit * 2, 100))

        async_tokens = ("async", "await", "task", "promise", "deferred", "background", "queue", "schedule", "settimeout", "setinterval")
        merged: list[dict[str, object]] = []
        seen: set[tuple[str, str | None, int | None]] = set()

        for item in symbol_matches:
            path = str(item["path"])
            symbol = str(item["symbol"])
            lower_symbol = symbol.lower()
            lower_path = path.lower()
            score = 0
            if needle:
                score += self._score_text_match(needle, symbol, exact=90, prefix=60, contains=35)
                score += self._score_text_match(needle, path, exact=40, prefix=25, contains=15)
            token_bonus = 0
            for token in async_tokens:
                if token in lower_symbol:
                    token_bonus += 25
                if token in lower_path:
                    token_bonus += 15
            if token_bonus == 0 and score <= 0:
                continue
            score += token_bonus
            score += self._path_weight(project_root, path)
            layer = self._infer_layer_from_path(path)
            key = (path, symbol, int(item["line_number"]))
            if key in seen:
                continue
            seen.add(key)
            snippet = None
            try:
                snippet = self.get_symbol_snippet(project_root, path=path, symbol=symbol, kind=str(item["kind"]), line_number=int(item["line_number"]))
            except FileNotFoundError:
                snippet = None
            merged.append(
                {
                    "score": score,
                    "path": path,
                    "layer": layer,
                    "symbol": symbol,
                    "kind": item["kind"],
                    "line_number": item["line_number"],
                    "container": item["container"],
                    "snippet": snippet["snippet"] if snippet else None,
                }
            )

        for item in code_matches:
            path = str(item["path"])
            lower_path = path.lower()
            score = 0
            if needle:
                score += self._score_text_match(needle, path, exact=40, prefix=25, contains=15)
            token_bonus = 0
            for token in async_tokens:
                if token in lower_path or token in str(item["summary"]).lower():
                    token_bonus += 15
            if token_bonus == 0 and score <= 0:
                continue
            key = (path, None, None)
            if key in seen:
                continue
            seen.add(key)
            merged.append(
                {
                    "score": score + token_bonus + self._path_weight(project_root, path),
                    "path": path,
                    "layer": self._infer_layer_from_path(path),
                    "symbol": None,
                    "kind": "file_match",
                    "line_number": None,
                    "container": None,
                    "snippet": item["summary"],
                }
            )

        merged.sort(key=lambda item: (-int(item["score"]), self._layer_rank(str(item["layer"])), str(item["path"]), item["line_number"] or 0))
        return {"concept": concept, "matches": merged[:limit]}

    def find_hotspots(self, project_root: Path, query: str | None = None, limit: int = 30) -> dict[str, object]:
        self.init_db(project_root)
        needle = (query or "").strip()
        if needle:
            self._ensure_parsed_candidates(project_root, needle, limit=limit * 6)

        with self.connect(project_root) as conn:
            rows = conn.execute(
                """
                SELECT cf.path, cf.language, cf.line_count, cf.summary, cf.role,
                       COUNT(DISTINCT co.symbol) AS outline_count,
                       COUNT(DISTINCT ce.target) AS dependency_count
                FROM code_files cf
                LEFT JOIN code_outlines co ON co.path = cf.path
                LEFT JOIN code_edges ce ON ce.source_path = cf.path
                GROUP BY cf.path, cf.language, cf.line_count, cf.summary, cf.role
                ORDER BY cf.path ASC
                """
            ).fetchall()

        hotspots: list[dict[str, object]] = []
        signal_tokens = ("legacy", "migration", "adapter", "compat", "validator", "validate", "policy", "permission", "async", "queue", "builder")
        for row in rows:
            path = str(row["path"])
            lower_path = path.lower()
            lower_summary = str(row["summary"] or "").lower()
            score = 0
            reasons: list[str] = []

            if needle:
                score += self._score_text_match(needle, path, exact=60, prefix=35, contains=20, reasons=reasons, label="path")
                score += self._score_text_match(needle, str(row["summary"] or ""), exact=25, prefix=15, contains=8, reasons=reasons, label="summary")

            outline_count = int(row["outline_count"] or 0)
            dependency_count = int(row["dependency_count"] or 0)
            line_count = int(row["line_count"] or 0)

            score += min(outline_count, 20) * 4
            score += min(dependency_count, 20) * 5
            score += min(line_count // 40, 20) * 2
            if outline_count:
                reasons.append(f"outline_count:{outline_count}")
            if dependency_count:
                reasons.append(f"dependency_count:{dependency_count}")
            if line_count:
                reasons.append(f"line_count:{line_count}")

            role = row["role"] or "unknown"
            role_bonus = {
                "service": 25,
                "controller": 20,
                "context-provider": 20,
                "component": 15,
                "page": 18,
                "layout": 12,
            }.get(role, 0)
            score += role_bonus
            if role_bonus:
                reasons.append(f"role:{role}")

            token_bonus = 0
            for token in signal_tokens:
                if token in lower_path or token in lower_summary:
                    token_bonus += 8
            score += token_bonus
            if token_bonus:
                reasons.append(f"signal_bonus:{token_bonus}")

            score += self._path_weight(project_root, path)
            hotspots.append(
                {
                    "path": path,
                    "language": row["language"],
                    "role": role,
                    "line_count": line_count,
                    "outline_count": outline_count,
                    "dependency_count": dependency_count,
                    "score": score,
                    "why": reasons,
                }
            )

        hotspots.sort(key=lambda item: (-int(item["score"]), str(item["path"])))
        limited = hotspots[:limit]
        return {
            "query": query,
            "matches": limited,
            "confidence": self._trace_confidence(limited),
            "why": self._trace_summary(limited),
        }

    def find_query_hotspots(self, project_root: Path, query: str | None = None, limit: int = 30) -> dict[str, object]:
        self.init_db(project_root)
        needle = (query or "").strip()
        if needle:
            self._ensure_parsed_candidates(project_root, needle, limit=limit * 4)

        with self.connect(project_root) as conn:
            rows = conn.execute(
                "SELECT path, language, line_count, summary, role FROM code_files ORDER BY path"
            ).fetchall()

        query_tokens = (
            ".include(",
            ".theninclude(",
            ".join(",
            ".groupjoin(",
            ".selectmany(",
            ".where(",
            ".select(",
            ".orderby(",
            ".groupby(",
            ".assplitquery(",
            ".asnotracking(",
            "fromsql",
            "to_list_async",
            "tolistasync",
            "query",
            "select *",
            " left join ",
            " right join ",
            " inner join ",
        )

        results: list[dict[str, object]] = []
        for row in rows:
            path = str(row["path"])
            abs_path = project_root / path
            if not abs_path.is_file():
                continue
            text = abs_path.read_text(encoding="utf-8", errors="ignore")
            lower = text.lower()
            lower_path = path.lower()
            line_count = len(text.splitlines())

            score = 0
            reasons: list[str] = []
            if needle:
                score += self._score_text_match(needle, path, exact=50, prefix=30, contains=20, reasons=reasons, label="path")
                score += self._score_text_match(needle, str(row["summary"] or ""), exact=20, prefix=10, contains=5, reasons=reasons, label="summary")

            include_count = lower.count(".include(") + lower.count(".theninclude(")
            join_count = lower.count(".join(") + lower.count(".groupjoin(") + lower.count(" left join ") + lower.count(" right join ") + lower.count(" inner join ")
            projection_count = lower.count(".select(") + lower.count(".selectmany(")
            filter_count = lower.count(".where(") + lower.count(".orderby(") + lower.count(".groupby(")
            sql_count = lower.count("fromsql") + lower.count("select *")

            if include_count:
                score += include_count * 12
                reasons.append(f"includes:{include_count}")
            if join_count:
                score += join_count * 15
                reasons.append(f"joins:{join_count}")
            if projection_count:
                score += projection_count * 8
                reasons.append(f"projections:{projection_count}")
            if filter_count:
                score += min(filter_count, 20) * 4
                reasons.append(f"filters:{filter_count}")
            if sql_count:
                score += sql_count * 18
                reasons.append(f"raw_sql:{sql_count}")

            if line_count:
                line_bonus = min(line_count // 60, 15) * 3
                score += line_bonus
                reasons.append(f"line_count:{line_count}")

            role = row["role"] or "unknown"
            role_bonus = {
                "service": 20,
                "controller": 15,
                "data-model": 10,
            }.get(role, 0)
            if role_bonus:
                score += role_bonus
                reasons.append(f"role:{role}")

            path_weight = self._path_weight(project_root, path)
            score += path_weight
            if path_weight:
                reasons.append(f"path_weight:{path_weight}")

            if row["language"] == "javascript" and role == "unknown" and any(token in lower_path for token in ("/assets/", "/pwaassets/", "/vendor/", "/vendors/")):
                score -= 250
                reasons.append("third_party_asset_penalty:250")

            if score <= 0:
                continue

            results.append(
                {
                    "path": path,
                    "language": row["language"],
                    "role": role,
                    "line_count": line_count,
                    "score": score,
                    "why": reasons,
                }
            )

        results.sort(key=lambda item: (-int(item["score"]), str(item["path"])))
        limited = results[:limit]
        return {
            "query": query,
            "matches": limited,
            "confidence": self._trace_confidence(limited),
            "why": self._trace_summary(limited),
        }

    def trace_component_usage(self, project_root: Path, component_name: str, limit: int = 50) -> dict[str, object]:
        self.init_db(project_root)
        needle = component_name.strip()
        if not needle:
            return {"component": component_name, "definitions": [], "references": [], "neighbors": []}

        self._ensure_parsed_candidates(project_root, needle, limit=limit * 4)
        definitions = [
            item for item in self.find_frontend_symbols(project_root, query=needle, limit=limit)
            if str(item.get("symbol") or "") == needle
        ]
        references = self.find_references(project_root, symbol=needle, limit=limit)["matches"]

        neighbors: list[dict[str, object]] = []
        seen_paths: set[str] = set()
        for definition in definitions[: min(len(definitions), 5)]:
            path = str(definition["path"])
            tree = self.get_component_tree(project_root, path=path, depth=1, limit=limit)
            for node in tree.get("nodes", []):
                node_path = str(node["path"])
                if node_path == path or node_path in seen_paths:
                    continue
                seen_paths.add(node_path)
                neighbors.append(node)

        match_count = len(definitions) + len(references) + len(neighbors[:limit])
        return {
            "component": component_name,
            "definitions": definitions,
            "references": references,
            "neighbors": neighbors[:limit],
            "confidence": "high" if match_count >= 4 else "medium" if match_count >= 2 else "low",
            "why": [
                f"definitions:{len(definitions)}",
                f"references:{len(references)}",
                f"neighbors:{len(neighbors[:limit])}",
            ],
        }

    def find_state_model_mismatch(self, project_root: Path, concept: str, limit: int = 50) -> dict[str, object]:
        self.init_db(project_root)
        needle = concept.strip()
        if not needle:
            return {"concept": concept, "matches": []}

        symbol_matches = self.search_symbols(project_root, query=needle, limit=limit)
        code_matches = self.search_code(project_root, query=needle, limit=limit)
        lower_concept = needle.lower()

        merged: list[dict[str, object]] = []
        seen: set[tuple[str, str | None, int | None]] = set()

        for item in symbol_matches:
            path = str(item["path"])
            symbol = str(item["symbol"])
            kind = str(item["kind"])
            lower_symbol = symbol.lower()

            mismatch_type = None
            score = 0
            if kind == "enum":
                mismatch_type = "enum_state_model"
                score = 120
            elif lower_symbol.startswith("is") and len(symbol) > 2:
                mismatch_type = "boolean_flag_model"
                score = 110
            elif any(token in lower_symbol for token in ("status", "state", "type", "mode", "kind", "flag")):
                mismatch_type = "named_state_field"
                score = 90
            elif lower_concept in lower_symbol:
                mismatch_type = "concept_match"
                score = 70

            if mismatch_type is None:
                continue

            key = (path, symbol, int(item["line_number"]))
            if key in seen:
                continue
            seen.add(key)
            snippet = None
            try:
                snippet = self.get_symbol_snippet(
                    project_root,
                    path=path,
                    symbol=symbol,
                    kind=kind,
                    line_number=int(item["line_number"]),
                )
            except FileNotFoundError:
                snippet = None

            merged.append(
                {
                    "score": score,
                    "path": path,
                    "symbol": symbol,
                    "kind": kind,
                    "line_number": item["line_number"],
                    "container": item["container"],
                    "layer": self._infer_layer_from_path(path),
                    "mismatch_type": mismatch_type,
                    "snippet": snippet["snippet"] if snippet else None,
                }
            )

        for item in code_matches:
            path = str(item["path"])
            key = (path, None, None)
            if key in seen:
                continue
            lower_path = path.lower()
            mismatch_type = None
            score = 0
            if any(token in lower_path for token in ("enum", "status", "state", "type", "flag")):
                mismatch_type = "file_state_hint"
                score = 50
            elif lower_concept in lower_path:
                mismatch_type = "concept_file_match"
                score = 40
            if mismatch_type is None:
                continue
            seen.add(key)
            merged.append(
                {
                    "score": score,
                    "path": path,
                    "symbol": None,
                    "kind": "file_match",
                    "line_number": None,
                    "container": None,
                    "layer": self._infer_layer_from_path(path),
                    "mismatch_type": mismatch_type,
                    "snippet": item["summary"],
                }
            )

        merged.sort(key=lambda item: (-int(item["score"]), self._layer_rank(str(item["layer"])), str(item["path"]), item["line_number"] or 0))
        return {
            "concept": concept,
            "matches": merged[:limit],
        }

    def find_routes(self, project_root: Path, query: str | None = None, limit: int = 50) -> dict[str, object]:
        self.init_db(project_root)
        needle = (query or "").strip()
        if needle:
            self._ensure_parsed_candidates(project_root, needle, limit=limit * 4)

        route_tokens = ("route", "controller", "/api/", "endpoint", "page", "handler")
        symbol_matches = self.search_symbols(project_root, needle or "controller", limit=max(limit * 3, 100)) if needle else self.search_symbols(project_root, "controller", limit=max(limit * 3, 100))
        code_matches = self.search_code(project_root, needle or "route", limit=max(limit * 3, 100)) if needle else self.search_code(project_root, "route", limit=max(limit * 3, 100))

        merged: list[dict[str, object]] = []
        seen: set[tuple[str, str | None, int | None]] = set()

        for item in symbol_matches:
            path = str(item["path"])
            symbol = str(item["symbol"])
            lower_symbol = symbol.lower()
            lower_path = path.lower()
            if needle and needle.lower() not in lower_symbol and needle.lower() not in lower_path and needle.lower() not in str(item.get("container") or "").lower():
                continue
            if not needle and not (any(token in lower_symbol for token in route_tokens) or any(token in lower_path for token in route_tokens)):
                continue

            score = 0
            if needle:
                score += self._score_text_match(needle, symbol, exact=110, prefix=80, contains=50)
                score += self._score_text_match(needle, path, exact=50, prefix=30, contains=20)
            for token in route_tokens:
                if token in lower_symbol:
                    score += 25
                if token in lower_path:
                    score += 20
            score += self._path_weight(project_root, path)
            layer = self._infer_layer_from_path(path)
            if layer == "api":
                score += 30
            elif layer == "ui":
                score += 15
            key = (path, symbol, int(item["line_number"]))
            if key in seen:
                continue
            seen.add(key)
            merged.append(
                {
                    "score": score,
                    "path": path,
                    "symbol": symbol,
                    "kind": item["kind"],
                    "line_number": item["line_number"],
                    "container": item["container"],
                    "layer": layer,
                }
            )

        for item in code_matches:
            path = str(item["path"])
            lower_path = path.lower()
            if not needle and not any(token in lower_path for token in route_tokens):
                continue
            key = (path, None, None)
            if key in seen:
                continue
            seen.add(key)
            score = self._path_weight(project_root, path)
            if needle:
                score += self._score_text_match(needle, path, exact=50, prefix=30, contains=20)
            if "/api/" in lower_path or lower_path.endswith("route.ts") or lower_path.endswith("route.js"):
                score += 40
            if "controller" in lower_path:
                score += 35
            merged.append(
                {
                    "score": score,
                    "path": path,
                    "symbol": None,
                    "kind": "file_match",
                    "line_number": None,
                    "container": None,
                    "layer": self._infer_layer_from_path(path),
                }
            )

        merged.sort(key=lambda item: (-int(item["score"]), self._layer_rank(str(item["layer"])), str(item["path"]), item["line_number"] or 0))
        return {"query": query, "matches": merged[:limit]}

    def trace_api_to_ui(self, project_root: Path, concept: str, limit: int = 50) -> dict[str, object]:
        routes = self.find_routes(project_root, query=concept, limit=limit)["matches"]
        touchpoints = self.find_ui_backend_touchpoints(project_root, concept=concept, limit=limit)["matches"]
        clusters = self.find_domain_clusters(project_root, concept=concept, limit=limit)["cluster"]

        api_side = [item for item in routes + touchpoints + clusters if item.get("layer") == "api"]
        ui_side = [item for item in touchpoints + clusters if item.get("layer") == "ui"]
        logic_side = [item for item in touchpoints + clusters if item.get("layer") == "logic"]

        return {
            "concept": concept,
            "api": api_side[:limit],
            "logic": logic_side[:limit],
            "ui": ui_side[:limit],
        }

    def find_ui_backend_touchpoints(self, project_root: Path, concept: str, limit: int = 50) -> dict[str, object]:
        self.init_db(project_root)
        needle = concept.strip()
        if not needle:
            return {"concept": concept, "matches": []}
        self._ensure_parsed_candidates(project_root, needle, limit=limit * 4)

        symbol_matches = self.search_symbols(project_root, query=needle, limit=limit)
        code_matches = self.search_code(project_root, query=needle, limit=limit)

        merged: list[dict[str, object]] = []
        seen: set[tuple[str, str | None, int | None]] = set()

        for item in symbol_matches:
            path = str(item["path"])
            layer = self._infer_layer_from_path(path)
            if layer not in {"data", "logic", "api", "ui"}:
                continue
            kind = str(item["kind"])
            symbol = str(item["symbol"])
            score = 0
            score += self._score_text_match(needle, symbol, exact=120, prefix=90, contains=60)
            if layer == "api":
                score += 25
            elif layer == "ui":
                score += 20
            elif layer == "logic":
                score += 18
            elif layer == "data":
                score += 15

            key = (path, symbol, int(item["line_number"]))
            if key in seen:
                continue
            seen.add(key)
            snippet = None
            try:
                snippet = self.get_symbol_snippet(
                    project_root,
                    path=path,
                    symbol=symbol,
                    kind=kind,
                    line_number=int(item["line_number"]),
                )
            except FileNotFoundError:
                snippet = None

            merged.append(
                {
                    "score": score,
                    "path": path,
                    "layer": layer,
                    "symbol": symbol,
                    "kind": kind,
                    "line_number": item["line_number"],
                    "container": item["container"],
                    "snippet": snippet["snippet"] if snippet else None,
                }
            )

        for item in code_matches:
            path = str(item["path"])
            layer = self._infer_layer_from_path(path)
            if layer not in {"data", "logic", "api", "ui"}:
                continue
            key = (path, None, None)
            if key in seen:
                continue
            seen.add(key)
            score = self._score_text_match(needle, path, exact=60, prefix=35, contains=20)
            merged.append(
                {
                    "score": score,
                    "path": path,
                    "layer": layer,
                    "symbol": None,
                    "kind": "file_match",
                    "line_number": None,
                    "container": None,
                    "snippet": item["summary"],
                }
            )

        merged.sort(key=lambda item: (-int(item["score"]), self._layer_rank(str(item["layer"])), str(item["path"]), item["line_number"] or 0))
        return {
            "concept": concept,
            "matches": merged[:limit],
        }

    def find_policy_surfaces(self, project_root: Path, concept: str, limit: int = 50) -> dict[str, object]:
        self.init_db(project_root)
        needle = concept.strip()
        if not needle:
            return {"concept": concept, "matches": []}
        self._ensure_parsed_candidates(project_root, needle, limit=limit * 4)

        symbol_matches = self.search_symbols(project_root, query=needle, limit=limit)
        code_matches = self.search_code(project_root, query=needle, limit=limit)

        merged: list[dict[str, object]] = []
        seen: set[tuple[str, str | None, int | None]] = set()

        policy_tokens = ("policy", "permission", "role", "claim", "guard", "authorize", "auth")

        for item in symbol_matches:
            path = str(item["path"])
            layer = self._infer_layer_from_path(path)
            symbol = str(item["symbol"])
            kind = str(item["kind"])
            lower_symbol = symbol.lower()
            lower_path = path.lower()

            score = self._score_text_match(needle, symbol, exact=120, prefix=90, contains=60)
            if any(token in lower_symbol for token in policy_tokens):
                score += 50
            if any(token in lower_path for token in policy_tokens):
                score += 30
            if layer == "api":
                score += 25
            elif layer == "logic":
                score += 20
            elif layer == "ui":
                score += 12
            else:
                score += 5

            key = (path, symbol, int(item["line_number"]))
            if key in seen:
                continue
            seen.add(key)

            snippet = None
            try:
                snippet = self.get_symbol_snippet(
                    project_root,
                    path=path,
                    symbol=symbol,
                    kind=kind,
                    line_number=int(item["line_number"]),
                )
            except FileNotFoundError:
                snippet = None

            merged.append(
                {
                    "score": score,
                    "path": path,
                    "layer": layer,
                    "symbol": symbol,
                    "kind": kind,
                    "line_number": item["line_number"],
                    "container": item["container"],
                    "snippet": snippet["snippet"] if snippet else None,
                }
            )

        for item in code_matches:
            path = str(item["path"])
            lower_path = path.lower()
            if not any(token in lower_path for token in policy_tokens) and needle.lower() not in lower_path:
                continue
            key = (path, None, None)
            if key in seen:
                continue
            seen.add(key)
            merged.append(
                {
                    "score": 40 if needle.lower() in lower_path else 20,
                    "path": path,
                    "layer": self._infer_layer_from_path(path),
                    "symbol": None,
                    "kind": "file_match",
                    "line_number": None,
                    "container": None,
                    "snippet": item["summary"],
                }
            )

        merged.sort(key=lambda item: (-int(item["score"]), self._layer_rank(str(item["layer"])), str(item["path"]), item["line_number"] or 0))
        return {
            "concept": concept,
            "matches": merged[:limit],
        }

    def find_entrypoints(self, project_root: Path, concept: str | None = None, limit: int = 50) -> dict[str, object]:
        self.init_db(project_root)
        needle = (concept or "").strip()
        if needle:
            self._ensure_parsed_candidates(project_root, needle, limit=limit * 4)

        patterns = (
            "start",
            "bootstrap",
            "init",
            "initialize",
            "setup",
            "register",
            "configure",
            "createapp",
            "main",
            "app",
            "provider",
        )

        symbol_matches = self.search_symbols(project_root, needle or "init", limit=max(limit * 2, 50)) if needle else []
        code_matches = self.search_code(project_root, needle or "main", limit=max(limit * 2, 50)) if needle else []

        merged: list[dict[str, object]] = []
        seen: set[tuple[str, str | None, int | None]] = set()

        for item in symbol_matches:
            path = str(item["path"])
            symbol = str(item["symbol"])
            lower_symbol = symbol.lower()
            lower_path = path.lower()
            if needle:
                if needle.lower() not in lower_symbol and needle.lower() not in lower_path and needle.lower() not in str(item.get("container") or "").lower():
                    continue
            elif not any(token in lower_symbol for token in patterns):
                continue

            score = 0
            for token in patterns:
                if token in lower_symbol:
                    score += 25
            if str(item["kind"]) in {"initializer", "context_provider", "component", "function"}:
                score += 20
            score += self._path_weight(project_root, path)
            layer = self._infer_layer_from_path(path)
            if layer in {"api", "logic", "ui"}:
                score += 10
            key = (path, symbol, int(item["line_number"]))
            if key in seen:
                continue
            seen.add(key)
            merged.append(
                {
                    "score": score,
                    "path": path,
                    "symbol": symbol,
                    "kind": item["kind"],
                    "line_number": item["line_number"],
                    "container": item["container"],
                    "layer": layer,
                }
            )

        for item in code_matches:
            path = str(item["path"])
            lower_path = path.lower()
            if not any(token in lower_path for token in patterns):
                continue
            key = (path, None, None)
            if key in seen:
                continue
            seen.add(key)
            merged.append(
                {
                    "score": self._path_weight(project_root, path) + 20,
                    "path": path,
                    "symbol": None,
                    "kind": "file_match",
                    "line_number": None,
                    "container": None,
                    "layer": self._infer_layer_from_path(path),
                }
            )

        merged.sort(key=lambda item: (-int(item["score"]), self._layer_rank(str(item["layer"])), str(item["path"]), item["line_number"] or 0))
        return {
            "concept": concept,
            "matches": merged[:limit],
        }

    def find_domain_clusters(self, project_root: Path, concept: str, limit: int = 50) -> dict[str, object]:
        self.init_db(project_root)
        needle = concept.strip()
        if not needle:
            return {"concept": concept, "cluster": []}
        self._ensure_parsed_candidates(project_root, needle, limit=limit * 4)

        code_matches = self.search_code(project_root, needle, limit=limit)
        symbol_matches = self.search_symbols(project_root, needle, limit=limit)
        schema_entities = []
        schema_fields = []
        try:
            from .schema_index_store import SchemaIndexStore

            schema = SchemaIndexStore()
            schema_entities = schema.find_schema_entities(project_root, query=needle, limit=limit)
            schema_fields = schema.find_schema_field(project_root, needle, limit=limit)
        except Exception:
            pass

        cluster: list[dict[str, object]] = []
        seen: set[tuple[str, str | None]] = set()

        for item in symbol_matches:
            key = (str(item["path"]), str(item["symbol"]))
            if key in seen:
                continue
            seen.add(key)
            cluster.append(
                {
                    "source": "symbol",
                    "path": item["path"],
                    "layer": self._infer_layer_from_path(str(item["path"])),
                    "symbol": item["symbol"],
                    "kind": item["kind"],
                    "line_number": item["line_number"],
                }
            )

        for item in code_matches:
            key = (str(item["path"]), None)
            if key in seen:
                continue
            seen.add(key)
            cluster.append(
                {
                    "source": "file",
                    "path": item["path"],
                    "layer": self._infer_layer_from_path(str(item["path"])),
                    "symbol": None,
                    "kind": "file_match",
                    "line_number": None,
                }
            )

        for item in schema_entities:
            key = (str(item["path"]), str(item["entity_name"]))
            if key in seen:
                continue
            seen.add(key)
            cluster.append(
                {
                    "source": "schema_entity",
                    "path": item["path"],
                    "layer": self._infer_layer_from_path(str(item["path"])),
                    "symbol": item["entity_name"],
                    "kind": item["kind"],
                    "line_number": item["line_number"],
                }
            )

        for item in schema_fields:
            key = (str(item["path"]), str(item["field_name"]))
            if key in seen:
                continue
            seen.add(key)
            cluster.append(
                {
                    "source": "schema_field",
                    "path": item["path"],
                    "layer": self._infer_layer_from_path(str(item["path"])),
                    "symbol": item["field_name"],
                    "kind": item["field_kind"],
                    "line_number": item["line_number"],
                }
            )

        cluster.sort(key=lambda item: (self._layer_rank(str(item["layer"])), str(item["path"]), str(item["symbol"] or "")))
        return {
            "concept": concept,
            "cluster": cluster[:limit],
        }

    def find_transition_points(self, project_root: Path, concept: str | None = None, limit: int = 50) -> dict[str, object]:
        self.init_db(project_root)
        needle = (concept or "").strip()
        if needle:
            self._ensure_parsed_candidates(project_root, needle, limit=limit * 4)

        code_matches = self.search_code(project_root, needle or "legacy", limit=max(limit * 3, 100))
        symbol_matches = self.search_symbols(project_root, needle or "legacy", limit=max(limit * 3, 100))

        transition_tokens = (
            "legacy",
            "migration",
            "migrate",
            "adapter",
            "compat",
            "compatibility",
            "bridge",
            "shim",
            "deprecated",
            "fallback",
            "transitional",
        )

        merged: list[dict[str, object]] = []
        seen: set[tuple[str, str | None, int | None]] = set()

        for item in symbol_matches:
            path = str(item["path"])
            symbol = str(item["symbol"])
            lower_symbol = symbol.lower()
            lower_path = path.lower()
            score = 0
            if needle:
                score += self._score_text_match(needle, symbol, exact=100, prefix=70, contains=40)
                score += self._score_text_match(needle, path, exact=50, prefix=30, contains=20)
            for token in transition_tokens:
                if token in lower_symbol:
                    score += 35
                if token in lower_path:
                    score += 20
            if score <= 0:
                continue
            key = (path, symbol, int(item["line_number"]))
            if key in seen:
                continue
            seen.add(key)
            snippet = None
            try:
                snippet = self.get_symbol_snippet(
                    project_root,
                    path=path,
                    symbol=symbol,
                    kind=str(item["kind"]),
                    line_number=int(item["line_number"]),
                )
            except FileNotFoundError:
                snippet = None
            merged.append(
                {
                    "score": score,
                    "path": path,
                    "layer": self._infer_layer_from_path(path),
                    "symbol": symbol,
                    "kind": item["kind"],
                    "line_number": item["line_number"],
                    "container": item["container"],
                    "snippet": snippet["snippet"] if snippet else None,
                }
            )

        for item in code_matches:
            path = str(item["path"])
            lower_path = path.lower()
            score = 0
            if needle:
                score += self._score_text_match(needle, path, exact=50, prefix=30, contains=20)
            for token in transition_tokens:
                if token in lower_path:
                    score += 30
                if token in str(item["summary"]).lower():
                    score += 15
            if score <= 0:
                continue
            key = (path, None, None)
            if key in seen:
                continue
            seen.add(key)
            merged.append(
                {
                    "score": score,
                    "path": path,
                    "layer": self._infer_layer_from_path(path),
                    "symbol": None,
                    "kind": "file_match",
                    "line_number": None,
                    "container": None,
                    "snippet": item["summary"],
                }
            )

        merged.sort(key=lambda item: (-int(item["score"]), self._layer_rank(str(item["layer"])), str(item["path"]), item["line_number"] or 0))
        return {
            "concept": concept,
            "matches": merged[:limit],
        }

    def get_outline(self, project_root: Path, path: str) -> list[dict[str, str | int | bool]]:
        self.init_db(project_root)
        with self.connect(project_root) as conn:
            rows = conn.execute(
                """
                SELECT symbol, kind, line_number, container, is_partial
                FROM code_outlines
                WHERE path = ?
                ORDER BY line_number ASC, symbol ASC
                """,
                (path,),
            ).fetchall()
        return [
            {
                "symbol": row["symbol"],
                "kind": row["kind"],
                "line_number": int(row["line_number"]),
                "container": row["container"],
                "is_partial": bool(row["is_partial"]),
            }
            for row in rows
        ]

    def find_partial_group(self, project_root: Path, symbol: str, limit: int = 50) -> list[dict[str, str | int | bool | None]]:
        self.init_db(project_root)
        if symbol.strip():
            self._ensure_parsed_candidates(project_root, symbol, limit=limit * 4)
        with self.connect(project_root) as conn:
            rows = conn.execute(
                """
                SELECT path, symbol, kind, line_number, container, is_partial
                FROM code_outlines
                WHERE symbol = ? AND is_partial = 1
                ORDER BY path ASC, line_number ASC
                LIMIT ?
                """,
                (symbol, limit),
            ).fetchall()
        return [
            {
                "path": row["path"],
                "symbol": row["symbol"],
                "kind": row["kind"],
                "line_number": int(row["line_number"]),
                "container": row["container"],
                "is_partial": bool(row["is_partial"]),
            }
            for row in rows
        ]

    def find_data_structures(
        self,
        project_root: Path,
        query: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, str | int | bool | None]]:
        self.init_db(project_root)
        if query and query.strip():
            self._ensure_parsed_candidates(project_root, query, limit=limit * 4)
        params: list[object] = []
        sql = """
            SELECT path, symbol, kind, line_number, container, is_partial
            FROM code_outlines
            WHERE kind IN ('class', 'record', 'struct', 'interface', 'enum', 'property', 'field', 'enum_member')
        """
        if query and query.strip():
            needle = f"%{query.strip()}%"
            sql += " AND (symbol LIKE ? OR COALESCE(container, '') LIKE ? OR path LIKE ?)"
            params.extend([needle, needle, needle])
        sql += " ORDER BY path ASC, line_number ASC LIMIT ?"
        params.append(limit)
        with self.connect(project_root) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            {
                "path": row["path"],
                "symbol": row["symbol"],
                "kind": row["kind"],
                "line_number": int(row["line_number"]),
                "container": row["container"],
                "is_partial": bool(row["is_partial"]),
            }
            for row in rows
        ]

    def find_frontend_symbols(
        self,
        project_root: Path,
        query: str | None = None,
        kinds: tuple[str, ...] = ("component", "context_provider", "hook", "function", "initializer"),
        limit: int = 50,
    ) -> list[dict[str, str | int | bool | None]]:
        self.init_db(project_root)
        if query and query.strip():
            self._ensure_parsed_candidates(project_root, query, limit=limit * 4)
        placeholders = ", ".join("?" for _ in kinds)
        sql = f"""
            SELECT path, symbol, kind, line_number, container, is_partial
            FROM code_outlines
            WHERE kind IN ({placeholders})
        """
        params: list[object] = list(kinds)
        if query and query.strip():
            needle = f"%{query.strip()}%"
            sql += " AND (symbol LIKE ? OR COALESCE(container, '') LIKE ? OR path LIKE ?)"
            params.extend([needle, needle, needle])
        sql += " LIMIT 500"
        with self.connect(project_root) as conn:
            rows = conn.execute(sql, params).fetchall()
        ranked = []
        needle_text = (query or "").strip()
        kind_weight = {"component": 30, "context_provider": 28, "hook": 24, "initializer": 18, "function": 12}
        for row in rows:
            score = kind_weight.get(row["kind"], 0)
            reasons: list[str] = []
            if kind_weight.get(row["kind"], 0):
                reasons.append(f"kind_weight:{kind_weight.get(row['kind'], 0)}")
            if needle_text:
                score += self._score_text_match(needle_text, row["symbol"], exact=140, prefix=100, contains=70, reasons=reasons, label="symbol")
                score += self._score_text_match(needle_text, row["container"] or "", exact=35, prefix=20, contains=10, reasons=reasons, label="container")
            path_weight = self._path_weight(project_root, str(row["path"]))
            score += path_weight
            if path_weight:
                reasons.append(f"path_weight:{path_weight}")
            score -= str(row["path"]).count("/")
            ranked.append((score, row, reasons))
        ranked.sort(key=lambda item: (-item[0], item[1]["path"], int(item[1]["line_number"])))
        return [
            {
                "path": row["path"],
                "symbol": row["symbol"],
                "kind": row["kind"],
                "line_number": int(row["line_number"]),
                "container": row["container"],
                "is_partial": bool(row["is_partial"]),
                "why": reasons,
            }
            for _, row, reasons in ranked[:limit]
        ]

    def find_initializers(
        self, project_root: Path, path: str | None = None, limit: int = 50
    ) -> list[dict[str, str | int | bool | None]]:
        self.init_db(project_root)
        query = """
            SELECT path, symbol, kind, line_number, container, is_partial
            FROM code_outlines
            WHERE kind = 'initializer'
        """
        params: list[object] = []
        if path is not None:
            query += " AND path = ?"
            params.append(path)
        query += " ORDER BY path ASC, line_number ASC LIMIT ?"
        params.append(limit)
        with self.connect(project_root) as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            {
                "path": row["path"],
                "symbol": row["symbol"],
                "kind": row["kind"],
                "line_number": int(row["line_number"]),
                "container": row["container"],
                "is_partial": bool(row["is_partial"]),
            }
            for row in rows
        ]

    def get_symbol_snippet(
        self,
        project_root: Path,
        path: str,
        symbol: str,
        kind: str | None = None,
        line_number: int | None = None,
    ) -> dict[str, str | int | bool | None]:
        self.init_db(project_root)
        with self.connect(project_root) as conn:
            if line_number is not None:
                row = conn.execute(
                    """
                    SELECT o.path, o.symbol, o.kind, o.line_number, o.container, o.is_partial, f.language
                    FROM code_outlines o
                    JOIN code_files f ON f.path = o.path
                    WHERE o.path = ? AND o.symbol = ? AND o.line_number = ?
                    LIMIT 1
                    """,
                    (path, symbol, line_number),
                ).fetchone()
            elif kind is not None:
                row = conn.execute(
                    """
                    SELECT o.path, o.symbol, o.kind, o.line_number, o.container, o.is_partial, f.language
                    FROM code_outlines o
                    JOIN code_files f ON f.path = o.path
                    WHERE o.path = ? AND o.symbol = ? AND o.kind = ?
                    ORDER BY o.line_number ASC
                    LIMIT 1
                    """,
                    (path, symbol, kind),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT o.path, o.symbol, o.kind, o.line_number, o.container, o.is_partial, f.language
                    FROM code_outlines o
                    JOIN code_files f ON f.path = o.path
                    WHERE o.path = ? AND o.symbol = ?
                    ORDER BY o.line_number ASC
                    LIMIT 1
                    """,
                    (path, symbol),
                ).fetchone()

        if row is None:
            raise FileNotFoundError(f"No indexed symbol '{symbol}' found in {path}")

        abs_path = project_root / row["path"]
        text = abs_path.read_text(encoding="utf-8", errors="ignore")
        snippet = self._extract_snippet(text, row["language"], int(row["line_number"]))
        return {
            "path": row["path"],
            "symbol": row["symbol"],
            "kind": row["kind"],
            "line_number": int(row["line_number"]),
            "container": row["container"],
            "is_partial": bool(row["is_partial"]),
            "language": row["language"],
            "snippet": snippet,
        }

    def get_symbol_bundle(
        self,
        project_root: Path,
        symbol: str,
        path: str | None = None,
        kind: str | None = None,
        limit: int = 50,
    ) -> dict[str, object]:
        self.init_db(project_root)
        definitions = self.search_symbols(project_root, query=symbol, limit=limit)
        if path is not None:
            definitions = [item for item in definitions if item["path"] == path]
        if kind is not None:
            definitions = [item for item in definitions if item["kind"] == kind]

        if not definitions:
            return {
                "symbol": symbol,
                "definitions": [],
                "references": [],
                "dependencies": [],
                "partials": [],
                "schema_entities": [],
                "schema_fields": [],
            }

        primary = definitions[0]
        definition_snippets = [
            self.get_symbol_snippet(
                project_root,
                path=str(item["path"]),
                symbol=str(item["symbol"]),
                kind=str(item["kind"]),
                line_number=int(item["line_number"]),
            )
            for item in definitions[: min(len(definitions), 8)]
        ]
        references = self.find_references(project_root, symbol=symbol, limit=limit)["matches"]
        dependencies = self.get_dependencies(project_root, str(primary["path"]))
        partials = self.find_partial_group(project_root, symbol=symbol, limit=limit)

        schema_entities = []
        schema_fields = []
        try:
            from .schema_index_store import SchemaIndexStore

            schema = SchemaIndexStore()
            schema_entities = schema.find_schema_entities(project_root, query=symbol, limit=limit)
            schema_fields = schema.find_schema_field(project_root, symbol, limit=limit)
        except Exception:
            pass

        return {
            "symbol": symbol,
            "definitions": definition_snippets,
            "references": references,
            "dependencies": dependencies,
            "partials": partials,
            "schema_entities": schema_entities,
            "schema_fields": schema_fields,
        }

    def get_subsystem_bundle(self, project_root: Path, concept: str, limit: int = 20) -> dict[str, object]:
        domain_cluster = self.find_domain_clusters(project_root, concept=concept, limit=limit)
        touchpoints = self.find_ui_backend_touchpoints(project_root, concept=concept, limit=limit)
        policy = self.find_policy_surfaces(project_root, concept=concept, limit=limit)
        transitions = self.find_transition_points(project_root, concept=concept, limit=limit)
        data_structures = self.find_data_structures(project_root, query=concept, limit=limit)
        entrypoints = self.find_entrypoints(project_root, concept=concept, limit=limit)

        return {
            "concept": concept,
            "domain_cluster": domain_cluster["cluster"],
            "touchpoints": touchpoints["matches"],
            "policy_surfaces": policy["matches"],
            "transition_points": transitions["matches"],
            "data_structures": data_structures,
            "entrypoints": entrypoints["matches"],
        }

    def get_dependencies(self, project_root: Path, path: str) -> list[dict[str, str]]:
        self.init_db(project_root)
        with self.connect(project_root) as conn:
            rows = conn.execute(
                "SELECT target, kind FROM code_edges WHERE source_path = ? ORDER BY kind, target",
                (path,),
            ).fetchall()
        return [{"target": row["target"], "kind": row["kind"]} for row in rows]

    def get_dependency_bundle(self, project_root: Path, path: str, include_dependents: bool = False, limit: int = 20) -> dict[str, object]:
        self.init_db(project_root)
        root_bundle = self.get_file_bundle(project_root, path)
        dependencies = []
        for edge in self.get_dependencies(project_root, path):
            resolved_paths = self._resolve_edge_to_paths(project_root, path, edge["target"], edge["kind"], limit=limit)
            dependencies.append(
                {
                    "target": edge["target"],
                    "kind": edge["kind"],
                    "resolved_paths": resolved_paths,
                    "resolved_files": [self._get_file_stub(project_root, item) for item in resolved_paths],
                }
            )

        dependents = []
        if include_dependents:
            direct_dependents = self.find_dependents(project_root, target=path, limit=limit)
            dependents = [
                {
                    "path": item["path"],
                    "kind": item["kind"],
                    "file": self._get_file_stub(project_root, item["path"]),
                }
                for item in direct_dependents
            ]

        return {
            "root": root_bundle,
            "dependencies": dependencies,
            "dependents": dependents,
        }

    def find_dependents(self, project_root: Path, target: str, limit: int = 50) -> list[dict[str, str]]:
        self.init_db(project_root)
        with self.connect(project_root) as conn:
            rows = conn.execute(
                "SELECT source_path, kind FROM code_edges WHERE target = ? ORDER BY source_path LIMIT ?",
                (target, limit),
            ).fetchall()
        return [{"path": row["source_path"], "kind": row["kind"]} for row in rows]

    def get_partial_bundle(self, project_root: Path, symbol: str, limit: int = 50) -> list[dict[str, str | int | bool | None]]:
        partials = self.find_partial_group(project_root, symbol=symbol, limit=limit)
        bundle = []
        for item in partials:
            bundle.append(
                self.get_symbol_snippet(
                    project_root,
                    path=str(item["path"]),
                    symbol=str(item["symbol"]),
                    kind=str(item["kind"]),
                    line_number=int(item["line_number"]),
                )
            )
        return bundle

    def get_file_bundle(self, project_root: Path, path: str) -> dict[str, object]:
        self.init_db(project_root)
        with self.connect(project_root) as conn:
            row = conn.execute(
                "SELECT path, language, line_count, summary, role FROM code_files WHERE path = ? LIMIT 1",
                (path,),
            ).fetchone()
        if row is None:
            raise FileNotFoundError(f"No indexed code file found: {path}")

        outline = self.get_outline(project_root, path)
        partial_groups = []
        initializers = []
        if row["language"] == "csharp":
            seen = set()
            for item in outline:
                if item["is_partial"] and item["symbol"] not in seen:
                    seen.add(item["symbol"])
                    partial_groups.append(
                        {
                            "symbol": item["symbol"],
                            "bundle": self.get_partial_bundle(project_root, symbol=str(item["symbol"])),
                        }
                    )
        if row["language"] in {"javascript", "typescript", "jsx", "tsx"}:
            initializers = self.find_initializers(project_root, path=path)

        return {
            "path": row["path"],
            "language": row["language"],
            "line_count": int(row["line_count"]),
            "summary": row["summary"],
            "role": row["role"] or "unknown",
            "outline": outline,
            "initializers": initializers,
            "partial_groups": partial_groups,
        }

    def get_component_bundle(self, project_root: Path, path: str, limit: int = 20) -> dict[str, object]:
        root = self.get_file_bundle(project_root, path)
        outline = root.get("outline", [])
        frontend_items = [
            item for item in outline if item.get("kind") in {"component", "context_provider", "hook", "initializer", "function"}
        ]

        imported_frontend_files: list[dict[str, object]] = []
        seen: set[str] = set()
        for dep in self.get_dependencies(project_root, path):
            for resolved in self._resolve_edge_to_paths(project_root, path, dep["target"], dep["kind"], limit=limit):
                if resolved in seen:
                    continue
                seen.add(resolved)
                stub = self._get_file_stub(project_root, resolved)
                if stub is None:
                    continue
                bundle = self.get_file_bundle(project_root, resolved)
                imported_frontend_files.append(
                    {
                        "target": dep["target"],
                        "kind": dep["kind"],
                        "file": bundle,
                    }
                )

        imported_frontend_files.sort(
            key=lambda item: (
                -1 if item["file"].get("role") in {"component", "context-provider", "hook-module", "page", "layout"} else 0,
                str(item["file"]["path"]),
            )
        )

        return {
            "root": root,
            "frontend_symbols": frontend_items,
            "imported_frontend_files": imported_frontend_files[:limit],
        }

    def get_service_bundle(self, project_root: Path, path: str, limit: int = 20) -> dict[str, object]:
        root = self.get_file_bundle(project_root, path)
        outline = root.get("outline", [])
        service_symbols = [item for item in outline if item.get("kind") in {"class", "record", "struct", "function", "method"}]

        dependencies = self.get_dependency_bundle(project_root, path=path, include_dependents=True, limit=limit)
        local_related: list[dict[str, object]] = []
        seen: set[str] = set()
        for dep in dependencies.get("dependencies", []):
            for resolved in dep.get("resolved_paths", []):
                if resolved in seen:
                    continue
                seen.add(resolved)
                stub = self._get_file_stub(project_root, resolved)
                if stub is None:
                    continue
                role = stub.get("role")
                if role in {"service", "controller", "data-model", "policy", "unknown"}:
                    local_related.append(
                        {
                            "target": dep["target"],
                            "kind": dep["kind"],
                            "file": self.get_file_bundle(project_root, resolved),
                        }
                    )

        local_related.sort(
            key=lambda item: (
                -1 if item["file"].get("role") in {"service", "controller", "data-model", "policy"} else 0,
                str(item["file"]["path"]),
            )
        )

        return {
            "root": root,
            "service_symbols": service_symbols,
            "dependencies": dependencies,
            "local_related_files": local_related[:limit],
        }

    def get_query_bundle(self, project_root: Path, path: str, limit: int = 20) -> dict[str, object]:
        root = self.get_file_bundle(project_root, path)
        hotspot_matches = self.find_query_hotspots(project_root, query=Path(path).stem, limit=limit)["matches"]
        hotspot = next((item for item in hotspot_matches if item["path"] == path), None)
        dependencies = self.get_dependency_bundle(project_root, path=path, include_dependents=True, limit=limit)

        schema_entities = []
        schema_fields = []
        try:
            from .schema_index_store import SchemaIndexStore

            schema = SchemaIndexStore()
            abs_path = project_root / path
            text = abs_path.read_text(encoding="utf-8", errors="ignore")
            entities = schema.find_schema_entities(project_root, query=None, limit=500)
            fields = schema.find_schema_field(project_root, "", limit=1000)
            lower_text = text.lower()
            schema_entities = [item for item in entities if str(item["entity_name"]).lower() in lower_text][:limit]
            schema_fields = [item for item in fields if str(item["field_name"]).lower() in lower_text][:limit]
        except Exception:
            pass

        return {
            "root": root,
            "hotspot": hotspot,
            "dependencies": dependencies,
            "schema_entities": schema_entities,
            "schema_fields": schema_fields,
        }

    def trace_query_shape(self, project_root: Path, path: str, limit: int = 20) -> dict[str, object]:
        root = self.get_file_bundle(project_root, path)
        query_bundle = self.get_query_bundle(project_root, path=path, limit=limit)

        relationship_paths = []
        try:
            from .schema_index_store import SchemaIndexStore

            schema = SchemaIndexStore()
            entity_names = [item["entity_name"] for item in query_bundle.get("schema_entities", [])]
            pairs = []
            for i, left in enumerate(entity_names):
                for right in entity_names[i + 1 :]:
                    pairs.append((left, right))
            for left, right in pairs[:limit]:
                traced = schema.trace_relationship_path(project_root, left, right, limit=5)
                if traced.get("paths"):
                    relationship_paths.append(traced)
        except Exception:
            relationship_paths = []

        return {
            "root": root,
            "hotspot": query_bundle.get("hotspot"),
            "dependencies": query_bundle.get("dependencies"),
            "schema_entities": query_bundle.get("schema_entities"),
            "schema_fields": query_bundle.get("schema_fields"),
            "relationship_paths": relationship_paths,
        }

    def get_component_tree(self, project_root: Path, path: str, depth: int = 2, limit: int = 50) -> dict[str, object]:
        seen: set[str] = set()
        queue: list[tuple[str, int]] = [(path, 0)]
        nodes: list[dict[str, object]] = []
        edges: list[dict[str, object]] = []

        while queue and len(nodes) < limit:
            current_path, current_depth = queue.pop(0)
            if current_path in seen:
                continue
            seen.add(current_path)

            try:
                bundle = self.get_file_bundle(project_root, current_path)
            except FileNotFoundError:
                continue

            if bundle.get("role") not in {"component", "context-provider", "hook-module", "page", "layout", "unknown"} and current_depth > 0:
                continue

            nodes.append(
                {
                    "path": bundle["path"],
                    "role": bundle["role"],
                    "symbols": [item["symbol"] for item in bundle.get("outline", []) if item.get("kind") in {"component", "context_provider", "hook", "function", "initializer"}],
                    "depth": current_depth,
                }
            )

            if current_depth >= depth:
                continue

            for dep in self.get_dependencies(project_root, current_path):
                for resolved in self._resolve_edge_to_paths(project_root, current_path, dep["target"], dep["kind"], limit=limit):
                    stub = self._get_file_stub(project_root, resolved)
                    if stub is None:
                        continue
                    if stub.get("role") not in {"component", "context-provider", "hook-module", "page", "layout", "unknown"}:
                        continue
                    edges.append({
                        "from": current_path,
                        "to": resolved,
                        "kind": dep["kind"],
                    })
                    if resolved not in seen:
                        queue.append((resolved, current_depth + 1))

        return {
            "root": path,
            "depth": depth,
            "nodes": nodes,
            "edges": edges[:limit],
        }

    def get_session_code_bundle(self, project_root: Path, session_id: str) -> dict[str, object]:
        if self.session_store is None:
            raise RuntimeError("SessionStore is required for session-guided code bundles")
        context = self.session_store.read_context(project_root, session_id)
        files = []
        for line in context.sections.get("Relevant Files", []):
            stripped = line.strip()
            if not stripped.startswith("-"):
                continue
            candidate = stripped[1:].strip().strip('`')
            if not candidate or candidate.startswith('/.MEMORY/'):
                continue
            files.append(candidate)

        bundles = []
        for file_path in files:
            try:
                bundles.append(self.get_file_bundle(project_root, file_path))
            except FileNotFoundError:
                bundles.append({"path": file_path, "missing": True})

        return {
            "session_id": session_id,
            "files": bundles,
        }

    def get_context_bundle(
        self,
        project_root: Path,
        session_id: str,
        include_dependencies: bool = True,
        include_styles: bool = True,
        limit: int = 50,
    ) -> dict[str, object]:
        if self.session_store is None:
            raise RuntimeError("SessionStore is required for session-guided bundles")

        primary_paths = self.session_store.session_code_targets(project_root, session_id)
        indexed_primary_paths = [path for path in primary_paths if self._is_indexed_file(project_root, path)]
        primary_files = [self.get_file_bundle(project_root, path) for path in indexed_primary_paths]

        dependency_items: list[dict[str, object]] = []
        if include_dependencies:
            seen_dep_paths: set[str] = set()
            for path in indexed_primary_paths:
                dep_bundle = self.get_dependency_bundle(project_root, path=path, include_dependents=False, limit=limit)
                for dep in dep_bundle["dependencies"]:
                    for resolved in dep["resolved_paths"]:
                        if resolved in primary_paths or resolved in seen_dep_paths:
                            continue
                        seen_dep_paths.add(resolved)
                        stub = self._get_file_stub(project_root, resolved)
                        if stub is not None:
                            dependency_items.append({
                                "path": resolved,
                                "source": path,
                                "kind": dep["kind"],
                                "target": dep["target"],
                                "file": stub,
                            })
            dependency_items.sort(key=lambda item: (item["path"].count("/"), item["path"], item["kind"]))

        ordered_items: list[dict[str, object]] = []
        score = 1000
        for index, item in enumerate(primary_files):
            ordered_items.append({
                "score": score - index,
                "kind": "primary_file",
                "path": item["path"],
                "payload": item,
            })
        dep_score = 800
        for index, item in enumerate(dependency_items):
            ordered_items.append({
                "score": dep_score - index,
                "kind": "dependency_file",
                "path": item["path"],
                "payload": item,
            })
        ordered_items.sort(key=lambda item: (-int(item["score"]), str(item["path"])))

        return {
            "session_id": session_id,
            "primary_files": primary_files,
            "dependency_files": dependency_items,
            "ordered_items": ordered_items,
        }

    def get_preset_bundle(
        self,
        project_root: Path,
        preset: str,
        value: str,
        limit: int = 50,
    ) -> dict[str, object]:
        if preset == "csharp-partial":
            return {
                "preset": preset,
                "value": value,
                "bundle": self.get_partial_bundle(project_root, symbol=value, limit=limit),
            }
        if preset == "js-initializer":
            return {
                "preset": preset,
                "value": value,
                "bundle": self.find_initializers(project_root, path=value if value.strip() else None, limit=limit),
            }
        if preset == "data-structure":
            return {
                "preset": preset,
                "value": value,
                "bundle": self.find_data_structures(project_root, query=value, limit=limit),
            }
        if preset == "session":
            return {
                "preset": preset,
                "value": value,
                "bundle": self.get_session_code_bundle(project_root, session_id=value),
            }
        if preset == "context":
            return {
                "preset": preset,
                "value": value,
                "bundle": self.get_context_bundle(project_root, session_id=value, limit=limit),
            }
        if preset == "dependency":
            return {
                "preset": preset,
                "value": value,
                "bundle": self.get_dependency_bundle(project_root, path=value, limit=limit),
            }
        raise ValueError(
            "Unknown preset. Allowed: csharp-partial, js-initializer, data-structure, session, context, dependency"
        )

    def _should_skip(self, project_root: Path, path: Path, include_tests: bool = False) -> bool:
        rel = path.relative_to(project_root).as_posix()
        prefixes = (
            ".git/",
            "build/",
            ".MEMORY/",
            ".opencode/",
            ".claude/",
            ".github/",
            ".BACKUP/",
            ".backups/",
            "temp/",
            "node_modules/",
            "dist/",
            "coverage/",
            "__pycache__/",
            ".venv/",
            "venv/",
        )
        if rel.startswith(prefixes):
            return True
        parts = rel.lower().split("/")
        if any(segment.startswith(".temp-") for segment in parts):
            return True
        if any(
            segment in parts
            for segment in (
                "node_modules",
                ".next",
                ".docusaurus",
                "compiled",
                "vendor",
                "vendors",
                "datatables",
                "dist",
                "coverage",
                "obj",
                "__pycache__",
                ".venv",
                "venv",
            )
        ):
            return True
        if "website" in parts and "build" in parts:
            return True
        if path.name.lower().endswith((".min.js", ".min.css")):
            return True
        if not include_tests:
            if "tests" in parts or "e2e" in parts or any(part.endswith(".test") for part in parts):
                return True
        return False

    def _language_for(self, path: Path) -> str | None:
        mapping = {
            ".py": "python",
            ".js": "javascript",
            ".ts": "typescript",
            ".tsx": "tsx",
            ".jsx": "jsx",
            ".cs": "csharp",
            ".sh": "shell",
            ".ps1": "powershell",
        }
        return mapping.get(path.suffix.lower())

    def _summarize(self, text: str, file_name: str, max_lines: int = 8) -> str:
        lines = [line.strip() for line in text.splitlines() if line.strip()][:max_lines]
        if not lines:
            return file_name
        return " | ".join(lines)[:400]

    def _extract_outline(self, text: str, language: str) -> list[tuple[str, str, int, str | None, bool]]:
        outlines: list[tuple[str, str, int, str | None, bool]] = []
        patterns: list[tuple[str, str]] = []
        if language == "python":
            return self._extract_python_outline(text)
        elif language in {"javascript", "typescript", "jsx", "tsx"}:
            ast_outline = self.frontend_ast.extract_outline(text, language)
            if ast_outline is not None:
                outlines.extend(ast_outline)
                for line_number, line in enumerate(text.splitlines(), start=1):
                    initializer = self._extract_js_initializer(line)
                    if initializer is not None:
                        outlines.append((initializer, "initializer", line_number, None, False))
                return outlines
            patterns = [
                (r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)", "class"),
                (r"^\s*(?:export\s+)?(?:default\s+)?function\s+([A-Za-z_][A-Za-z0-9_]*)", "function"),
                (r"^\s*function\s+([A-Za-z_][A-Za-z0-9_]*)", "function"),
                (r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:async\s*)?\(", "function"),
                (r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:async\s*)?[A-Za-z_][A-Za-z0-9_]*\s*=>", "function"),
            ]
        elif language == "csharp":
            return self._extract_csharp_outline(text)
        elif language == "css":
            return []

        for line_number, line in enumerate(text.splitlines(), start=1):
            for pattern, kind in patterns:
                match = re.match(pattern, line)
                if match:
                    symbol = match.group(1)
                    js_kind = kind
                    if language in {"javascript", "typescript", "jsx", "tsx"}:
                        if symbol.startswith("use") and len(symbol) > 3 and symbol[3:4].isupper():
                            js_kind = "hook"
                        elif symbol[:1].isupper() and symbol.endswith("Provider"):
                            js_kind = "context_provider"
                        elif symbol[:1].isupper():
                            js_kind = "component"
                    outlines.append((symbol, js_kind, line_number, None, False))
                    break
            if language in {"javascript", "typescript", "jsx", "tsx"}:
                initializer = self._extract_js_initializer(line)
                if initializer is not None:
                    outlines.append((initializer, "initializer", line_number, None, False))
        return outlines

    def _extract_csharp_outline(self, text: str) -> list[tuple[str, str, int, str | None, bool]]:
        outlines: list[tuple[str, str, int, str | None, bool]] = []
        namespace_name: str | None = None

        type_pattern = re.compile(
            r"^\s*(?:public|private|internal|protected|sealed|abstract|static|unsafe|new|file|readonly|partial|\s)*\b(partial\s+)?(class|interface|struct|record|enum)\s+([A-Za-z_][A-Za-z0-9_]*)"
        )
        method_pattern = re.compile(
            r"^\s*(?:public|private|internal|protected|static|virtual|override|abstract|async|sealed|extern|unsafe|new|partial|\s)+[A-Za-z_<>,\[\]?]+\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("
        )
        property_pattern = re.compile(
            r"^\s*(?:public|private|internal|protected|static|virtual|override|abstract|sealed|required|init|readonly|unsafe|new|\s)+[A-Za-z_<>,\[\]\.?]+\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{\s*(?:get;|set;|init;)"
        )
        field_pattern = re.compile(
            r"^\s*(?:public|private|internal|protected|static|readonly|const|volatile|unsafe|new|\s)+[A-Za-z_<>,\[\]\.?]+\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:=|;)"
        )
        namespace_pattern = re.compile(r"^\s*namespace\s+([A-Za-z_][A-Za-z0-9_\.]*)")

        current_type: str | None = None
        current_kind: str | None = None
        brace_depth = 0
        type_depth: int | None = None
        inside_enum = False

        for line_number, line in enumerate(text.splitlines(), start=1):
            opens = line.count("{")
            closes = line.count("}")

            ns_match = namespace_pattern.match(line)
            if ns_match:
                namespace_name = ns_match.group(1)

            type_match = type_pattern.match(line)
            if type_match:
                is_partial = bool(type_match.group(1)) or " partial " in f" {line} "
                kind = type_match.group(2)
                symbol = type_match.group(3)
                container = namespace_name
                outlines.append((symbol, kind, line_number, container, is_partial))
                current_type = symbol
                current_kind = kind
                type_depth = brace_depth + 1
                inside_enum = kind == "enum"

            method_match = method_pattern.match(line)
            if method_match and current_type is not None:
                symbol = method_match.group(1)
                outlines.append((symbol, "method", line_number, current_type, False))

            property_match = property_pattern.match(line)
            if property_match and current_type is not None and current_kind != "enum":
                symbol = property_match.group(1)
                outlines.append((symbol, "property", line_number, current_type, False))

            field_match = field_pattern.match(line)
            if field_match and current_type is not None and current_kind != "enum":
                symbol = field_match.group(1)
                outlines.append((symbol, "field", line_number, current_type, False))

            if inside_enum and current_type is not None:
                enum_member = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:=\s*[^,]+)?\s*,?\s*$", line)
                if enum_member:
                    symbol = enum_member.group(1)
                    if symbol not in {"public", "private", "internal", "protected"}:
                        outlines.append((symbol, "enum_member", line_number, current_type, False))

            brace_depth += opens
            brace_depth -= closes
            if type_depth is not None and brace_depth < type_depth - 1:
                current_type = None
                current_kind = None
                type_depth = None
                inside_enum = False

        return outlines

    def _extract_python_outline(self, text: str) -> list[tuple[str, str, int, str | None, bool]]:
        outlines: list[tuple[str, str, int, str | None, bool]] = []
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return outlines

        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                outlines.append((node.name, "class", node.lineno, None, False))
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        outlines.append((child.name, "method", child.lineno, node.name, False))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "function"
                if node.name.startswith("use") and len(node.name) > 3 and node.name[3:4].isupper():
                    kind = "hook"
                outlines.append((node.name, kind, node.lineno, None, False))

        return outlines

    def _extract_js_initializer(self, line: str) -> str | None:
        checks = [
            (r"document\.addEventListener\(\s*['\"]DOMContentLoaded['\"]", "document:DOMContentLoaded"),
            (r"\$\(document\)\.ready\s*\(", "jquery:ready"),
            (r"window\.addEventListener\(\s*['\"]load['\"]", "window:load"),
            (r"window\.addEventListener\(\s*['\"]resize['\"]", "window:resize"),
        ]
        for pattern, symbol in checks:
            if re.search(pattern, line):
                return symbol
        return None

    def _extract_edges(self, text: str, language: str) -> list[tuple[str, str]]:
        edges: list[tuple[str, str]] = []
        if language == "python":
            return self._extract_python_edges(text)

        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if language in {"javascript", "typescript", "jsx", "tsx"}:
                m = re.match(r"^import\s+.*?from\s+['\"]([^'\"]+)['\"]", stripped)
                if m:
                    edges.append((m.group(1), "import"))
                m = re.match(r"^const\s+.*?=\s*require\(\s*['\"]([^'\"]+)['\"]\s*\)", stripped)
                if m:
                    edges.append((m.group(1), "require"))
                m = re.search(r"import\(\s*['\"]([^'\"]+)['\"]\s*\)", stripped)
                if m:
                    edges.append((m.group(1), "dynamic_import"))
            elif language == "csharp":
                m = re.match(r"^using\s+([A-Za-z_][A-Za-z0-9_\.]*)\s*;", stripped)
                if m:
                    edges.append((m.group(1), "using"))
        # dedupe preserve order
        seen = set()
        result: list[tuple[str, str]] = []
        for edge in edges:
            if edge in seen:
                continue
            seen.add(edge)
            result.append(edge)
        return result

    def _extract_python_edges(self, text: str) -> list[tuple[str, str]]:
        edges: list[tuple[str, str]] = []
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return edges

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    edges.append((alias.name, "import"))
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level and module:
                    target = "." * node.level + module
                elif node.level:
                    target = "." * node.level
                else:
                    target = module
                if target:
                    edges.append((target, "import"))

        seen = set()
        result: list[tuple[str, str]] = []
        for edge in edges:
            if edge in seen:
                continue
            seen.add(edge)
            result.append(edge)
        return result

    def _resolve_edge_to_paths(
        self,
        project_root: Path,
        source_path: str,
        target: str,
        kind: str,
        limit: int = 20,
    ) -> list[str]:
        candidates: list[str] = []
        source_abs = project_root / source_path

        if kind in {"import", "require", "dynamic_import"} and target.startswith("."):
            base = (source_abs.parent / target).resolve()
            candidates.extend(self._existing_relative_candidates(project_root, base))
        elif kind == "import":
            module_base = project_root / target.replace(".", "/")
            candidates.extend(self._existing_relative_candidates(project_root, module_base, python_only=True))
        elif kind == "using":
            with self.connect(project_root) as conn:
                rows = conn.execute(
                    "SELECT DISTINCT path FROM code_outlines WHERE container = ? ORDER BY path LIMIT ?",
                    (target, limit),
                ).fetchall()
            candidates.extend([row["path"] for row in rows])

        # preserve order + uniqueness
        seen = set()
        resolved: list[str] = []
        for item in candidates:
            if item not in seen:
                seen.add(item)
                resolved.append(item)
        return resolved[:limit]

    def _existing_relative_candidates(self, project_root: Path, base: Path, python_only: bool = False) -> list[str]:
        options: list[Path] = []
        if base.suffix:
            options.append(base)
        else:
            if python_only:
                options.extend([base.with_suffix(".py"), base / "__init__.py"])
            else:
                options.extend(
                    [
                        base,
                        base.with_suffix(".js"),
                        base.with_suffix(".ts"),
                        base.with_suffix(".jsx"),
                        base.with_suffix(".tsx"),
                        base.with_suffix(".py"),
                        base.with_suffix(".cs"),
                        base / "index.js",
                        base / "index.ts",
                        base / "__init__.py",
                    ]
                )

        result = []
        for option in options:
            try:
                resolved = option.resolve()
            except FileNotFoundError:
                continue
            if resolved.exists() and resolved.is_file():
                try:
                    result.append(resolved.relative_to(project_root).as_posix())
                except ValueError:
                    continue
        return result

    def _get_file_stub(self, project_root: Path, path: str) -> dict[str, str | int] | None:
        with self.connect(project_root) as conn:
            row = conn.execute(
                "SELECT path, language, line_count, summary, role FROM code_files WHERE path = ? LIMIT 1",
                (path,),
            ).fetchone()
        if row is None:
            return None
        return {
            "path": row["path"],
            "language": row["language"],
            "line_count": int(row["line_count"]),
            "summary": row["summary"],
            "role": row["role"] or "unknown",
        }

    def _is_indexed_file(self, project_root: Path, path: str) -> bool:
        return self._get_file_stub(project_root, path) is not None

    def _file_exists(self, project_root: Path, path: str) -> bool:
        return (project_root / path).is_file()

    def _extract_relevant_files(self, sections: dict[str, list[str]]) -> list[str]:
        results: list[str] = []
        for line in sections.get("Relevant Files", []):
            stripped = line.strip()
            if not stripped.startswith("-"):
                continue
            candidate = stripped[1:].strip().strip('`')
            if not candidate or candidate.startswith('/.MEMORY/'):
                continue
            results.append(candidate)
        seen = set()
        ordered = []
        for item in results:
            if item not in seen:
                seen.add(item)
                ordered.append(item)
        return ordered

    def _score_text_match(
        self,
        needle: str,
        candidate: str,
        exact: int,
        prefix: int,
        contains: int,
        reasons: list[str] | None = None,
        label: str | None = None,
    ) -> int:
        if not candidate:
            return 0
        n = needle.lower()
        c = candidate.lower()
        if c == n:
            if reasons is not None and label is not None:
                reasons.append(f"{label}:exact")
            return exact
        if c.startswith(n):
            if reasons is not None and label is not None:
                reasons.append(f"{label}:prefix")
            return prefix
        if n in c:
            if reasons is not None and label is not None:
                reasons.append(f"{label}:contains")
            return contains
        return 0

    def _trace_confidence(self, matches: list[dict[str, object]]) -> str:
        if len(matches) >= 4:
            return "high"
        if len(matches) >= 2:
            return "medium"
        return "low"

    def _trace_summary(self, matches: list[dict[str, object]]) -> list[str]:
        if not matches:
            return ["matches:0"]
        layers: dict[str, int] = {}
        for item in matches:
            layer = str(item.get("layer") or "unknown")
            layers[layer] = layers.get(layer, 0) + 1
        summary = [f"matches:{len(matches)}"]
        summary.extend(f"layer:{layer}:{count}" for layer, count in sorted(layers.items()))
        return summary

    def _concept_variants(self, concept: str) -> list[str]:
        raw = concept.strip()
        if not raw:
            return []

        variants = {raw, raw.lower()}
        suffixes = ("Dto", "Model", "ViewModel", "Entity", "Service", "Controller", "Settings", "Options", "Request", "Response", "Id")

        if raw.endswith("s") and len(raw) > 3:
            variants.add(raw[:-1])
        else:
            variants.add(raw + "s")

        for suffix in suffixes:
            if raw.endswith(suffix) and len(raw) > len(suffix):
                variants.add(raw[: -len(suffix)])
            variants.add(raw + suffix)

        if raw.startswith("Is") and len(raw) > 2:
            variants.add(raw[2:])
        else:
            variants.add("Is" + raw[:1].upper() + raw[1:])

        if raw.startswith("Has") and len(raw) > 3:
            variants.add(raw[3:])
        else:
            variants.add("Has" + raw[:1].upper() + raw[1:])

        return [item for item in variants if item]

    def _path_weight(self, project_root: Path, path: str) -> int:
        lower = path.lower()
        score = 0
        positive_tokens = (
            "/src/",
            "/app/",
            "/web/",
            "/components/",
            "/services/",
            "/controllers/",
            "/models/",
            "/domain/",
            "/infrastructure/",
            "/application/",
        )
        negative_tokens = (
            "/test/",
            "/tests/",
            "/fixture/",
            "/fixtures/",
            "/mock/",
            "/mocks/",
            "/example/",
            "/examples/",
            "/template/",
            "/templates/",
            "/generated/",
            "/snapshot/",
            "/assets/",
            "/pwaassets/",
            "/wwwroot/lib/",
            "/static/",
        )
        for token in positive_tokens:
            if token in lower:
                score += 20
        for token in negative_tokens:
            if token in lower:
                score -= 35
        hints = self._load_indexing_hints(project_root)
        for root in hints["preferred_roots"]:
            if lower.startswith(root):
                score += 40
        for root in hints["avoid_roots"]:
            if lower.startswith(root):
                score -= 60
        return score

    def _load_indexing_hints(self, project_root: Path) -> dict[str, list[str]]:
        cache_key = str(project_root)
        if cache_key in self._indexing_hint_cache:
            return self._indexing_hint_cache[cache_key]

        hints = {"preferred_roots": [], "avoid_roots": []}
        config_path = project_root / ".MEMORY" / "config" / "indexing.md"
        if not config_path.is_file():
            self._indexing_hint_cache[cache_key] = hints
            return hints

        current = None
        for raw in config_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if line.startswith("## "):
                current = line[3:].strip().lower()
                continue
            if not line.startswith("-"):
                continue
            value = line[1:].strip().strip('`').replace('\\', '/').strip().lower().lstrip('/')
            if not value:
                continue
            if current == "preferred roots":
                hints["preferred_roots"].append(value)
            elif current == "avoid roots":
                hints["avoid_roots"].append(value)

        self._indexing_hint_cache[cache_key] = hints
        return hints

    def _ensure_parsed_candidates(self, project_root: Path, query: str, limit: int = 100) -> int:
        needle = query.strip()
        if not needle:
            return 0
        pattern = f"%{needle}%"
        with self.connect(project_root) as conn:
            rows = conn.execute(
                """
                SELECT path
                FROM code_files
                WHERE parsed = 0 AND (path LIKE ? OR summary LIKE ?)
                ORDER BY path ASC
                LIMIT ?
                """,
                (pattern, pattern, limit),
            ).fetchall()
        paths = [row["path"] for row in rows]
        if not paths:
            return 0
        return self.sync_code_files(project_root, paths=paths)

    def _infer_layer_from_path(self, path: str) -> str:
        lower = path.lower()
        if any(token in lower for token in ("dto", "viewmodel", "model", "entity")):
            return "data"
        if any(token in lower for token in ("controller", "api", "endpoint", "route")):
            return "api"
        if any(token in lower for token in ("service", "handler", "manager", "provider")):
            return "logic"
        if any(token in lower for token in ("component", "page", "view", "form", "dialog", "screen")):
            return "ui"
        return "code"

    def _infer_code_role(
        self,
        project_root: Path,
        path: str,
        language: str,
        outlines: list[tuple[str, str, int, str | None, bool]],
    ) -> str | None:
        lower = path.lower()
        parts = lower.split("/")
        name = Path(path).stem.lower()
        kinds = {item[1] for item in outlines}
        if language in {"jsx", "tsx"}:
            if lower.endswith("/page.tsx") or lower.endswith("/page.jsx"):
                return "page"
            if "pages" in parts:
                return "page"
            if lower.endswith("/layout.tsx") or lower.endswith("/layout.jsx"):
                return "layout"
            if name.endswith("provider") or name == "providers":
                return "context-provider"
            if "hooks" in parts:
                return "hook-module"
            if "context_provider" in kinds:
                return "context-provider"
            if "hook" in kinds and kinds <= {"hook"}:
                return "hook-module"
            if "component" in kinds:
                return "component"
            if "components" in parts:
                return "component"
            if self._looks_like_component_name(Path(path).stem):
                return "component"
        if language in {"javascript", "typescript"}:
            plugin_role = self._infer_plugin_structure_role(project_root, path)
            if plugin_role is not None:
                return plugin_role
            if "wwwroot" in parts and "js" in parts:
                return "asset-script"
            if len(parts) == 1 and name in {"bootstrap", "generate", "setup", "setup-app", "verify-test-suite"}:
                return "script"
            if "framework-generators" in parts:
                return "framework-generator"
            if "core" in parts:
                return "core-module"
            if lower.endswith("/route.ts") or lower.endswith("/route.js"):
                return "route-handler"
            if name == "middleware" or lower.endswith("/middleware.ts") or lower.endswith("/middleware.js"):
                return "middleware"
            if lower.endswith((".config.js", ".config.ts", ".config.mjs", ".config.cjs")) or any(token in parts for token in ("schemas", "schema")):
                return "config-module"
            if lower.endswith("next-env.d.ts") or name in {"next-env", "sidebars"}:
                return "config-module"
            if any(token in parts for token in ("scripts", "bin", "cli")):
                return "script"
            if any(token in parts for token in ("lib", "utils", "helpers")):
                return "utility-module"
            if any(token in parts for token in ("server", "runtime")):
                return "server-module"
            if any(token in parts for token in ("prisma", "db", "database")):
                return "data-access"
            if "hooks" in parts and (name.startswith("use") or "hook" in name):
                return "hook-module"
            if any(token in parts for token in ("components", "features")) and language == "typescript" and name == "index":
                return "barrel-module"
            if name in {"types", "type", "storage", "registry", "constants", "page-key", "evidence"}:
                return "utility-module"
            if "initializer" in kinds:
                return "initializer-module"
            if "hook" in kinds and kinds <= {"hook"}:
                return "hook-module"
        if language == "csharp":
            logical_name = name.split(".", 1)[0]
            if lower.endswith(".cshtml.cs"):
                return "page-model"
            if "pages" in parts and name.endswith("model"):
                return "page-model"
            if "pages" in parts and logical_name.endswith("pagebase"):
                return "page-model"
            if "dto" in parts or "dtos" in parts:
                return "data-model"
            if "entities" in parts:
                return "data-model"
            if "enums" in parts:
                return "data-model"
            if "interfaces" in parts:
                return "abstraction"
            if lower.endswith("program.cs"):
                return "initializer-module"
            if lower.endswith("dependencyinjection.cs"):
                return "initializer-module"
            if name.endswith("dbcontext") or "dbcontext" in name:
                return "data-access"
            if "seeding" in parts or logical_name.startswith("seed"):
                return "script"
            if "hubs" in parts or name.endswith("hub"):
                return "hub"
            if "viewcomponents" in parts or name.endswith("viewcomponent"):
                return "component"
            if "authorization" in parts or name.endswith(("handler", "provider", "requirement", "attribute")):
                return "policy"
            if name.endswith("controller"):
                return "controller"
            if logical_name.endswith(("service", "renderer", "sanitizer", "processor", "provider")):
                return "service"
            if name.endswith("policy"):
                return "policy"
            if logical_name.endswith(("converter", "binder")):
                return "configuration"
            if name.endswith(("configuration", "config")) or any(token in parts for token in ("configurations", "entityconfigurations", "mapping")):
                return "configuration"
            if name.endswith(("validator", "validation")):
                return "validator"
            if name.endswith(("repository", "store")):
                return "repository"
            if name.endswith(("middleware", "filter")):
                return "middleware"
            if logical_name.endswith(("resources", "map", "ids")):
                return "utility"
            if name.endswith(("helper", "utilities", "utility", "extensions")) or any(token in parts for token in ("helpers", "utils", "extensions")):
                return "utility"
            if name.endswith(("job", "worker", "task")) or any(token in parts for token in ("workers", "jobs", "background")):
                return "worker"
            if name.endswith(("dto", "model", "viewmodel", "entity")):
                return "data-model"
        if language == "python":
            if lower.endswith("__init__.py"):
                return "module-init"
            if any(token in parts for token in ("scripts", "bin", "cli", "tools")):
                return "script"
            if any(token in parts for token in ("utils", "helpers")):
                return "utility-module"
        if language == "powershell":
            return "script"
        if language == "shell":
            return "script"
        return None

    def _infer_plugin_structure_role(self, project_root: Path, path: str) -> str | None:
        rel_path = Path(path)
        name = rel_path.stem.lower()
        suffix = rel_path.suffix.lower()
        if suffix not in {".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs"}:
            return None
        candidate = rel_path.parent
        checked = 0
        while True:
            if checked > 4:
                break
            abs_candidate = project_root / candidate
            if not abs_candidate.exists():
                break
            has_package = (abs_candidate / "package.json").is_file()
            has_templates = (abs_candidate / "templates").is_dir()
            has_prisma = (abs_candidate / "prisma").is_dir()
            has_generator = any((abs_candidate / f"generator{ext}").is_file() for ext in (".js", ".ts", ".mjs", ".cjs"))
            marker_count = sum(1 for flag in (has_package, has_templates, has_prisma, has_generator) if flag)
            if marker_count >= 2 or (has_package and (has_templates or has_generator or has_prisma)):
                if name == "generator":
                    return "plugin-generator"
                if name == "index":
                    return "plugin-module"
                if "hooks" in rel_path.parts and (name.startswith("use") or "hook" in name):
                    return "hook-module"
                if "components" in rel_path.parts or self._looks_like_component_name(rel_path.stem):
                    return "component"
                if "middleware" in rel_path.parts:
                    return "middleware"
                if "templates" in rel_path.parts:
                    return "plugin-template-module"
                if name in {"types", "type", "storage", "registry", "constants", "page-key", "evidence"}:
                    return "utility-module"
                return None
            if candidate == Path(".") or candidate == candidate.parent:
                break
            candidate = candidate.parent
            checked += 1
        return None

    def _looks_like_component_name(self, name: str) -> bool:
        return bool(name and name[0].isupper() and any(ch.isupper() for ch in name[1:]))

    def _role_group(self, role: str) -> str:
        if role in {"component", "context-provider", "hook-module", "page", "layout", "asset-script"}:
            return "frontend"
        if role in {"controller", "route-handler", "page-model", "hub"}:
            return "request-surfaces"
        if role in {"service", "policy", "repository", "validator", "middleware", "worker", "server-module", "core-module"}:
            return "logic-runtime"
        if role in {"data-model", "data-access"}:
            return "data"
        if role in {"initializer-module", "module-init", "config-module", "script", "utility", "utility-module", "configuration", "plugin-generator", "plugin-module", "plugin-template-module", "framework-generator", "barrel-module", "abstraction"}:
            return "support"
        return "unknown"

    def _layer_rank(self, layer: str) -> int:
        order = {
            "data": 0,
            "logic": 1,
            "api": 2,
            "ui": 3,
            "code": 4,
        }
        return order.get(layer, 9)

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        existing = {row[1] for row in rows}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def _extract_snippet(self, text: str, language: str, start_line: int) -> str:
        lines = text.splitlines()
        index = max(0, start_line - 1)
        if index >= len(lines):
            return ""

        if language == "python":
            return self._extract_indent_block(lines, index)
        if language in {"javascript", "typescript", "jsx", "tsx", "csharp"}:
            return self._extract_brace_block(lines, index)
        return "\n".join(lines[index : min(len(lines), index + 20)]).rstrip() + "\n"

    def _extract_indent_block(self, lines: list[str], index: int) -> str:
        start = index
        base_indent = len(lines[start]) - len(lines[start].lstrip())
        end = start + 1
        while end < len(lines):
            line = lines[end]
            if not line.strip():
                end += 1
                continue
            indent = len(line) - len(line.lstrip())
            if indent <= base_indent:
                break
            end += 1
        return "\n".join(lines[start:end]).rstrip() + "\n"

    def _extract_brace_block(self, lines: list[str], index: int) -> str:
        start = index
        brace_balance = 0
        seen_open = False
        end = start
        while end < len(lines):
            line = lines[end]
            brace_balance += line.count("{")
            brace_balance -= line.count("}")
            if line.count("{") > 0:
                seen_open = True
            if seen_open and brace_balance <= 0:
                end += 1
                break
            if not seen_open and end > start and line.strip().endswith(";"):
                end += 1
                break
            end += 1
        return "\n".join(lines[start:end]).rstrip() + "\n"
