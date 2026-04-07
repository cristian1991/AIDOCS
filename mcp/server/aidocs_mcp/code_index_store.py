from __future__ import annotations

import os
import re
import sqlite3
import time
from pathlib import Path


def parse_modified_since(value: str | None) -> int | None:
    """Parse a modified_since shortcut into nanosecond timestamp.

    Accepts: "today", "1h", "2h", "24h", "1d", "7d", "30d",
    or an ISO datetime string (e.g. "2026-04-05T10:00:00").
    Returns None if value is None/empty.
    """
    if not value:
        return None
    v = value.strip().lower()
    now_ns = time.time_ns()
    shortcuts: dict[str, int] = {
        "1h": 3_600,
        "2h": 7_200,
        "6h": 21_600,
        "12h": 43_200,
        "24h": 86_400,
        "1d": 86_400,
        "2d": 172_800,
        "7d": 604_800,
        "30d": 2_592_000,
    }
    if v in shortcuts:
        return now_ns - shortcuts[v] * 1_000_000_000
    if v == "today":
        from datetime import datetime, timezone
        midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        return int(midnight.timestamp() * 1_000_000_000)
    # Try ISO datetime
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(value.strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1_000_000_000)
    except ValueError:
        return None

from .code_index_analysis_service import CodeIndexAnalysisService
from .code_index_modules_service import CodeIndexModulesService
from .code_index_symbol_search_service import CodeIndexSymbolSearchService
from .code_index_sync_service import CodeIndexSyncService
from .code_index_trace_surfaces_service import CodeIndexTraceSurfacesService
from .code_index_route_query_service import CodeIndexRouteQueryService
from .code_index_hotspot_service import CodeIndexHotspotService
from .code_index_bundle_service import CodeIndexBundleService
from .code_index_utility_service import CodeIndexUtilityService
from .code_index_outline_service import CodeIndexOutlineService
from .code_index_edge_service import CodeIndexEdgeService
from .code_index_inference_service import CodeIndexInferenceService
from .frontend_ast import FrontendAstExtractor
from .language_descriptors import language_for_builtin_descriptor, language_for_custom_descriptor, layer_from_descriptor, load_index_config, module_hints_from_descriptors, role_from_descriptor
from .session_store import SessionStore


class CodeIndexStore:
    """Derived SQLite index for repository code files and lightweight summaries."""

    INDEX_VERSION = "code-index-v7"

    def __init__(self, session_store: SessionStore | None = None) -> None:
        self.session_store = session_store
        self.frontend_ast = FrontendAstExtractor()
        self._modules = CodeIndexModulesService(self)
        self._sync = CodeIndexSyncService(self)
        self._analysis = CodeIndexAnalysisService(self)
        self._symbols = CodeIndexSymbolSearchService(self)
        self._trace_surfaces = CodeIndexTraceSurfacesService(self)
        self._route_queries = CodeIndexRouteQueryService(self)
        self._hotspots = CodeIndexHotspotService(self)
        self._bundles = CodeIndexBundleService(self)
        self._utility = CodeIndexUtilityService(self)
        self._outlines = CodeIndexOutlineService(self)
        self._edges = CodeIndexEdgeService(self)
        self._inference = CodeIndexInferenceService(self)
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
        self._last_project_root = project_root
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
                    language_tier TEXT,
                    language_source TEXT,
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

                CREATE TABLE IF NOT EXISTS code_modules (
                    module_path TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    stack TEXT,
                    entry_point TEXT,
                    file_count INTEGER NOT NULL DEFAULT 0,
                    description TEXT
                );

                """
            )
            self._ensure_column(conn, "code_files", "role", "TEXT")
            self._ensure_column(conn, "code_files", "size_bytes", "INTEGER")
            self._ensure_column(conn, "code_files", "mtime_ns", "INTEGER")
            self._ensure_column(conn, "code_files", "parsed", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "code_files", "language_tier", "TEXT")
            self._ensure_column(conn, "code_files", "language_source", "TEXT")
            self._ensure_column(conn, "code_files", "module", "TEXT")
            self._ensure_column(conn, "code_outlines", "container", "TEXT")
            self._ensure_column(conn, "code_outlines", "is_partial", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_index_version(conn)

    def _ensure_index_version(self, conn: sqlite3.Connection) -> None:
        row = conn.execute("SELECT value FROM index_meta WHERE key = 'code_index_version'").fetchone()
        current = row["value"] if row else None
        if current == self.INDEX_VERSION:
            return

        # Incremental migration: keep file metadata, invalidate parsed state.
        # Outlines and edges will be repopulated on next sync_code_files.
        conn.execute("UPDATE code_files SET parsed = 0")
        conn.execute("DELETE FROM code_outlines")
        conn.execute("DELETE FROM code_edges")
        conn.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES ('code_index_version', ?)",
            (self.INDEX_VERSION,),
        )

    # ── Module detection ────────────────────────────────────────────────

    # Fallback manifest files (overridden by _index_config.toml module_manifests)
    _MODULE_MANIFESTS_DEFAULT: dict[str, str] = {
        "package.json": "javascript",
        "Cargo.toml": "rust",
        "pyproject.toml": "python",
        "setup.py": "python",
        "go.mod": "go",
        "pom.xml": "java",
        "build.gradle": "java",
        "build.gradle.kts": "kotlin",
        "mix.exs": "elixir",
        "Gemfile": "ruby",
        "composer.json": "php",
    }

    # Fallback entry points (overridden by per-language descriptor entry_points)
    _ENTRY_POINT_PATTERNS_DEFAULT: list[tuple[str, str]] = [
        ("index.ts", "typescript"),
        ("index.js", "javascript"),
        ("index.tsx", "tsx"),
        ("index.jsx", "jsx"),
        ("main.py", "python"),
        ("__init__.py", "python"),
        ("main.rs", "rust"),
        ("lib.rs", "rust"),
        ("main.go", "go"),
        ("mod.rs", "rust"),
        ("Program.cs", "csharp"),
        ("Startup.cs", "csharp"),
        ("server.js", "javascript"),
        ("server.ts", "typescript"),
        ("app.js", "javascript"),
        ("app.ts", "typescript"),
        ("app.py", "python"),
    ]

    # Fallback skip dirs (overridden by _index_config.toml module_detection.skip_dirs)
    _MODULE_SKIP_DIRS_DEFAULT: set[str] = {
        "node_modules", "dist", "build", "coverage", "obj", "bin",
        "__pycache__", ".venv", "venv", ".git", ".MEMORY", ".opencode",
        ".claude", ".github", ".next", ".docusaurus", "vendor", "vendors",
        "tmp", "temp", ".BACKUP", ".backups", "compiled", "target",
    }

    @property
    def _MODULE_MANIFESTS(self) -> dict[str, str]:
        return self._modules._MODULE_MANIFESTS

    @property
    def _MODULE_SKIP_DIRS(self) -> set[str]:
        return self._modules._MODULE_SKIP_DIRS

    def _get_entry_point_patterns(self, project_root: Path) -> list[tuple[str, str]]:
        return self._modules._get_entry_point_patterns(project_root)

    def detect_modules(self, project_root: Path) -> list[dict[str, str | int | None]]:
        return self._modules.detect_modules(project_root)

    def sync_modules(self, project_root: Path) -> int:
        return self._modules.sync_modules(project_root)

    def get_modules(self, project_root: Path, kind: str | None = None) -> list[dict[str, object]]:
        return self._modules.get_modules(project_root, kind=kind)

    def get_module_files(self, project_root: Path, module_path: str, limit: int = 200, modified_since_ns: int | None = None) -> list[dict[str, object]]:
        return self._modules.get_module_files(project_root, module_path, limit=limit, modified_since_ns=modified_since_ns)



    def sync_code_manifest(self, project_root: Path, include_tests: bool = False) -> int:
        return self._sync.sync_code_manifest(project_root, include_tests=include_tests)

    def sync_code_files(
        self,
        project_root: Path,
        paths: list[str] | None = None,
        include_tests: bool = False,
    ) -> int:
        return self._sync.sync_code_files(project_root, paths=paths, include_tests=include_tests)

    def sync_session_code(self, project_root: Path, session_id: str, include_tests: bool = False) -> int:
        return self._sync.sync_session_code(project_root, session_id, include_tests=include_tests)

    def code_status(self, project_root: Path) -> dict[str, object]:
        return self._sync.code_status(project_root)

    def _code_freshness(self, project_root: Path) -> dict[str, object]:
        return self._sync._code_freshness(project_root)



    def search_code(self, project_root: Path, query: str, limit: int = 10, modified_since_ns: int | None = None) -> list[dict[str, str | int]]:
        self.init_db(project_root)
        needle = query.strip()
        if not needle and modified_since_ns is None:
            return []
        mtime_filter = ""
        params: list[object] = []
        if needle:
            pattern = f"%{needle}%"
            where = "path LIKE ? OR summary LIKE ?"
            params = [pattern, pattern]
        else:
            where = "1=1"
        if modified_since_ns is not None:
            mtime_filter = " AND mtime_ns >= ?"
            params.append(modified_since_ns)
        with self.connect(project_root) as conn:
            rows = conn.execute(
                f"""
                SELECT path, language, language_tier, language_source, line_count, summary, role
                FROM code_files
                WHERE ({where}){mtime_filter}
                LIMIT 250
                """,
                params,
            ).fetchall()
        ranked = []
        for row in rows:
            score = 0
            reasons: list[str] = []
            if needle:
                score += self._score_text_match(needle, row["path"], exact=120, prefix=90, contains=60, reasons=reasons, label="path")
                score += self._score_text_match(needle, row["summary"], exact=40, prefix=25, contains=15, reasons=reasons, label="summary")
            path_weight = self._path_weight(project_root, str(row["path"]))
            score += path_weight
            if path_weight:
                reasons.append(f"path_weight:{path_weight}")
            tier = str(row["language_tier"] or "unknown")
            tier_weight = {"rich": 10, "heuristic": 3, "summary": 1}.get(tier, 0)
            score += tier_weight
            if tier_weight:
                reasons.append(f"tier_weight:{tier_weight}")
            score -= row["path"].count("/")
            if modified_since_ns is not None:
                reasons.append("mtime_filter")
            ranked.append((score, row, reasons))
        ranked.sort(key=lambda item: (-item[0], item[1]["path"]))
        return [
            {
                "path": row["path"],
                "language": row["language"],
                "language_tier": row["language_tier"] or "unknown",
                "language_source": row["language_source"] or "unknown",
                "line_count": int(row["line_count"]),
                "summary": row["summary"],
                "role": row["role"] or "unknown",
                "why": reasons,
            }
            for _, row, reasons in ranked[:limit]
        ]

    def is_file_stale(self, project_root: Path, path: str) -> bool:
        """Check if a file has been modified since last index sync."""
        self.init_db(project_root)
        rel = path.replace("\\", "/").strip()
        abs_path = project_root / rel
        if not abs_path.is_file():
            return False
        with self.connect(project_root) as conn:
            row = conn.execute(
                "SELECT mtime_ns, size_bytes FROM code_files WHERE path = ?", (rel,)
            ).fetchone()
        if row is None:
            # Not indexed yet — not stale, just unknown
            return False
        try:
            stat = abs_path.stat()
            return int(stat.st_mtime_ns) != int(row["mtime_ns"]) or int(stat.st_size) != int(row["size_bytes"])
        except Exception:
            return False


    def search_text(
        self,
        project_root: Path,
        text: str,
        *,
        glob: str | None = None,
        case_sensitive: bool = False,
        regex: bool = False,
        limit: int = 50,
        include_tests: bool = False,
    ) -> list[dict[str, object]]:
        """Search indexed file contents. Literal by default, | or ' OR ' splits into multiple terms. Set regex=True for pattern matching."""
        self.init_db(project_root)
        raw = text.strip()
        if not raw:
            return []

        import re as _re

        if regex:
            try:
                pattern = _re.compile(raw, 0 if case_sensitive else _re.IGNORECASE)
            except _re.error:
                return []
            needles = None
        else:
            # Split on | or ' OR ' for multi-term literal search
            split_text = raw.replace(" OR ", "|").replace(" or ", "|")
            needles = [t.strip() for t in split_text.split("|") if t.strip()]
            if not needles:
                return []
            if not case_sensitive:
                needles = [n.lower() for n in needles]
            pattern = None

        with self.connect(project_root) as conn:
            query_sql = "SELECT path FROM code_files"
            params: list[object] = []
            if not include_tests:
                query_sql += " WHERE (role IS NULL OR role NOT IN ('test', 'fixture'))"
            rows = conn.execute(query_sql, params).fetchall()

        import fnmatch
        matches: list[dict[str, object]] = []
        for row in rows:
            rel_path = str(row["path"])
            if glob and not fnmatch.fnmatch(rel_path, glob):
                continue
            abs_path = project_root / rel_path
            if not abs_path.is_file():
                continue
            try:
                content = abs_path.read_text(encoding="utf-8")
            except Exception:
                continue
            search_content = content if case_sensitive else content.lower()
            if regex:
                if not pattern.search(content if case_sensitive else search_content):
                    continue
            else:
                if not any(n in search_content for n in needles):
                    continue
            lines_matched: list[dict[str, object]] = []
            for i, line in enumerate(content.splitlines(), 1):
                check_line = line if case_sensitive else line.lower()
                if regex:
                    hit = bool(pattern.search(line if case_sensitive else check_line))
                else:
                    hit = any(n in check_line for n in needles)
                if hit:
                    lines_matched.append({"line_number": i, "line": line.rstrip()})
                    if len(lines_matched) >= 5:
                        break
            if regex:
                total_count = len(pattern.findall(content if case_sensitive else search_content))
            else:
                total_count = sum(search_content.count(n) for n in needles)
            matches.append({
                "path": rel_path,
                "match_count": total_count,
                "lines": lines_matched,
            })
            if len(matches) >= limit:
                break

        return matches


    def preview_extraction_deps(
        self,
        project_root: Path,
        path: str,
        start_line: int,
        end_line: int,
    ) -> dict[str, object]:
        return self._analysis.preview_extraction_deps(project_root, path, start_line, end_line)

    def _preview_deps_python(
        self, path: str, full_text: str, block_text: str, outside_text: str,
        start_line: int, end_line: int,
    ) -> dict[str, object]:
        return self._analysis._preview_deps_python(path, full_text, block_text, outside_text, start_line, end_line)

    def _preview_deps_js(
        self, path: str, full_text: str, block_text: str, outside_text: str,
        start_line: int, end_line: int,
    ) -> dict[str, object]:
        return self._analysis._preview_deps_js(path, full_text, block_text, outside_text, start_line, end_line)

    def find_symbol_range(
        self,
        project_root: Path,
        path: str,
        symbol: str,
        kind: str | None = None,
        line_number: int | None = None,
    ) -> dict[str, object]:
        return self._analysis.find_symbol_range(project_root, path, symbol, kind=kind, line_number=line_number)

    def suggest_extractions(
        self,
        project_root: Path,
        path: str,
        min_lines: int = 20,
        limit: int = 10,
    ) -> list[dict[str, object]]:
        return self._analysis.suggest_extractions(project_root, path, min_lines=min_lines, limit=limit)

    def find_stale_references(
        self,
        project_root: Path,
        symbols: list[str],
        *,
        exclude_path: str | None = None,
        include_tests: bool = False,
        limit: int = 50,
    ) -> list[dict[str, object]]:
        return self._analysis.find_stale_references(project_root, symbols, exclude_path=exclude_path, include_tests=include_tests, limit=limit)

    def find_dead_code(
        self,
        project_root: Path,
        path: str,
    ) -> dict[str, object]:
        return self._analysis.find_dead_code(project_root, path)



    def search_symbols(
        self,
        project_root: Path,
        query: str,
        kind: str | None = None,
        role: str | None = None,
        limit: int = 25,
        modified_since_ns: int | None = None,
    ) -> list[dict[str, str | int | bool | None]]:
        return self._symbols.search_symbols(project_root, query, kind=kind, role=role, limit=limit, modified_since_ns=modified_since_ns)

    def get_method_signature(
        self,
        project_root: Path,
        method_name: str,
        container: str | None = None,
        limit: int = 20,
    ) -> dict[str, object]:
        return self._symbols.get_method_signature(project_root, method_name, container=container, limit=limit)

    def get_method_signatures(
        self,
        project_root: Path,
        methods: list[str],
        container: str | None = None,
        limit_per_method: int = 20,
    ) -> dict[str, object]:
        return self._symbols.get_method_signatures(project_root, methods, container=container, limit_per_method=limit_per_method)

    def get_enum_values(
        self,
        project_root: Path,
        enum_name: str,
        limit: int = 50,
    ) -> dict[str, object]:
        return self._symbols.get_enum_values(project_root, enum_name, limit=limit)

    def get_constructor_params(
        self,
        project_root: Path,
        type_name: str,
        limit: int = 20,
        include_related: bool = False,
    ) -> dict[str, object]:
        return self._symbols.get_constructor_params(project_root, type_name, limit=limit, include_related=include_related)

    def get_constructor_params_batch(
        self,
        project_root: Path,
        types: list[str],
        include_related: bool = False,
        limit_per_type: int = 20,
    ) -> dict[str, object]:
        return self._symbols.get_constructor_params_batch(project_root, types, include_related=include_related, limit_per_type=limit_per_type)

    def get_service_api(
        self,
        project_root: Path,
        service_name: str,
        limit: int = 100,
    ) -> dict[str, object]:
        return self._symbols.get_service_api(project_root, service_name, limit=limit)

    def get_entity_properties(
        self,
        project_root: Path,
        entity_name: str,
    ) -> dict[str, object]:
        return self._symbols.get_entity_properties(project_root, entity_name)

    def find_references(self, project_root: Path, symbol: str, limit: int = 100) -> dict[str, object]:
        return self._symbols.find_references(project_root, symbol, limit=limit)



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
                "container": item.get("container"),
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
                    "kind": item.get("kind") or item.get("field_kind", ""),
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

            # Only fetch snippets for high-scoring method/property matches to prevent output overflow
            snippet = None
            if score >= 60 and kind in ("method", "property", "field"):
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
                    "container": item.get("container"),
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
            # Truncate file summaries to prevent oversized results
            summary = str(item["summary"] or "")
            if len(summary) > 300:
                summary = summary[:300] + "..."
            merged.append(
                {
                    "score": score,
                    "path": path,
                    "layer": self._infer_layer_from_path(path),
                    "symbol": None,
                    "kind": "file_match",
                    "line_number": None,
                    "container": None,
                    "snippet": summary,
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
                    "container": item.get("container"),
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
        return self._trace_surfaces.find_mutation_points(project_root, concept, limit=limit)

    def find_validation_surfaces(self, project_root: Path, concept: str, limit: int = 50) -> dict[str, object]:
        return self._trace_surfaces.find_validation_surfaces(project_root, concept, limit=limit)

    def find_async_boundaries(self, project_root: Path, concept: str, limit: int = 50) -> dict[str, object]:
        return self._trace_surfaces.find_async_boundaries(project_root, concept, limit=limit)

    def find_hotspots(self, project_root: Path, query: str | None = None, limit: int = 30) -> dict[str, object]:
        return self._hotspots.find_hotspots(project_root, query=query, limit=limit)

    def find_query_hotspots(self, project_root: Path, query: str | None = None, limit: int = 30) -> dict[str, object]:
        return self._hotspots.find_query_hotspots(project_root, query=query, limit=limit)

    def find_domain_clusters(self, project_root: Path, concept: str, limit: int = 20) -> dict[str, object]:
        return self._hotspots.find_domain_clusters(project_root, concept, limit=limit)

    def find_duplicate_structures(self, project_root: Path, role_filter: str | None = None, limit: int = 20) -> dict[str, object]:
        return self._hotspots.find_duplicate_structures(project_root, role_filter=role_filter, limit=limit)

    def find_transition_points(self, project_root: Path, concept: str, limit: int = 20) -> dict[str, object]:
        return self._hotspots.find_transition_points(project_root, concept, limit=limit)
    def trace_component_usage(self, project_root: Path, component_name: str, limit: int = 50) -> dict[str, object]:
        return self._hotspots.trace_component_usage(project_root, component_name, limit=limit)

    def find_state_model_mismatch(self, project_root: Path, concept: str, limit: int = 20) -> dict[str, object]:
        return self._hotspots.find_state_model_mismatch(project_root, concept, limit=limit)

    def find_routes(self, project_root: Path, query: str | None = None, limit: int = 50) -> dict[str, object]:
        return self._route_queries.find_routes(project_root, query=query, limit=limit)

    def trace_api_to_ui(self, project_root: Path, concept: str, limit: int = 50) -> dict[str, object]:
        return self._route_queries.trace_api_to_ui(project_root, concept, limit=limit)

    def find_ui_backend_touchpoints(self, project_root: Path, concept: str, limit: int = 50) -> dict[str, object]:
        return self._route_queries.find_ui_backend_touchpoints(project_root, concept, limit=limit)

    def find_policy_surfaces(self, project_root: Path, concept: str, limit: int = 50) -> dict[str, object]:
        return self._route_queries.find_policy_surfaces(project_root, concept, limit=limit)

    def find_entrypoints(self, project_root: Path, concept: str, limit: int = 50) -> dict[str, object]:
        return self._route_queries.find_entrypoints(project_root, concept, limit=limit)

    def find_factories(self, project_root: Path, query: str, include_tests: bool = True, limit: int = 50) -> dict[str, object]:
        return self._hotspots.find_factories(project_root, query, include_tests=include_tests, limit=limit)

    def get_outline(self, project_root: Path, path: str) -> list[dict[str, str | int | bool]]:
        return self._hotspots.get_outline(project_root, path)

    def find_partial_group(self, project_root: Path, symbol: str, limit: int = 50) -> list[dict[str, str | int | bool | None]]:
        return self._hotspots.find_partial_group(project_root, symbol, limit=limit)

    def find_data_structures(
        self,
        project_root: Path,
        query: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, str | int | bool | None]]:
        return self._hotspots.find_data_structures(project_root, query=query, limit=limit)

    def find_frontend_symbols(
        self,
        project_root: Path,
        query: str | None = None,
        kinds: tuple[str, ...] = ("component", "context_provider", "hook", "function", "initializer"),
        limit: int = 50,
    ) -> list[dict[str, str | int | bool | None]]:
        results = self._hotspots.find_frontend_symbols(project_root, query=query, kinds=kinds, limit=limit)
        if results or not query or not query.strip():
            return results
        self._ensure_parsed_candidates(project_root, query, limit=limit * 4)
        results = self._hotspots.find_frontend_symbols(project_root, query=query, kinds=kinds, limit=limit)
        if results:
            return results
        file_hits = self.search_code(project_root, query, limit=limit * 4)
        paths = [str(item.get("path") or "") for item in file_hits if str(item.get("path") or "")]
        if paths:
            self.sync_code_files(project_root, paths=paths)
            results = self._hotspots.find_frontend_symbols(project_root, query=query, kinds=kinds, limit=limit)
            if results:
                return results
        fallback = self.search_symbols(project_root, query=query, limit=limit * 3)
        allowed = set(kinds)
        filtered = [item for item in fallback if str(item.get("kind") or "") in allowed]
        return filtered[:limit]





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
                **({"container": row["container"]} if row["container"] else {}),
                **({"is_partial": True} if row["is_partial"] else {}),
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
                **({"container": row["container"]} if row["container"] else {}),
                **({"is_partial": True} if row["is_partial"] else {}),
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
            **({"container": row["container"]} if row["container"] else {}),
            **({"is_partial": True} if row["is_partial"] else {}),
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
        return self._bundles.get_symbol_bundle(project_root, symbol, path=path, kind=kind, limit=limit)

    def get_subsystem_bundle(self, project_root: Path, concept: str, limit: int = 20) -> dict[str, object]:
        return self._bundles.get_subsystem_bundle(project_root, concept, limit=limit)

    def investigate(
        self,
        project_root: Path,
        concept: str,
        limit: int = 5,
        depth: str = "standard",
        focus: str = "general",
    ) -> dict[str, object]:
        return self._bundles.investigate(project_root, concept, limit=limit, depth=depth, focus=focus)



    def _namespace_for_path(self, project_root: Path, path: str, cache: dict[str, str | None] | None = None) -> str | None:
        if cache is not None and path in cache:
            return cache[path]
        abs_path = project_root / path
        namespace = None
        try:
            text = abs_path.read_text(encoding="utf-8", errors="ignore")
            match = re.search(r"^\s*namespace\s+([A-Za-z_][A-Za-z0-9_\.]*)", text, re.MULTILINE)
            if match:
                namespace = match.group(1)
        except OSError:
            namespace = None
        if cache is not None:
            cache[path] = namespace
        return namespace

    def _extract_method_signature(self, project_root: Path, path: str, line_number: int) -> dict[str, object]:
        abs_path = project_root / path
        try:
            lines = abs_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            return {}
        index = max(0, line_number - 1)
        window = lines[index:index + 4]
        header_parts = []
        paren_balance = 0
        seen_open = False
        for raw_line in window:
            line = raw_line.strip()
            if not line:
                continue
            header_parts.append(line)
            paren_balance += line.count("(") - line.count(")")
            if "(" in line:
                seen_open = True
            if seen_open and paren_balance <= 0:
                break
        merged = " ".join(header_parts)
        if "{" in merged:
            merged = merged.split("{", 1)[0].rstrip()
        match = re.search(
            r"(?P<return>(?:public|private|internal|protected|static|virtual|override|abstract|async|sealed|extern|unsafe|new|partial|readonly|\s)+[A-Za-z_<>,\[\]\.?]+)\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*(?P<params>\([^)]*\))",
            merged,
        )
        if not match:
            return {"signature": merged.strip()} if merged.strip() else {}
        return_type = re.sub(r"\s+", " ", match.group("return")).strip()
        params = match.group("params").strip()
        return {
            "return_type": return_type,
            "params": params,
            "signature": f"{return_type} {match.group('name')}{params}",
        }

    def _enum_members_for_container(self, project_root: Path, path: str, container: str) -> list[str]:
        self.init_db(project_root)
        with self.connect(project_root) as conn:
            rows = conn.execute(
                "SELECT symbol FROM code_outlines WHERE path = ? AND kind = 'enum_member' AND container = ? ORDER BY line_number",
                (path, container),
            ).fetchall()
        return [str(row["symbol"]) for row in rows]

    def _extract_constructor_params(self, project_root: Path, path: str, type_name: str) -> dict[str, object]:
        abs_path = project_root / path
        try:
            text = abs_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return {}

        text = re.sub(r"//.*?$", "", text, flags=re.MULTILINE)
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)

        record_match = re.search(rf"\brecord\s+{re.escape(type_name)}\s*\(([^)]*)\)", text)
        if record_match:
            raw = record_match.group(1).strip()
            params = [item.strip() for item in raw.split(",") if item.strip()]
            return {
                "kind": "record_constructor",
                "params": params,
                "signature": f"{type_name}({raw})",
            }

        ctor_match = re.search(rf"\b{re.escape(type_name)}\s*\(([^)]*)\)", text)
        if ctor_match:
            raw = ctor_match.group(1).strip()
            params = [item.strip() for item in raw.split(",") if item.strip()]
            return {
                "kind": "constructor",
                "params": params,
                "signature": f"{type_name}({raw})",
            }
        return {}

    def _extract_service_methods_from_declaring_files(self, project_root: Path, service_name: str, limit: int = 100) -> list[dict[str, object]]:
        pattern = re.compile(rf"\b(class|record|struct)\s+{re.escape(service_name)}\b")
        method_pattern = re.compile(
            r"^\s*(public\s+(?:static\s+)?(?:async\s+)?[A-Za-z_<>,\[\]\.?]+)\s+([A-Za-z_][A-Za-z0-9_]*)\s*(\([^)]*\))",
            re.MULTILINE,
        )
        namespace_cache: dict[str, str | None] = {}
        results: list[dict[str, object]] = []
        for path in sorted(project_root.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() not in {".cs", ".ts", ".tsx", ".js", ".jsx"}:
                continue
            rel = path.relative_to(project_root).as_posix()
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if not pattern.search(text):
                continue
            for match in method_pattern.finditer(text):
                return_type, method_name, params = match.groups()
                line_number = text[: match.start()].count("\n") + 1
                results.append(
                    {
                        "path": rel,
                        "symbol": method_name,
                        "kind": "method",
                        "line_number": line_number,
                        "container": service_name,
                        **(
                            {"namespace": namespace}
                            if (namespace := self._namespace_for_path(project_root, rel, namespace_cache))
                            else {}
                        ),
                        "return_type": return_type.strip(),
                        "params": params.strip(),
                        "signature": f"{return_type.strip()} {method_name}{params.strip()}",
                    }
                )
                if len(results) >= limit:
                    return results
        return results

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
                if item.get("is_partial") and item["symbol"] not in seen:
                    seen.add(item["symbol"])
                    # Include partial file paths and outlines but NOT full snippets —
                    # those are 20-35KB each and destroy token budgets.
                    # Agent can use code_get_lines if it needs the actual code.
                    raw_bundle = self.get_partial_bundle(project_root, symbol=str(item["symbol"]))
                    slim_bundle = [
                        {
                            "path": b["path"],
                            "line_count": b.get("line_count", 0),
                            **({"outline": b["outline"]} if b.get("outline") else {}),
                        }
                        for b in raw_bundle if isinstance(b, dict)
                    ]
                    partial_groups.append({"symbol": item["symbol"], "files": slim_bundle})
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

    def get_style_bundle(
        self,
        project_root: Path,
        class_names: list[str],
        limit: int = 100,
    ) -> dict[str, object]:
        """Find CSS definitions and usages for given class names across indexed files."""
        self.init_db(project_root)
        results: list[dict[str, object]] = []

        with self.connect(project_root) as conn:
            for class_name in class_names[:20]:  # cap input
                clean = class_name.strip().lstrip(".")
                if not clean:
                    continue

                # Find definitions in CSS files (kind=css_class)
                definitions = conn.execute(
                    """
                    SELECT co.path, co.symbol, co.kind, co.line_number, cf.role
                    FROM code_outlines co
                    JOIN code_files cf ON cf.path = co.path
                    WHERE co.symbol = ? AND co.kind = 'css_class'
                    LIMIT 20
                    """,
                    (clean,),
                ).fetchall()

                # Find usages in razor/cshtml files (grep-style via outline text won't work —
                # class usages are in HTML attributes, not indexed as symbols).
                # Instead, search code_files content for the class name in razor files.
                # For now, return definitions and related CSS variables.
                css_vars: list[dict[str, object]] = []
                if definitions:
                    # Get all CSS variables from the same files for context
                    def_paths = list({row["path"] for row in definitions})
                    for def_path in def_paths[:5]:
                        vars_in_file = conn.execute(
                            """
                            SELECT symbol, line_number, container
                            FROM code_outlines
                            WHERE path = ? AND kind = 'css_variable'
                            ORDER BY line_number
                            LIMIT 50
                            """,
                            (def_path,),
                        ).fetchall()
                        css_vars.extend([
                            {"variable": row["symbol"], "line": int(row["line_number"]), "context": row["container"]}
                            for row in vars_in_file
                        ])

                results.append({
                    "class_name": clean,
                    "definitions": [
                        {
                            "path": row["path"],
                            "line_number": int(row["line_number"]),
                            "role": row["role"],
                        }
                        for row in definitions
                    ],
                    "related_variables": css_vars[:20],
                })

        return {"results": results[:limit]}

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

    # Directories to prune during os.walk — never descend into these.
    _PRUNE_DIRS_BASE: set[str] = {
        ".git", ".memory", ".opencode", ".claude", ".github", ".backup", ".backups",
        "node_modules", ".next", ".docusaurus", "__pycache__", ".venv", "venv",
        "dist", "build", "coverage", "obj", "bin", "compiled", "vendor", "vendors",
        "datatables", "target", "migrations", "dumps", "backups", "backup",
        "temp", "tmp",
    }

    @classmethod
    def _prune_dirs(cls) -> set[str]:
        from .config import INDEX_EXTRA_SKIP_DIRS
        return cls._PRUNE_DIRS_BASE | INDEX_EXTRA_SKIP_DIRS

    @classmethod
    def _module_hint_dirs(cls, project_root: Path | None = None) -> set[str]:
        from .config import INDEX_EXTRA_MODULE_HINTS
        base = cls(None)._modules._MODULE_HINT_DIRS_BASE | INDEX_EXTRA_MODULE_HINTS
        if project_root is not None:
            base = base | module_hints_from_descriptors(project_root)
        return base

    @staticmethod
    def _max_json_size() -> int:
        from .config import INDEX_MAX_JSON_SIZE
        return INDEX_MAX_JSON_SIZE

    def _walk_source_files(self, project_root: Path, include_tests: bool = False) -> list[Path]:
        """Walk the project tree with directory pruning. Much faster than rglob for large repos."""
        results: list[Path] = []
        root_str = str(project_root)
        for dirpath, dirnames, filenames in os.walk(root_str):
            # Prune directories in-place (modifying dirnames prevents os.walk from descending)
            rel_dir = os.path.relpath(dirpath, root_str).replace("\\", "/")
            rel_parts = rel_dir.lower().split("/") if rel_dir != "." else []
            dirnames[:] = [
                d for d in dirnames
                if d.lower() not in self._prune_dirs()
                and d.lower() != "index_languages"
                and not d.startswith(".")
                and not (d.lower() == "lib" and "wwwroot" in rel_parts)
            ]

            # Apply test skip at directory level
            if not include_tests:
                dirnames[:] = [
                    d for d in dirnames
                    if d.lower() not in ("tests", "test", "e2e", "__tests__", "__test__")
                    and not d.lower().endswith(".test")
                ]

            # Skip known non-source directories
            if any(segment in ("executed", "archived", "old", "bak", "fixtures", "sqlscripts") for segment in rel_parts):
                dirnames.clear()
                continue

            for fname in filenames:
                fpath = Path(dirpath) / fname
                lower_name = fname.lower()
                if lower_name.endswith((".min.js", ".min.css", ".bak", ".dump", ".backup")):
                    continue
                if lower_name in (
                    "package-lock.json", "bun.lock", "yarn.lock", "pnpm-lock.yaml",
                    "composer.lock", "cargo.lock", "gemfile.lock", "poetry.lock",
                    "tsconfig.tsbuildinfo", ".eslintcache",
                ):
                    continue
                # Skip large JSON files (> 100KB)
                if lower_name.endswith(".json"):
                    try:
                        if fpath.stat().st_size > self._max_json_size():
                            continue
                    except OSError:
                        continue
                results.append(fpath)
        return sorted(results)

    def _should_skip(self, project_root: Path, path: Path, include_tests: bool = False) -> bool:
        rel = path.relative_to(project_root).as_posix()
        prefixes = (
            ".git/",
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
        if "index_languages" in parts:
            return True
        if any(segment.startswith(".temp-") for segment in parts):
            return True
        if any(
            segment in parts
                for segment in (
                    "node_modules",
                    ".next",
                    ".docusaurus",
                    "build",
                    "compiled",
                    "vendor",
                    "vendors",
                    "datatables",
                "dist",
                "coverage",
                "obj",
                "bin",
                "__pycache__",
                ".venv",
                "venv",
                "target",
                "migrations",
                "dumps",
                "backups",
                "backup",
            )
        ):
            return True
        # Skip executed/archived SQL scripts, migration dumps, and test fixtures
        if any(segment in ("executed", "archived", "old", "bak", "fixtures") for segment in parts):
            return True
        if "sqlscripts" in parts:
            return True
        if "wwwroot" in parts and "lib" in parts:
            return True
        if path.name.lower().endswith((".min.js", ".min.css", ".bak", ".dump", ".backup")):
            return True
        # Skip noisy/generated data files
        lower_name = path.name.lower()
        if lower_name in (
            "package-lock.json", "bun.lock", "yarn.lock", "pnpm-lock.yaml",
            "composer.lock", "cargo.lock", "gemfile.lock", "poetry.lock",
            "tsconfig.tsbuildinfo", ".eslintcache",
        ):
            return True
        # Skip large JSON data files (> 100KB) — likely generated or data dumps
        if lower_name.endswith(".json") and path.stat().st_size > self._max_json_size():
            return True
        if not include_tests:
            if self._path_looks_like_test(rel):
                return True
        return False

    @staticmethod
    def _path_looks_like_test(path: str) -> bool:
        parts = [part for part in path.lower().split("/") if part]
        return any(
            part in {"tests", "test", "e2e", "__tests__", "__test__"}
            or part.endswith(".test")
            for part in parts
        )

    def _language_for(self, path: Path, project_root: Path | None = None) -> str | None:
        if project_root is not None:
            rel = path.relative_to(project_root).as_posix()
            return language_for_custom_descriptor(project_root, rel, path.suffix.lower())
        return language_for_builtin_descriptor(path.as_posix(), path.suffix.lower())

    def _summarize(self, text: str, file_name: str, max_lines: int = 8) -> str:
        return self._outlines._summarize(text, file_name, max_lines=max_lines)

    def _extract_outline(self, project_root: Path, text: str, code_language: str) -> list[tuple[str, str, int, str | None, bool]]:
        return self._outlines._extract_outline(project_root, text, code_language)

    def _extract_csharp_outline(self, text: str) -> list[tuple[str, str, int, str | None, bool]]:
        return self._outlines._extract_csharp_outline(text)

    def _extract_python_outline(self, text: str) -> list[tuple[str, str, int, str | None, bool]]:
        return self._outlines._extract_python_outline(text)

    def _extract_component_semantics(
        self,
        project_root: Path,
        code_language: str,
        text: str,
        ast_outline: list[tuple[str, str, int, str | None, bool]],
        outlines: list[tuple[str, str, int, str | None, bool]],
    ) -> None:
        self._outlines._extract_component_semantics(project_root, code_language, text, ast_outline, outlines)

    def _find_brace_end(self, lines: list[str], start_idx: int) -> int:
        return self._outlines._find_brace_end(lines, start_idx)

    def _extract_js_initializer(self, line: str) -> str | None:
        return self._outlines._extract_js_initializer(line)

    def _extract_razor_outline(self, text: str) -> list[tuple[str, str, int, str | None, bool]]:
        return self._outlines._extract_razor_outline(text)

    def _extract_resx_outline(self, text: str) -> list[tuple[str, str, int, str | None, bool]]:
        return self._outlines._extract_resx_outline(text)

    def _extract_css_outline(self, text: str) -> list[tuple[str, str, int, str | None, bool]]:
        return self._outlines._extract_css_outline(text)


    def _extract_edges(self, text: str, language: str) -> list[tuple[str, str]]:
        return self._edges._extract_edges(text, language)

    def _extract_python_edges(self, text: str) -> list[tuple[str, str]]:
        return self._edges._extract_python_edges(text)

    def _resolve_edge_to_paths(
        self,
        project_root: Path,
        source_path: str,
        target: str,
        kind: str,
        limit: int = 20,
    ) -> list[str]:
        return self._edges._resolve_edge_to_paths(project_root, source_path, target, kind, limit=limit)

    def _existing_relative_candidates(self, project_root: Path, base: Path, python_only: bool = False) -> list[str]:
        return self._edges._existing_relative_candidates(project_root, base, python_only=python_only)


    def _get_file_stub(self, project_root: Path, path: str) -> dict[str, str | int] | None:
        return self._utility._get_file_stub(project_root, path)

    def _is_indexed_file(self, project_root: Path, path: str) -> bool:
        return self._utility._is_indexed_file(project_root, path)

    def _file_exists(self, project_root: Path, path: str) -> bool:
        return self._utility._file_exists(project_root, path)

    def _extract_relevant_files(self, sections: dict[str, list[str]]) -> list[str]:
        return self._utility._extract_relevant_files(sections)

    @staticmethod
    def _outline_row_to_dict(row: sqlite3.Row) -> dict[str, object]:
        return CodeIndexUtilityService._outline_row_to_dict(row)

    def _extract_snippet(self, text: str, language: str, start_line: int) -> str:
        return self._utility._extract_snippet(text, language, start_line)

    def _extract_indent_block(self, lines: list[str], index: int) -> str:
        return self._utility._extract_indent_block(lines, index)

    def _extract_brace_block(self, lines: list[str], index: int) -> str:
        return self._utility._extract_brace_block(lines, index)


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
        return self._inference._score_text_match(
            needle,
            candidate,
            exact,
            prefix,
            contains,
            reasons=reasons,
            label=label,
        )

    def _trace_confidence(self, matches: list[dict[str, object]]) -> str:
        return self._inference._trace_confidence(matches)

    def _trace_summary(self, matches: list[dict[str, object]]) -> list[str]:
        return self._inference._trace_summary(matches)

    def _concept_variants(self, concept: str) -> list[str]:
        return self._inference._concept_variants(concept)

    def _path_weight(self, project_root: Path, path: str) -> int:
        return self._inference._path_weight(project_root, path)

    @property
    def _ROLE_RELEVANCE(self) -> dict[str, int]:
        return self._inference._ROLE_RELEVANCE

    def _role_relevance_boost(self, project_root: Path, path: str) -> int:
        return self._inference._role_relevance_boost(project_root, path)

    def _load_indexing_hints(self, project_root: Path) -> dict[str, list[str]]:
        return self._inference._load_indexing_hints(project_root)

    def _ensure_parsed_candidates(self, project_root: Path, query: str, limit: int = 100) -> int:
        return self._inference._ensure_parsed_candidates(project_root, query, limit=limit)

    def _infer_layer_from_path(self, path: str) -> str:
        return self._inference._infer_layer_from_path(path)

    def _infer_code_role(
        self,
        project_root: Path,
        path: str,
        code_language: str,
        outlines: list[tuple[str, str, int, str | None, bool]],
    ) -> str | None:
        return self._inference._infer_code_role(project_root, path, code_language, outlines)

    def _infer_plugin_structure_role(self, project_root: Path, path: str) -> str | None:
        return self._inference._infer_plugin_structure_role(project_root, path)

    def _looks_like_component_name(self, name: str) -> bool:
        return self._inference._looks_like_component_name(name)

    def _role_group(self, role: str) -> str:
        return self._inference._role_group(role)

    def _layer_rank(self, layer: str) -> int:
        return self._inference._layer_rank(layer)



    def _infer_layer_from_path(self, path: str) -> str:
        lower = path.lower()
        suffix = Path(path).suffix.lower()
        # 1. Try language-descriptor layer_tokens first
        try:
            # Use the most recent project_root from init_db if available
            project_root = getattr(self, '_last_project_root', None)
            if project_root:
                layer = layer_from_descriptor(project_root, path, suffix)
                if layer:
                    return layer
        except Exception:
            pass
        # 2. Fall back to global config default_layer_tokens
        config = load_index_config()
        default_tokens = config.get("default_layer_tokens", {})
        if isinstance(default_tokens, dict):
            for layer, tokens in default_tokens.items():
                if any(token in lower for token in tokens):
                    return layer
        # 3. Hardcoded fallback (for when no config is loaded)
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
        code_language: str,
        outlines: list[tuple[str, str, int, str | None, bool]],
    ) -> str | None:
        descriptor_role = role_from_descriptor(project_root, path, Path(path).suffix.lower())
        if descriptor_role:
            return descriptor_role
        lower = path.lower()
        parts = lower.split("/")
        name = Path(path).stem.lower()
        kinds = {item[1] for item in outlines}
        if code_language in {"jsx", "tsx"}:
            if "context_provider" in kinds:
                return "context-provider"
            if "hook" in kinds and kinds <= {"hook"}:
                return "hook-module"
            if "component" in kinds:
                return "component"
            if self._looks_like_component_name(Path(path).stem):
                return "component"
        if code_language in {"javascript", "typescript"}:
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
            if lower.endswith((".config.js", ".config.ts", ".config.mjs", ".config.cjs")) or any(token in parts for token in ("schemas", "schema")):
                return "config-module"
            if lower.endswith((".d.ts", "-env.d.ts")) or name in {"next-env", "sst-env", "env", "sidebars"}:
                return "config-module"
            if name in {"vite", "happydom", "vitest", "jest", "tsconfig"}:
                return "config-module"
            if any(token in parts for token in ("lib", "utils", "helpers")):
                return "utility-module"
            if any(token in parts for token in ("prisma", "db", "database")):
                return "data-access"
            if name in {"types", "type", "storage", "registry", "constants", "page-key", "evidence"}:
                return "utility-module"
            if any(token in parts for token in ("assets", "pwaassets", "static")):
                return "asset-script"
            if "initializer" in kinds:
                return "initializer-module"
            if "hook" in kinds and kinds <= {"hook"}:
                return "hook-module"
        if code_language == "csharp":
            logical_name = name.split(".", 1)[0]
            if lower.endswith(".cshtml.cs"):
                return "page-model"
            if "pages" in parts and name.endswith("model"):
                return "page-model"
            if "pages" in parts and logical_name.endswith("pagebase"):
                return "page-model"
            if lower.endswith("program.cs"):
                return "initializer-module"
            if lower.endswith("dependencyinjection.cs"):
                return "initializer-module"
            if name.endswith("dbcontext") or "dbcontext" in name:
                return "data-access"
            if "seeding" in parts or logical_name.startswith("seed"):
                return "script"
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
            if any(token in parts for token in ("database", "data", "dal", "persistence")):
                return "data-access"
            if any(token in parts for token in ("areas",)):
                return "controller"
        if code_language == "python":
            pass
        if code_language == "razor":
            if name.startswith("_"):
                return "partial-view"
            return "page-view"
        if code_language == "resx":
            return "resource"
        if code_language == "css":
            if "input" in name or "tailwind" in name:
                return "asset-style-source"
            return "asset-style"
        if code_language == "powershell":
            return "script"
        if code_language == "shell":
            return "script"

        # ── New language role inference ──────────────────────────────────
        if code_language == "rust":
            if name in ("main", "lib"):
                return "core-module"
            if name == "mod":
                return "module-init"
            if any(token in parts for token in ("tests", "benches", "examples")):
                return "script"
            if any(token in parts for token in ("models", "types", "schema")):
                return "data-model"
            if any(token in parts for token in ("utils", "helpers", "common")):
                return "utility-module"
            if any(token in parts for token in ("api", "handlers", "routes")):
                return "route-handler"
            if any(token in parts for token in ("services", "engine", "core")):
                return "service"
            return "core-module"
        if code_language == "go":
            if name == "main":
                return "core-module"
            if any(token in parts for token in ("handlers", "api", "routes")):
                return "route-handler"
            if any(token in parts for token in ("models", "types", "schema")):
                return "data-model"
            if any(token in parts for token in ("services", "pkg")):
                return "service"
            if any(token in parts for token in ("cmd", "cli")):
                return "script"
            if name.endswith("_test"):
                return "script"
            return "core-module"
        if code_language == "java":
            if name.endswith(("controller", "resource")):
                return "controller"
            if name.endswith("service") or name.endswith("serviceimpl"):
                return "service"
            if name.endswith(("repository", "dao")):
                return "repository"
            if name.endswith(("entity", "model", "dto")):
                return "data-model"
            if name.endswith("config") or name.endswith("configuration"):
                return "configuration"
            if name.endswith(("test", "spec")):
                return "script"
            return "core-module"
        if code_language == "kotlin":
            if name.endswith(("controller", "resource")):
                return "controller"
            if name.endswith("service"):
                return "service"
            if name.endswith(("repository", "dao")):
                return "repository"
            if name.endswith(("entity", "model", "dto")):
                return "data-model"
            return "core-module"
        if code_language == "ruby":
            if any(token in parts for token in ("controllers",)):
                return "controller"
            if any(token in parts for token in ("models",)):
                return "data-model"
            if any(token in parts for token in ("views", "templates")):
                return "page-view"
            if any(token in parts for token in ("services", "jobs", "workers")):
                return "service"
            if name.endswith("_spec") or name.endswith("_test"):
                return "script"
            return "core-module"
        if code_language == "php":
            if name.endswith("controller"):
                return "controller"
            if name.endswith("model") or any(token in parts for token in ("models", "entities")):
                return "data-model"
            if name.endswith("service"):
                return "service"
            if any(token in parts for token in ("views", "templates", "resources")):
                return "page-view"
            return "core-module"
        if code_language == "elixir":
            if any(token in parts for token in ("controllers",)):
                return "controller"
            if any(token in parts for token in ("views", "templates")):
                return "page-view"
            if name.endswith("_test"):
                return "script"
            return "core-module"
        if code_language == "sql":
            if any(token in parts for token in ("schema", "db", "database")):
                return "data-access"
            return "data-access"
        if code_language in {"scss", "sass", "less"}:
            if "input" in name or "tailwind" in name:
                return "asset-style-source"
            return "asset-style"
        if code_language == "html":
            if any(token in parts for token in ("templates", "email", "emailtemplates")):
                return "template"
            if "wwwroot" in parts:
                return "asset-html"
            return "template"
        if code_language == "vue":
            if "pages" in parts:
                return "page"
            if "layouts" in parts:
                return "layout"
            if "components" in parts:
                return "component"
            return "component"
        if code_language == "svelte":
            if "routes" in parts:
                return "page"
            if "components" in parts:
                return "component"
            return "component"
        if code_language == "prisma":
            return "data-access"
        if code_language == "toml":
            return "configuration"
        if code_language in {"yaml", "yml"}:
            if any(token in parts for token in ("ci", "workflows", ".github")):
                return "configuration"
            return "configuration"
        if code_language == "json":
            if name == "package":
                return "configuration"
            if name == "tsconfig" or name.endswith("config"):
                return "configuration"
            if name == "manifest":
                return "configuration"
            if any(token in parts for token in ("schemas", "archetypes")):
                return "configuration"
            return "data-file"

        # ── Fallback path-based heuristics (any language) ───────────────
        if any(token in parts for token in ("components", "features")):
            return "component"
        if any(token in parts for token in ("pages", "views")):
            return "page"
        if any(token in parts for token in ("layouts",)):
            return "layout"
        if any(token in parts for token in ("hooks",)):
            return "hook-module"
        if any(token in parts for token in ("services",)):
            return "service"
        if any(token in parts for token in ("utils", "helpers", "lib", "app_helpers")):
            return "utility-module"
        if any(token in parts for token in ("scripts", "bin", "cli", "tools")):
            return "script"
        if any(token in parts for token in ("config", "configs", "app_start", "infra")):
            return "configuration"
        if any(token in parts for token in ("api", "routes", "controllers")):
            return "route-handler"
        if any(token in parts for token in ("models", "entities", "types", "dto", "dtos")):
            return "data-model"
        if any(token in parts for token in ("middleware",)):
            return "middleware"
        if any(token in parts for token in ("worker", "workers", "jobs")):
            return "worker"
        if any(token in parts for token in ("examples", "demo", "demos")):
            return "script"
        if any(token in parts for token in ("src",)):
            return "core-module"

        return None


    def _looks_like_component_name(self, name: str) -> bool:
        return bool(name and name[0].isupper() and any(ch.isupper() for ch in name[1:]))

    def _role_group(self, role: str) -> str:
        if role in {"component", "context-provider", "hook-module", "page", "layout", "asset-script",
                     "page-view", "partial-view", "shared-view"}:
            return "frontend"
        if role in {"controller", "route-handler", "page-model", "hub"}:
            return "request-surfaces"
        if role in {"service", "policy", "repository", "validator", "middleware", "worker", "server-module", "core-module"}:
            return "logic-runtime"
        if role in {"data-model", "data-access"}:
            return "data"
        if role in {"initializer-module", "module-init", "config-module", "script", "utility", "utility-module",
                     "configuration", "plugin-generator", "plugin-module", "plugin-template-module",
                     "framework-generator", "barrel-module", "abstraction", "resource",
                     "asset-style", "asset-style-source"}:
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

