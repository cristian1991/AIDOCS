from __future__ import annotations

import ast
import hashlib
import os
import re
import sqlite3
from pathlib import Path

from .frontend_ast import FrontendAstExtractor
from .language_descriptors import descriptor_for_language, entry_points_from_descriptors, extractor_family_for_language, language_for_builtin_descriptor, language_for_custom_descriptor, layer_from_descriptor, line_patterns_for_language, load_index_config, module_hints_from_descriptors, outline_family_for_language, outline_patterns_for_language, role_from_descriptor, role_from_descriptor_extended
from .outline_extractors import (
    extract_css_outline,
    extract_csharp_outline,
    extract_generic_outline,
    extract_line_patterns,
    extract_python_outline,
    extract_resx_outline,
    generic_outline_patterns,
    outline_family_patterns,
)
from .session_store import SessionStore


class CodeIndexStore:
    """Derived SQLite index for repository code files and lightweight summaries."""

    INDEX_VERSION = "code-index-v7"

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
        config = load_index_config()
        manifests = config.get("module_manifests")
        return dict(manifests) if manifests and isinstance(manifests, dict) else self._MODULE_MANIFESTS_DEFAULT

    @property
    def _MODULE_SKIP_DIRS(self) -> set[str]:
        config = load_index_config()
        skip = config.get("skip_dirs")
        return set(skip) if skip and isinstance(skip, set) else self._MODULE_SKIP_DIRS_DEFAULT

    def _get_entry_point_patterns(self, project_root: Path) -> list[tuple[str, str]]:
        """Get entry point patterns: descriptor-defined first, then defaults."""
        try:
            eps = entry_points_from_descriptors(project_root)
            if eps:
                return list(eps.items())
        except Exception:
            pass
        return self._ENTRY_POINT_PATTERNS_DEFAULT

    # Well-known top-level directory names that strongly suggest a module.
    _MODULE_HINT_DIRS_BASE: set[str] = {
        "app", "cli", "core", "lib", "server", "web", "website", "api",
        "frontend", "backend", "services", "packages", "plugins", "analyzer",
        "worker", "workers", "gateway", "proxy", "admin", "dashboard",
        "mobile", "desktop", "console", "docs", "sdk", "engine",
        "database-generators", "feature-generators", "framework-generators",
        "archetypes", "templates", "schemas",
        # Common project dirs that hold meaningful source
        "scripts", "components", "pages", "views", "layouts",
        "prisma", "config", "configs", "models", "entities",
        "middleware", "hooks", "utils", "helpers", "tools",
        "examples", "demo", "assets", "public", "static",
        "infra", "infrastructure", "deploy", "ci",
        "shared", "common", "internal",
    }

    def detect_modules(self, project_root: Path) -> list[dict[str, str | int | None]]:
        """Detect logical modules in a project (formal workspaces + informal monorepo heuristics).

        Returns a list of dicts with: module_path, name, kind, stack, entry_point, description.
        """
        modules: list[dict[str, str | int | None]] = []
        seen: set[str] = set()

        # 1. Formal workspaces (package.json workspaces, Cargo workspace members, etc.)
        self._detect_formal_workspaces(project_root, modules, seen)

        # 2. .csproj / .sln based modules (.NET)
        self._detect_dotnet_projects(project_root, modules, seen)

        # 3. Informal modules: top-level dirs with manifests or entry points
        self._detect_informal_modules(project_root, modules, seen)

        # 4. Nested workspaces: subprojects that themselves declare workspaces
        self._detect_nested_workspaces(project_root, modules, seen)

        return modules

    def _detect_formal_workspaces(
        self,
        project_root: Path,
        modules: list[dict[str, str | int | None]],
        seen: set[str],
    ) -> None:
        """Detect npm/bun workspaces, Cargo workspaces, etc."""
        import json as json_mod

        # npm/bun/pnpm workspaces
        pkg_json = project_root / "package.json"
        if pkg_json.is_file():
            try:
                pkg = json_mod.loads(pkg_json.read_text(encoding="utf-8", errors="ignore"))
                workspaces = pkg.get("workspaces", [])
                if isinstance(workspaces, dict):
                    workspaces = workspaces.get("packages", [])
                if isinstance(workspaces, list):
                    import glob as glob_mod
                    for pattern in workspaces:
                        for match in glob_mod.glob(str(project_root / pattern)):
                            match_path = Path(match)
                            if match_path.is_dir() and (match_path / "package.json").is_file():
                                rel = match_path.relative_to(project_root).as_posix()
                                if rel not in seen:
                                    seen.add(rel)
                                    modules.append(self._build_module_entry(
                                        project_root, rel, "workspace", "javascript",
                                        self._find_entry_point(match_path),
                                    ))
            except (json_mod.JSONDecodeError, OSError):
                pass

        # pnpm workspaces (pnpm-workspace.yaml)
        pnpm_ws = project_root / "pnpm-workspace.yaml"
        if pnpm_ws.is_file():
            try:
                text = pnpm_ws.read_text(encoding="utf-8", errors="ignore")
                import glob as glob_mod
                in_packages = False
                for line in text.splitlines():
                    stripped = line.strip()
                    if stripped == "packages:" or stripped.startswith("packages:"):
                        in_packages = True
                        continue
                    if in_packages and stripped.startswith("- "):
                        pattern = stripped[2:].strip().strip("'\"")
                        if pattern:
                            for match in glob_mod.glob(str(project_root / pattern)):
                                match_path = Path(match)
                                if match_path.is_dir() and (match_path / "package.json").is_file():
                                    rel = match_path.relative_to(project_root).as_posix()
                                    if rel not in seen:
                                        seen.add(rel)
                                        modules.append(self._build_module_entry(
                                            project_root, rel, "workspace", "javascript",
                                            self._find_entry_point(match_path),
                                        ))
                    elif in_packages and not stripped.startswith("-") and not stripped.startswith("#"):
                        in_packages = False
            except OSError:
                pass

        # Cargo workspaces
        cargo_toml = project_root / "Cargo.toml"
        if cargo_toml.is_file():
            try:
                text = cargo_toml.read_text(encoding="utf-8", errors="ignore")
                # Simple TOML parsing for workspace members
                in_workspace = False
                in_members = False
                for line in text.splitlines():
                    stripped = line.strip()
                    if stripped == "[workspace]":
                        in_workspace = True
                        continue
                    if in_workspace and stripped.startswith("members"):
                        in_members = True
                        continue
                    if in_workspace and stripped.startswith("[") and stripped != "[workspace]":
                        in_workspace = False
                        in_members = False
                        continue
                    if in_members:
                        member_match = re.match(r'\s*"([^"]+)"', stripped)
                        if member_match:
                            member_pattern = member_match.group(1)
                            import glob as glob_mod
                            for match in glob_mod.glob(str(project_root / member_pattern)):
                                match_path = Path(match)
                                if match_path.is_dir():
                                    rel = match_path.relative_to(project_root).as_posix()
                                    if rel not in seen:
                                        seen.add(rel)
                                        modules.append(self._build_module_entry(
                                            project_root, rel, "workspace", "rust",
                                            self._find_entry_point(match_path),
                                        ))
                        if stripped == "]":
                            in_members = False
            except OSError:
                pass

    def _detect_nested_workspaces(
        self,
        project_root: Path,
        modules: list[dict[str, str | int | None]],
        seen: set[str],
    ) -> None:
        """Check detected subprojects for their own workspace declarations (nested monorepos)."""
        import json as json_mod

        # Collect subprojects that were already found
        subproject_paths = [m["module_path"] for m in modules if m["kind"] in ("subproject", "workspace")]
        for sp in subproject_paths:
            sp_dir = project_root / sp

            # Check for npm/bun workspaces in nested package.json
            pkg_json = sp_dir / "package.json"
            if pkg_json.is_file():
                try:
                    pkg = json_mod.loads(pkg_json.read_text(encoding="utf-8", errors="ignore"))
                    workspaces = pkg.get("workspaces", [])
                    if isinstance(workspaces, dict):
                        workspaces = workspaces.get("packages", [])
                    if isinstance(workspaces, list) and workspaces:
                        import glob as glob_mod
                        for pattern in workspaces:
                            for match in glob_mod.glob(str(sp_dir / pattern)):
                                match_path = Path(match)
                                if match_path.is_dir() and (match_path / "package.json").is_file():
                                    rel = match_path.relative_to(project_root).as_posix()
                                    if rel not in seen:
                                        seen.add(rel)
                                        modules.append(self._build_module_entry(
                                            project_root, rel, "workspace", "javascript",
                                            self._find_entry_point(match_path),
                                        ))
                except (json_mod.JSONDecodeError, OSError):
                    pass

            # Check for pnpm workspaces in nested pnpm-workspace.yaml
            pnpm_ws = sp_dir / "pnpm-workspace.yaml"
            if pnpm_ws.is_file():
                try:
                    text = pnpm_ws.read_text(encoding="utf-8", errors="ignore")
                    import glob as glob_mod
                    in_packages = False
                    for line in text.splitlines():
                        stripped = line.strip()
                        if stripped == "packages:" or stripped.startswith("packages:"):
                            in_packages = True
                            continue
                        if in_packages and stripped.startswith("- "):
                            pat = stripped[2:].strip().strip("'\"")
                            if pat:
                                for match in glob_mod.glob(str(sp_dir / pat)):
                                    match_path = Path(match)
                                    if match_path.is_dir() and (match_path / "package.json").is_file():
                                        rel = match_path.relative_to(project_root).as_posix()
                                        if rel not in seen:
                                            seen.add(rel)
                                            modules.append(self._build_module_entry(
                                                project_root, rel, "workspace", "javascript",
                                                self._find_entry_point(match_path),
                                            ))
                        elif in_packages and not stripped.startswith("-") and not stripped.startswith("#"):
                            in_packages = False
                except OSError:
                    pass

    def _detect_dotnet_projects(
        self,
        project_root: Path,
        modules: list[dict[str, str | int | None]],
        seen: set[str],
    ) -> None:
        """Detect .NET projects from .csproj files (up to 3 levels deep)."""
        for depth_pattern in ["*.csproj", "*/*.csproj", "*/*/*.csproj"]:
            for csproj in project_root.glob(depth_pattern):
                module_dir = csproj.parent
                rel = module_dir.relative_to(project_root).as_posix()
                if rel == ".":
                    continue
                if rel not in seen and not any(skip in rel.lower().split("/") for skip in self._MODULE_SKIP_DIRS):
                    seen.add(rel)
                    modules.append(self._build_module_entry(
                        project_root, rel, "project", "csharp",
                        csproj.relative_to(project_root).as_posix(),
                    ))

    def _detect_informal_modules(
        self,
        project_root: Path,
        modules: list[dict[str, str | int | None]],
        seen: set[str],
    ) -> None:
        """Detect modules from top-level directories that look like independent modules."""
        for child in sorted(project_root.iterdir()):
            if not child.is_dir():
                continue
            name_lower = child.name.lower()
            if name_lower in self._MODULE_SKIP_DIRS or name_lower.startswith("."):
                continue
            rel = child.relative_to(project_root).as_posix()
            if rel in seen:
                continue

            # Check for manifest files
            for manifest_name, stack in self._MODULE_MANIFESTS.items():
                manifest = child / manifest_name
                if manifest.is_file():
                    # Skip if this is the root package.json's node_modules or similar
                    seen.add(rel)
                    modules.append(self._build_module_entry(
                        project_root, rel, "subproject", stack,
                        self._find_entry_point(child),
                    ))
                    break

            if rel in seen:
                continue

            # Check for entry point files
            entry = self._find_entry_point(child)
            if entry:
                stack = self._stack_from_entry(entry)
                seen.add(rel)
                modules.append(self._build_module_entry(
                    project_root, rel, "module", stack, entry,
                ))
                continue

            # Check if this is a well-known module-like directory with source files
            if name_lower in self._module_hint_dirs(project_root):
                source_count = sum(1 for _ in child.rglob("*") if _.is_file() and self._language_for(_, project_root=project_root) is not None)
                if source_count > 0:
                    stack = self._guess_stack_from_dir(project_root, child)
                    seen.add(rel)
                    modules.append(self._build_module_entry(
                        project_root, rel, "module", stack,
                        self._find_entry_point(child),
                    ))
                    continue

            # Fallback: any top-level dir with 2+ source files is an informal module
            source_count = 0
            checked = 0
            for f in child.rglob("*"):
                checked += 1
                if checked > 500:
                    break  # cap walk to avoid deep traversals
                if f.is_file() and self._language_for(f, project_root=project_root) is not None:
                    if not any(skip in f.relative_to(child).as_posix().lower().split("/") for skip in self._MODULE_SKIP_DIRS):
                        source_count += 1
                        if source_count >= 2:
                            break
            if source_count >= 2:
                stack = self._guess_stack_from_dir(project_root, child)
                seen.add(rel)
                modules.append(self._build_module_entry(
                    project_root, rel, "module", stack,
                    self._find_entry_point(child),
                ))

    def _build_module_entry(
        self,
        project_root: Path,
        rel_path: str,
        kind: str,
        stack: str | None,
        entry_point: str | None,
    ) -> dict[str, str | int | None]:
        name = rel_path.rsplit("/", 1)[-1]
        module_dir = project_root / rel_path
        # Count source files (fast — no deep parsing)
        file_count = 0
        try:
            for f in module_dir.rglob("*"):
                if f.is_file() and self._language_for(f, project_root=project_root) is not None:
                    if not self._should_skip(project_root, f, include_tests=False):
                        file_count += 1
        except OSError:
            pass
        description = self._describe_module(name, kind, stack, file_count)
        return {
            "module_path": rel_path,
            "name": name,
            "kind": kind,
            "stack": stack,
            "entry_point": entry_point,
            "file_count": file_count,
            "description": description,
        }

    def _find_entry_point(self, directory: Path) -> str | None:
        patterns = self._get_entry_point_patterns(getattr(self, '_last_project_root', None) or directory)
        for pattern, _ in patterns:
            candidate = directory / pattern
            if candidate.is_file():
                return candidate.name
        return None

    def _stack_from_entry(self, entry: str) -> str | None:
        patterns = self._get_entry_point_patterns(getattr(self, '_last_project_root', None) or Path('.'))
        for pattern, stack in patterns:
            if entry == pattern:
                return stack
        return None

    def _guess_stack_from_dir(self, project_root: Path, directory: Path) -> str | None:
        """Guess the primary stack of a directory by counting file extensions."""
        counts: dict[str, int] = {}
        try:
            for f in directory.rglob("*"):
                if f.is_file():
                    lang = self._language_for(f, project_root=project_root)
                    if lang:
                        counts[lang] = counts.get(lang, 0) + 1
        except OSError:
            return None
        if not counts:
            return None
        return max(counts, key=lambda k: counts[k])

    def _describe_module(self, name: str, kind: str, stack: str | None, file_count: int) -> str:
        stack_label = f" ({stack})" if stack else ""
        return f"{kind}: {name}{stack_label}, {file_count} source files"

    def sync_modules(self, project_root: Path) -> int:
        """Detect and persist module boundaries into the code_modules table."""
        self.init_db(project_root)
        modules = self.detect_modules(project_root)
        with self.connect(project_root) as conn:
            conn.execute("DELETE FROM code_modules")
            conn.executemany(
                "INSERT INTO code_modules (module_path, name, kind, stack, entry_point, file_count, description) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (m["module_path"], m["name"], m["kind"], m["stack"], m["entry_point"], m["file_count"], m["description"])
                    for m in modules
                ],
            )
            # Reset and re-tag all code_files with their module
            conn.execute("UPDATE code_files SET module = NULL")
            for m in modules:
                module_prefix = m["module_path"] + "/"
                conn.execute(
                    "UPDATE code_files SET module = ? WHERE path LIKE ?",
                    (m["module_path"], module_prefix + "%"),
                )
        return len(modules)

    def get_modules(self, project_root: Path, kind: str | None = None) -> list[dict[str, object]]:
        """Query detected modules."""
        self.init_db(project_root)
        with self.connect(project_root) as conn:
            if kind:
                rows = conn.execute(
                    "SELECT * FROM code_modules WHERE kind = ? ORDER BY module_path", (kind,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM code_modules ORDER BY module_path").fetchall()
        return [dict(r) for r in rows]

    def get_module_files(self, project_root: Path, module_path: str, limit: int = 200) -> list[dict[str, object]]:
        """Get all indexed files belonging to a module."""
        self.init_db(project_root)
        with self.connect(project_root) as conn:
            rows = conn.execute(
                "SELECT path, language, role, line_count, summary FROM code_files WHERE module = ? ORDER BY path LIMIT ?",
                (module_path, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def sync_code_manifest(self, project_root: Path, include_tests: bool = False) -> int:
        self.init_db(project_root)
        manifest_rows: list[tuple[str, str, str | None, str | None, str, int, int]] = []
        seen_paths: set[str] = set()

        for path in self._walk_source_files(project_root, include_tests=include_tests):
            if not path.is_file():
                continue
            rel = path.relative_to(project_root).as_posix()
            code_language = self._language_for(path, project_root=project_root)
            if code_language is None:
                continue
            descriptor = descriptor_for_language(project_root, rel, path.suffix.lower())
            language_tier = descriptor.tier if descriptor else None
            language_source = descriptor.source if descriptor else None
            stat = path.stat()
            seen_paths.add(rel)
            role = self._infer_code_role(project_root, rel, code_language, [])
            manifest_rows.append((rel, code_language, language_tier, language_source, role, int(stat.st_size), int(stat.st_mtime_ns)))

        with self.connect(project_root) as conn:
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
        self.init_db(project_root)
        self.sync_code_manifest(project_root, include_tests=include_tests)
        rows: list[tuple[str, str, str | None, str | None, str, int, str, str | None, int, int, int]] = []
        outline_rows: list[tuple[str, str, str, int, str | None, int]] = []
        edge_rows: list[tuple[str, str, str]] = []

        scoped_paths = None
        if paths is not None:
            scoped_paths = {item.replace("\\", "/") for item in paths if str(item).strip()}

        existing_meta = {}
        with self.connect(project_root) as conn:
            for row in conn.execute("SELECT path, checksum, size_bytes, mtime_ns, language, language_tier, language_source, line_count, summary, role, parsed FROM code_files"):
                existing_meta[row["path"]] = dict(row)

        seen_paths: set[str] = set()
        for path in self._walk_source_files(project_root, include_tests=include_tests):
            if not path.is_file():
                continue
            rel = path.relative_to(project_root).as_posix()
            if scoped_paths is not None and rel not in scoped_paths:
                continue
            seen_paths.add(rel)
            code_language = self._language_for(path, project_root=project_root)
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
            outlines = self._extract_outline(project_root, text, code_language)
            role = self._infer_code_role(project_root, rel, code_language, outlines)
            rows.append((rel, code_language, language_tier, language_source, checksum, line_count, summary, role, size_bytes, mtime_ns))
            rows[-1] = (*rows[-1], 1)
            outline_rows.extend(
                (rel, symbol, kind, line_number, container, 1 if is_partial else 0)
                for symbol, kind, line_number, container, is_partial in outlines
            )
            edge_rows.extend((rel, target, kind) for target, kind in self._extract_edges(text, code_language))

        with self.connect(project_root) as conn:
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
        if self.session_store is None:
            raise RuntimeError("SessionStore is required for session-guided code sync")
        paths = self.session_store.session_code_targets(project_root, session_id)
        return self.sync_code_files(project_root, paths=paths, include_tests=include_tests)

    def code_status(self, project_root: Path) -> dict[str, object]:
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
            "language_tiers": tiers,
            "language_sources": sources,
            "freshness": self._code_freshness(project_root),
        }

    def _code_freshness(self, project_root: Path) -> dict[str, object]:
        indexed_rows: dict[str, sqlite3.Row] = {}
        with self.connect(project_root) as conn:
            for row in conn.execute("SELECT path, checksum, mtime_ns, parsed FROM code_files ORDER BY path"):
                indexed_rows[str(row["path"])] = row

        include_tests = any(self._path_looks_like_test(path) for path in indexed_rows)
        tracked_paths: dict[str, dict[str, int | str]] = {}
        for path in self._walk_source_files(project_root, include_tests=include_tests):
            if not path.is_file():
                continue
            rel = path.relative_to(project_root).as_posix()
            code_language = self._language_for(path, project_root=project_root)
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

    def search_code(self, project_root: Path, query: str, limit: int = 10) -> list[dict[str, str | int]]:
        self.init_db(project_root)
        needle = query.strip()
        if not needle:
            return []
        pattern = f"%{needle}%"
        with self.connect(project_root) as conn:
            rows = conn.execute(
                """
                SELECT path, language, language_tier, language_source, line_count, summary, role
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
            tier = str(row["language_tier"] or "unknown")
            tier_weight = {"rich": 10, "heuristic": 3, "summary": 1}.get(tier, 0)
            score += tier_weight
            if tier_weight:
                reasons.append(f"tier_weight:{tier_weight}")
            score -= row["path"].count("/")
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
        """Check if a file has been modified outside AIDOCS since last index."""
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
            return True
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
        limit: int = 50,
        include_tests: bool = False,
    ) -> list[dict[str, object]]:
        """Search indexed file contents for literal text. Supports | as OR delimiter."""
        self.init_db(project_root)
        raw = text.strip()
        if not raw:
            return []

        # Split on | for OR queries; each term is a separate needle
        needles = [t.strip() for t in raw.split("|") if t.strip()]
        if not needles:
            return []
        if not case_sensitive:
            needles = [n.lower() for n in needles]

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
            if not any(n in search_content for n in needles):
                continue
            lines_matched: list[dict[str, object]] = []
            for i, line in enumerate(content.splitlines(), 1):
                check_line = line if case_sensitive else line.lower()
                if any(n in check_line for n in needles):
                    lines_matched.append({"line_number": i, "line": line.rstrip()})
                    if len(lines_matched) >= 5:
                        break
            total_count = sum(search_content.count(n) for n in needles)
            matches.append({
                "path": rel_path,
                "match_count": total_count,
                "lines": lines_matched,
            })
            if len(matches) >= limit:
                break

        return matches


    def find_symbol_range(
        self,
        project_root: Path,
        path: str,
        symbol: str,
        kind: str | None = None,
        line_number: int | None = None,
    ) -> dict[str, object]:
        """Find the start and end line of a symbol using indexed outlines + block extraction."""
        self.init_db(project_root)
        with self.connect(project_root) as conn:
            if line_number is not None:
                row = conn.execute(
                    "SELECT o.symbol, o.kind, o.line_number, f.language FROM code_outlines o "
                    "JOIN code_files f ON f.path = o.path WHERE o.path = ? AND o.symbol = ? AND o.line_number = ? LIMIT 1",
                    (path, symbol, line_number),
                ).fetchone()
            elif kind is not None:
                row = conn.execute(
                    "SELECT o.symbol, o.kind, o.line_number, f.language FROM code_outlines o "
                    "JOIN code_files f ON f.path = o.path WHERE o.path = ? AND o.symbol = ? AND o.kind = ? ORDER BY o.line_number ASC LIMIT 1",
                    (path, symbol, kind),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT o.symbol, o.kind, o.line_number, f.language FROM code_outlines o "
                    "JOIN code_files f ON f.path = o.path WHERE o.path = ? AND o.symbol = ? ORDER BY o.line_number ASC LIMIT 1",
                    (path, symbol),
                ).fetchone()

        if row is None:
            return {"error": f"Symbol '{symbol}' not found in {path}"}

        abs_path = project_root / path
        text = abs_path.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()
        start_idx = max(0, int(row["line_number"]) - 1)
        lang = row["language"]

        if lang == "python":
            snippet = self._extract_indent_block(lines, start_idx)
        elif lang in {"javascript", "typescript", "jsx", "tsx", "csharp"}:
            snippet = self._extract_brace_block(lines, start_idx)
        else:
            snippet = "\n".join(lines[start_idx:min(len(lines), start_idx + 20)])

        end_line = int(row["line_number"]) + snippet.count("\n")

        return {
            "path": path,
            "symbol": row["symbol"],
            "kind": row["kind"],
            "start": int(row["line_number"]),
            "end": end_line,
            "lines": end_line - int(row["line_number"]) + 1,
        }



    def suggest_extractions(
        self,
        project_root: Path,
        path: str,
        min_lines: int = 20,
        limit: int = 10,
    ) -> list[dict[str, object]]:
        """Suggest symbols that are good extraction candidates based on size and cohesion."""
        self.init_db(project_root)
        abs_path = (project_root / path.replace("\\", "/")).resolve()
        if not abs_path.is_file():
            return []
        text = abs_path.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()

        with self.connect(project_root) as conn:
            rows = conn.execute(
                "SELECT symbol, kind, line_number, container FROM code_outlines WHERE path = ? ORDER BY line_number",
                (path.replace("\\", "/"),),
            ).fetchall()

        if not rows:
            return []

        # Get language for block extraction
        lang_row = conn.execute("SELECT language FROM code_files WHERE path = ?", (path.replace("\\", "/"),)).fetchone()
        language = lang_row["language"] if lang_row else "unknown"

        candidates: list[dict[str, object]] = []
        for row in rows:
            start_idx = max(0, int(row["line_number"]) - 1)
            if language == "python":
                snippet = self._extract_indent_block(lines, start_idx)
            elif language in {"javascript", "typescript", "jsx", "tsx", "csharp"}:
                snippet = self._extract_brace_block(lines, start_idx)
            else:
                continue
            line_count = snippet.count("\n") + 1
            if line_count < min_lines:
                continue

            kind = row["kind"]
            container = row["container"]
            # Skip nested functions — they move with their parent
            if container and kind == "function":
                continue

            candidates.append({
                "symbol": row["symbol"],
                "kind": kind,
                "start": int(row["line_number"]),
                "lines": line_count,
                "container": container,
            })

        candidates.sort(key=lambda c: -c["lines"])
        return candidates[:limit]


    def find_stale_references(
        self,
        project_root: Path,
        symbols: list[str],
        *,
        exclude_path: str | None = None,
        include_tests: bool = False,
        limit: int = 50,
    ) -> list[dict[str, object]]:
        """Find remaining references to removed/renamed symbols across the project."""
        if not symbols:
            return []
        results: list[dict[str, object]] = []
        for symbol in symbols:
            matches = self.search_text(
                project_root,
                symbol,
                include_tests=include_tests,
                limit=limit,
            )
            for match in matches:
                path = str(match.get("path", ""))
                if exclude_path and path == exclude_path.replace("\\", "/").strip():
                    continue
                results.append({
                    "symbol": symbol,
                    "path": path,
                    "match_count": match.get("match_count", 0),
                    "lines": match.get("lines", []),
                })
        return results[:limit]

    def find_dead_code(
        self,
        project_root: Path,
        path: str,
    ) -> dict[str, object]:
        """Find dead imports and unused locals in a single file."""
        abs_path = (project_root / path.replace("\\", "/")).resolve()
        if not abs_path.is_file():
            return {"path": path, "dead_imports": [], "unused_locals": [], "error": f"File not found: {path}"}
        try:
            text = abs_path.read_text(encoding="utf-8")
        except Exception as exc:
            return {"path": path, "dead_imports": [], "unused_locals": [], "error": str(exc)}

        ext = abs_path.suffix.lower()
        if ext == ".py":
            return self._find_dead_code_python(path, text)
        if ext in {".js", ".ts", ".jsx", ".tsx"}:
            return self._find_dead_code_js(path, text)
        return {"path": path, "dead_imports": [], "unused_locals": [], "error": f"Unsupported language: {ext}"}

    @staticmethod
    def _find_dead_code_python(path: str, text: str) -> dict[str, object]:
        import ast

        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            return {"path": path, "dead_imports": [], "unused_locals": [], "error": str(exc)}

        # Find TYPE_CHECKING guarded import lines to exclude
        type_checking_lines: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING":
                for child in ast.walk(node):
                    if isinstance(child, (ast.Import, ast.ImportFrom)):
                        type_checking_lines.add(child.lineno)

        # Collect all imported names (skip __future__ and TYPE_CHECKING)
        imported: dict[str, int] = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)) and node.lineno in type_checking_lines:
                continue
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.asname or alias.name.split(".")[0]
                    imported[name] = node.lineno
            elif isinstance(node, ast.ImportFrom):
                if node.module == "__future__":
                    continue
                for alias in node.names:
                    name = alias.asname or alias.name
                    if name != "*":
                        imported[name] = node.lineno

        # Collect all Name references (reads) + names in string annotations
        used_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                used_names.add(node.id)
            elif isinstance(node, ast.Attribute):
                if isinstance(node.value, ast.Name):
                    used_names.add(node.value.id)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                # String annotations reference type names
                import re as _re
                for word in _re.findall(r'[A-Za-z_]\w*', node.value):
                    used_names.add(word)

        dead_imports = [
            {"name": name, "line": line}
            for name, line in sorted(imported.items(), key=lambda x: x[1])
            if name not in used_names
        ]

        # Collect assigned locals at module level that are never read
        # (skip _ prefixed names — convention for intentional unused)
        assigned: dict[str, int] = {}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and not target.id.startswith("_"):
                        assigned[target.id] = node.lineno

        unused_locals = [
            {"name": name, "line": line}
            for name, line in sorted(assigned.items(), key=lambda x: x[1])
            if name not in used_names and name not in imported
        ]

        return {"path": path, "dead_imports": dead_imports, "unused_locals": unused_locals}

    @staticmethod
    def _find_dead_code_js(path: str, text: str) -> dict[str, object]:
        import re

        import_pattern = re.compile(
            r"""(?:import\s+(?:type\s+)?(?:\{([^}]+)\}|(\w+)).*?from|import\s+(\w+)\s+from)""",
            re.MULTILINE,
        )
        all_lines = text.splitlines()
        dead_imports: list[dict[str, object]] = []
        for i, line in enumerate(all_lines, 1):
            m = import_pattern.search(line)
            if not m:
                continue
            names: list[str] = []
            if m.group(1):
                names = [n.strip().split(" as ")[-1].strip() for n in m.group(1).split(",") if n.strip()]
            elif m.group(2):
                names = [m.group(2)]
            elif m.group(3):
                names = [m.group(3)]
            # Check each imported name against all lines AFTER this import
            rest = "\n".join(all_lines[i:])
            for name in names:
                if name and not re.search(r'\b' + re.escape(name) + r'\b', rest):
                    dead_imports.append({"name": name, "line": i})

        return {"path": path, "dead_imports": dead_imports, "unused_locals": []}




    def search_symbols(
        self,
        project_root: Path,
        query: str,
        kind: str | None = None,
        role: str | None = None,
        limit: int = 25,
    ) -> list[dict[str, str | int | bool | None]]:
        self.init_db(project_root)
        needle = query.strip()

        # Allow searching by kind alone (no symbol name needed)
        if not needle and not kind:
            return []

        with self.connect(project_root) as conn:
            if needle:
                self._ensure_parsed_candidates(project_root, needle, limit=limit * 4)
                variants = self._concept_variants(needle)

                # Build multi-word CamelCase variants for priority matching
                needle_words = needle.split()
                priority_needles: list[str] = [needle]
                if len(needle_words) > 1:
                    priority_needles.append("".join(w.capitalize() for w in needle_words))
                    priority_needles.append(needle_words[0].lower() + "".join(w.capitalize() for w in needle_words[1:]))
                priority_needles = list(dict.fromkeys(priority_needles))  # dedupe preserving order

                join_clause = "JOIN code_files cf ON cf.path = co.path" if role else ""
                kind_filter = " AND co.kind = ?" if kind else ""
                role_filter = " AND cf.role = ?" if role else ""
                extra_params: list[object] = []
                if kind:
                    extra_params.append(kind)
                if role:
                    extra_params.append(role)

                # Phase 1: exact/prefix matches on original needle and CamelCase joins
                seen_keys: set[tuple[str, str, int]] = set()
                rows: list[sqlite3.Row] = []

                for pn in priority_needles:
                    pn_params: list[object] = [f"{pn}%", f"%{pn}%"]
                    pn_params.extend(extra_params)
                    phase1 = conn.execute(
                        f"""
                        SELECT co.path, co.symbol, co.kind, co.line_number, co.container, co.is_partial
                        FROM code_outlines co
                        {join_clause}
                        WHERE (co.symbol LIKE ? OR co.symbol LIKE ?){kind_filter}{role_filter}
                        LIMIT 100
                        """,
                        pn_params,
                    ).fetchall()
                    for r in phase1:
                        key = (r["path"], r["symbol"], r["line_number"])
                        if key not in seen_keys:
                            seen_keys.add(key)
                            rows.append(r)

                # Phase 2: broader variant matches to fill remaining slots
                broad_clauses = " OR ".join(["co.symbol LIKE ? OR COALESCE(co.container, '') LIKE ?" for _ in variants])
                broad_params: list[object] = []
                for variant in variants:
                    pattern = f"%{variant}%"
                    broad_params.extend([pattern, pattern])
                broad_params.extend(extra_params)

                phase2 = conn.execute(
                    f"""
                    SELECT co.path, co.symbol, co.kind, co.line_number, co.container, co.is_partial
                    FROM code_outlines co
                    {join_clause}
                    WHERE ({broad_clauses}){kind_filter}{role_filter}
                    LIMIT 500
                    """,
                    broad_params,
                ).fetchall()
                for r in phase2:
                    key = (r["path"], r["symbol"], r["line_number"])
                    if key not in seen_keys:
                        seen_keys.add(key)
                        rows.append(r)
            else:
                join_clause = "JOIN code_files cf ON cf.path = co.path" if role else ""
                where = "1=1"
                params: list[object] = []
                if kind:
                    where += " AND co.kind = ?"
                    params.append(kind)
                if role:
                    where += " AND cf.role = ?"
                    params.append(role)
                rows = conn.execute(
                    f"""
                    SELECT co.path, co.symbol, co.kind, co.line_number, co.container, co.is_partial
                    FROM code_outlines co
                    {join_clause}
                    WHERE {where}
                    ORDER BY co.path, co.line_number
                    LIMIT 500
                    """,
                    params,
                ).fetchall()
        namespace_cache: dict[str, str | None] = {}
        ranked = []
        kind_weight = {
            "class": 30,
            "record": 28,
            "struct": 26,
            "interface": 24,
            "type_alias": 23,
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
        # Pre-compute CamelCase variant of needle for multi-word scoring
        needle_words = needle.split()
        needle_variants_for_scoring: list[str] = [needle]
        if len(needle_words) > 1:
            needle_variants_for_scoring.append("".join(w.capitalize() for w in needle_words))
            needle_variants_for_scoring.append(needle_words[0].lower() + "".join(w.capitalize() for w in needle_words[1:]))
            needle_variants_for_scoring.append("_".join(w.lower() for w in needle_words))
        elif any(c.isupper() for c in needle[1:]):
            # CamelCase input — also score against space-separated words
            camel_split = re.sub(r'([a-z])([A-Z])', r'\1 \2', needle)
            needle_variants_for_scoring.append(camel_split.lower())

        for row in rows:
            score = 0
            reasons: list[str] = []
            # Score against all needle variants, take the best
            best_symbol_score = 0
            for nv in needle_variants_for_scoring:
                nv_reasons: list[str] = []
                s = self._score_text_match(nv, row["symbol"], exact=140, prefix=100, contains=70, reasons=nv_reasons, label="symbol")
                if s > best_symbol_score:
                    best_symbol_score = s
                    reasons = nv_reasons
            score += best_symbol_score
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
            # For kind-only queries, boost by role relevance (services > utilities > tests)
            if not needle:
                role_boost = self._role_relevance_boost(project_root, str(row["path"]))
                score += role_boost
                if role_boost:
                    reasons.append(f"role_boost:{role_boost}")
            score -= row["path"].count("/")
            ranked.append((score, row, reasons))
        ranked.sort(key=lambda item: (-item[0], item[1]["path"], int(item[1]["line_number"])))
        return [
            {
                "path": row["path"],
                "symbol": row["symbol"],
                "kind": row["kind"],
                "line_number": int(row["line_number"]),
                **({"container": row["container"]} if row["container"] else {}),
                **(
                    {"namespace": namespace}
                    if (namespace := self._namespace_for_path(project_root, str(row["path"]), namespace_cache))
                    else {}
                ),
                **({"is_partial": True} if row["is_partial"] else {}),
                "why": reasons,
            }
            for _, row, reasons in ranked[:limit]
        ]

    def get_method_signature(
        self,
        project_root: Path,
        method_name: str,
        container: str | None = None,
        limit: int = 20,
    ) -> dict[str, object]:
        self.init_db(project_root)
        matches = self.search_symbols(project_root, query=method_name, kind="method", limit=max(limit * 3, 20))
        filtered = [m for m in matches if not container or str(m.get("container") or "") == container][:limit]
        signatures = []
        for item in filtered:
            signature = self._extract_method_signature(project_root, str(item["path"]), int(item["line_number"]))
            signatures.append(
                {
                    **item,
                    **signature,
                }
            )
        return {
            "method": method_name,
            "container": container,
            "matches": signatures,
        }

    def get_method_signatures(
        self,
        project_root: Path,
        methods: list[str],
        container: str | None = None,
        limit_per_method: int = 20,
    ) -> dict[str, object]:
        results = []
        for method in methods:
            if not method or not method.strip():
                continue
            payload = self.get_method_signature(
                project_root,
                method_name=method.strip(),
                container=None,
                limit=max(limit_per_method * 3, 20),
            )
            matches = payload.get("matches", []) if isinstance(payload, dict) else []
            if container:
                preferred = [item for item in matches if str(item.get("container") or "") == container]
                others = [item for item in matches if str(item.get("container") or "") != container]
                matches = preferred + others
            payload["container"] = container
            payload["matches"] = matches[:limit_per_method]
            results.append(payload)
        return {
            "container": container,
            "methods": results,
        }

    def get_enum_values(self, project_root: Path, enum_name: str, limit: int = 50, include_related: bool = False) -> dict[str, object]:
        self.init_db(project_root)
        enums = self.search_symbols(project_root, query=enum_name, kind="enum", limit=limit)
        exact = [item for item in enums if str(item.get("symbol") or "") == enum_name]
        fuzzy = [item for item in enums if str(item.get("symbol") or "") != enum_name]
        enums = exact + fuzzy if include_related else exact or fuzzy[:1]
        matches = []
        for enum_item in enums[:limit]:
            values = self._enum_members_for_container(project_root, str(enum_item["path"]), str(enum_item["symbol"]))
            matches.append({**enum_item, "values": values})
        return {
            "enum": enum_name,
            "include_related": include_related,
            "matches": matches,
        }

    def get_constructor_params(
        self,
        project_root: Path,
        type_name: str,
        limit: int = 20,
        include_related: bool = False,
    ) -> dict[str, object]:
        self.init_db(project_root)
        matches = self.search_symbols(project_root, query=type_name, kind="record", limit=max(limit * 2, 20))
        if not matches:
            matches = self.search_symbols(project_root, query=type_name, kind="class", limit=max(limit * 2, 20))
        exact = [item for item in matches if str(item.get("symbol") or "") == type_name]
        fuzzy = [item for item in matches if str(item.get("symbol") or "") != type_name]
        matches = exact + fuzzy if include_related else exact or fuzzy[:1]
        results = []
        for item in matches[:limit]:
            constructor = self._extract_constructor_params(project_root, str(item["path"]), str(item["symbol"]))
            results.append({**item, **constructor})
        return {
            "type": type_name,
            "include_related": include_related,
            "matches": results,
        }

    def get_constructor_params_batch(
        self,
        project_root: Path,
        types: list[str],
        include_related: bool = False,
        limit_per_type: int = 20,
    ) -> dict[str, object]:
        results = []
        for type_name in types:
            if not type_name or not type_name.strip():
                continue
            results.append(
                self.get_constructor_params(
                    project_root,
                    type_name=type_name.strip(),
                    limit=limit_per_type,
                    include_related=include_related,
                )
            )
        return {
            "types": results,
            "include_related": include_related,
        }

    def get_service_api(self, project_root: Path, service_name: str, limit: int = 100) -> dict[str, object]:
        self.init_db(project_root)
        service_matches = self.search_symbols(project_root, query=service_name, kind="class", limit=max(limit, 20))
        exact = next((item for item in service_matches if str(item.get("symbol") or "") == service_name), None)
        if not exact:
            return {"service": service_name, "match": None, "methods": [], "not_found": True}
        target = exact

        with self.connect(project_root) as conn:
            rows = conn.execute(
                "SELECT path, symbol, kind, line_number, container, is_partial FROM code_outlines WHERE kind = 'method' AND container = ? ORDER BY path, line_number LIMIT ?",
                (service_name, limit),
            ).fetchall()
        methods = []
        namespace_cache: dict[str, str | None] = {}
        for row in rows:
            base = {
                "path": row["path"],
                "symbol": row["symbol"],
                "kind": row["kind"],
                "line_number": int(row["line_number"]),
                **({"container": row["container"]} if row["container"] else {}),
                **(
                    {"namespace": namespace}
                    if (namespace := self._namespace_for_path(project_root, str(row["path"]), namespace_cache))
                    else {}
                ),
            }
            methods.append({**base, **self._extract_method_signature(project_root, str(row["path"]), int(row["line_number"]))})
        if not methods or len({m["path"] for m in methods}) < 2:
            fallback_methods = self._extract_service_methods_from_declaring_files(project_root, service_name, limit=limit)
            seen = {(m.get("path"), m.get("symbol"), m.get("line_number")) for m in methods}
            for item in fallback_methods:
                key = (item.get("path"), item.get("symbol"), item.get("line_number"))
                if key not in seen:
                    seen.add(key)
                    methods.append(item)
        # Deduplicate: hoist common container/namespace to top level
        containers = {m.get("container") for m in methods if m.get("container")}
        namespaces = {m.get("namespace") for m in methods if m.get("namespace")}
        if len(containers) == 1:
            common_container = containers.pop()
            for m in methods:
                m.pop("container", None)
        else:
            common_container = None
        if len(namespaces) == 1:
            common_namespace = namespaces.pop()
            for m in methods:
                m.pop("namespace", None)
        else:
            common_namespace = None

        result: dict[str, object] = {"service": service_name, "match": target}
        if common_container:
            result["container"] = common_container
        if common_namespace:
            result["namespace"] = common_namespace
        result["method_count"] = len(methods)
        result["methods"] = methods
        return result

    def get_entity_properties(self, project_root: Path, entity_name: str) -> dict[str, object]:
        try:
            from .schema_index_store import SchemaIndexStore

            result = SchemaIndexStore().get_entity_properties(project_root, entity_name)
            if not result.get("properties"):
                result["note"] = "No class-style properties found. If this is a record or constructor-heavy type, use code_get_constructor_params."
            return result
        except Exception:
            return {
                "entity_name": entity_name,
                "properties": [],
                "note": "No class-style properties found. If this is a record or constructor-heavy type, use code_get_constructor_params.",
            }

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
            "source": "file_content",
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
        self.init_db(project_root)
        needle = concept.strip()
        if not needle:
            return {"concept": concept, "matches": []}

        self._ensure_parsed_candidates(project_root, needle, limit=limit * 4)
        symbol_matches = self.search_symbols(project_root, query=needle, limit=limit)
        references = self.find_references(project_root, symbol=needle, limit=limit)["matches"]
        code_matches = self.search_code(project_root, query=needle, limit=limit)

        mutation_tokens = ("set", "update", "save", "create", "delete", "remove", "toggle", "apply", "sync", "write", "assign", "change", "complete")

        # Also search for methods INSIDE the queried container (e.g., query="CashFlowService" finds CreateAccountAsync inside it)
        with self.connect(project_root) as conn:
            mutation_like_clauses = " OR ".join(["LOWER(co.symbol) LIKE ?" for _ in mutation_tokens])
            mutation_like_params = [f"%{t}%" for t in mutation_tokens]
            container_methods = conn.execute(
                f"""
                SELECT co.path, co.symbol, co.kind, co.line_number, co.container
                FROM code_outlines co
                WHERE co.container = ? AND co.kind = 'method'
                  AND ({mutation_like_clauses})
                LIMIT ?
                """,
                [needle] + mutation_like_params + [limit * 2],
            ).fetchall()
            for row in container_methods:
                # Add as symbol match so the main loop processes it
                symbol_matches.append({
                    "path": row["path"], "symbol": row["symbol"],
                    "kind": row["kind"], "line_number": row["line_number"],
                    "container": row["container"],
                })
        factory_tokens = ("factory", "fixture", "testbase")
        merged: list[dict[str, object]] = []
        seen: set[tuple[str, str | None, int | None]] = set()

        lower_needle = needle.lower()
        # Extract concept tokens for container matching (e.g., "CashFlowService" -> "cashflowservice")
        needle_tokens = [t.lower() for t in re.split(r'[.\s]+', needle) if t]

        for item in symbol_matches:
            path = str(item["path"])
            symbol = str(item["symbol"])
            container = str(item.get("container") or "")
            lower_symbol = symbol.lower()
            lower_path = path.lower()
            lower_container = container.lower()
            score = self._score_text_match(needle, symbol, exact=90, prefix=60, contains=35)

            # Also check if the query matches the container (e.g., query="CashFlowService", container="CashFlowService")
            container_match = self._score_text_match(needle, container, exact=50, prefix=35, contains=20) if container else 0
            # Or if any needle token appears in the container/path
            context_match = any(t in lower_container or t in lower_path for t in needle_tokens)

            token_bonus = 0
            for token in mutation_tokens:
                if token in lower_symbol:
                    token_bonus += 25
            # Skip only if: no mutation token AND no concept match (symbol, container, or path)
            if token_bonus == 0 and score == 0 and container_match == 0 and not context_match:
                continue
            # If mutation token present but no direct symbol match, use container match as base
            if score == 0 and container_match > 0:
                score = container_match
            score += token_bonus
            layer = self._infer_layer_from_path(path)
            score += self._path_weight(project_root, path)
            if layer in {"logic", "api", "ui"}:
                score += 10
            if any(token in lower_path for token in factory_tokens) or any(token in lower_symbol for token in factory_tokens):
                score -= 25
            if "/test" in lower_path or "tests/" in lower_path:
                score -= 20
            key = (path, symbol, int(item["line_number"]))
            if key in seen:
                continue
            seen.add(key)
            # Only fetch snippets for actual mutation methods (have token_bonus),
            # skip snippets for class definitions and context-only matches to reduce output size
            snippet = None
            kind_str = str(item["kind"])
            if token_bonus > 0 and kind_str in ("method", "function"):
                try:
                    snippet = self.get_symbol_snippet(project_root, path=path, symbol=symbol, kind=kind_str, line_number=int(item["line_number"]))
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
                    "container": item.get("container"),
                    "snippet": snippet["snippet"] if snippet else None,
                }
            )

        line_pattern = re.compile(rf"\b{re.escape(needle)}\b", re.IGNORECASE)
        for item in references:
            path = str(item["path"])
            line = str(item["line"])
            lower_line = line.lower()
            lower_path = path.lower()
            token_bonus = 0
            for token in mutation_tokens:
                if token in lower_line:
                    token_bonus += 18
            if token_bonus == 0 or not line_pattern.search(line):
                continue
            score = 70 + token_bonus + self._path_weight(project_root, path)
            if any(token in lower_path for token in factory_tokens):
                score -= 20
            if "/test" in lower_path or "tests/" in lower_path:
                score -= 15
            key = (path, None, int(item["line_number"]))
            if key in seen:
                continue
            seen.add(key)
            merged.append(
                {
                    "score": score,
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
            lower_summary = str(item["summary"] or "").lower()
            token_hits = sum(1 for token in mutation_tokens if token in lower_summary)
            if token_hits == 0 and not any(token in lower_path for token in mutation_tokens):
                continue
            key = (path, None, None)
            if key in seen:
                continue
            seen.add(key)
            score = 15 + token_hits * 18 + self._path_weight(project_root, path)
            if any(token in lower_path for token in factory_tokens) or any(token in lower_summary for token in factory_tokens):
                score -= 25
            if "/test" in lower_path or "tests/" in lower_path:
                score -= 20
            merged.append(
                {
                    "score": score,
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
                    "container": item.get("container"),
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
                    "container": item.get("container"),
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
                    "container": item.get("container"),
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
                    "container": item.get("container"),
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
        query = concept.strip()
        service_name = ""
        method_name = ""
        if "." in query:
            left, right = query.split(".", 1)
            service_name = left.strip()
            method_name = right.strip()

        routes = self.find_routes(project_root, query=query, limit=limit)["matches"]
        touchpoints = self.find_ui_backend_touchpoints(project_root, concept=query, limit=limit)["matches"]
        clusters = self.find_domain_clusters(project_root, concept=query, limit=limit)["cluster"]

        if method_name:
            refs = self.find_references(project_root, symbol=method_name, limit=limit * 3).get("matches", [])
            for ref in refs:
                path = str(ref.get("path") or "")
                layer = str(ref.get("layer") or self._infer_layer_from_path(path))
                touchpoints.append(
                    {
                        "score": 130 if layer == "api" else 90 if layer == "ui" else 75,
                        "path": path,
                        "layer": layer,
                        "symbol": method_name,
                        "kind": "reference",
                        "line_number": ref.get("line_number"),
                        "container": None,
                        "snippet": ref.get("line"),
                    }
                )
        if service_name:
            service_symbols = self.search_symbols(project_root, query=service_name, limit=limit * 3)
            for item in service_symbols:
                path = str(item.get("path") or "")
                symbol = str(item.get("symbol") or "")
                container = str(item.get("container") or "")
                # Only include symbols that actually relate to the service
                text_match = self._score_text_match(service_name, symbol, exact=80, prefix=50, contains=30)
                container_match = self._score_text_match(service_name, container, exact=60, prefix=40, contains=20)
                if text_match == 0 and container_match == 0:
                    continue
                layer = self._infer_layer_from_path(path)
                base_score = max(text_match, container_match)
                layer_bonus = 30 if layer == "api" else 15 if layer == "logic" else 0
                touchpoints.append(
                    {
                        "score": base_score + layer_bonus,
                        "path": path,
                        "layer": layer,
                        "symbol": symbol,
                        "kind": item.get("kind"),
                        "line_number": item.get("line_number"),
                        "container": item.get("container"),
                        "snippet": None,
                    }
                )

        def dedupe(items: list[dict[str, object]]) -> list[dict[str, object]]:
            seen: set[tuple[str, str | None, int | None]] = set()
            ordered: list[dict[str, object]] = []
            for item in sorted(items, key=lambda x: (-int(x.get("score", 0)), str(x.get("path") or ""), int(x.get("line_number") or 0))):
                key = (str(item.get("path") or ""), str(item.get("symbol") or ""), int(item.get("line_number") or 0) or None)
                if key in seen:
                    continue
                seen.add(key)
                ordered.append(item)
            return ordered

        api_side = dedupe([item for item in routes + touchpoints + clusters if item.get("layer") == "api"])
        ui_side = dedupe([item for item in touchpoints + clusters if item.get("layer") == "ui"])
        # Filter logic results by minimum score to suppress unrelated interface noise
        min_logic_score = 30
        logic_side = dedupe([item for item in touchpoints + clusters if item.get("layer") == "logic" and int(item.get("score", 0)) >= min_logic_score])

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
            container = str(item.get("container") or "")
            text_score = self._score_text_match(needle, symbol, exact=120, prefix=90, contains=60)
            # Also check container match (e.g., query="CompleteItemAsync" in container "DocumentService")
            container_score = self._score_text_match(needle, container, exact=80, prefix=50, contains=30) if container else 0
            # Skip symbols with no relevance to the query — prevents logic noise
            if text_score == 0 and container_score == 0:
                continue
            score = max(text_score, container_score)
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
                    "container": item.get("container"),
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

        policy_tokens = (
            "policy", "permission", "role", "claim", "guard", "authorize", "auth",
            "middleware", "filter", "tenant", "scope", "isolation", "security",
            "require", "attribute", "handler", "interceptor", "validator",
        )

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
                    "container": item.get("container"),
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
                    "container": item.get("container"),
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

    def find_factories(self, project_root: Path, query: str, include_tests: bool = True, limit: int = 50) -> dict[str, object]:
        """Find factory/setup/create helpers. Includes test paths by default since factories often live there."""
        self.init_db(project_root)
        # Factories often live in test/fixture paths — ensure they're indexed
        if include_tests:
            self.sync_code_files(project_root, include_tests=True)
        needle = query.strip()
        symbol_matches = []
        seen: set[tuple[str, str, int]] = set()
        for term in [needle, "Create", "Factory"]:
            if not term:
                continue
            for item in self.search_symbols(project_root, term, limit=max(limit * 3, 50)):
                key = (str(item.get("path")), str(item.get("symbol")), int(item.get("line_number") or 0))
                if key not in seen:
                    seen.add(key)
                    symbol_matches.append(item)

        with self.connect(project_root) as conn:
            rows = conn.execute(
                """
                SELECT path, symbol, kind, line_number, container, is_partial
                FROM code_outlines
                WHERE kind IN ('method', 'function', 'class', 'record')
                  AND (symbol LIKE 'Create%' OR symbol LIKE '%Factory%')
                ORDER BY path, line_number
                LIMIT ?
                """,
                (max(limit * 4, 50),),
            ).fetchall()
        for row in rows:
            item = {
                "path": row["path"],
                "symbol": row["symbol"],
                "kind": row["kind"],
                "line_number": int(row["line_number"]),
                **({"container": row["container"]} if row["container"] else {}),
                **({"is_partial": True} if row["is_partial"] else {}),
            }
            key = (str(item.get("path")), str(item.get("symbol")), int(item.get("line_number") or 0))
            if key not in seen:
                seen.add(key)
                symbol_matches.append(item)
        factory_matches = []
        for item in symbol_matches:
            symbol = str(item.get("symbol") or "")
            path = str(item.get("path") or "")
            if not symbol:
                continue
            lower_symbol = symbol.lower()
            lower_path = path.lower()
            if lower_symbol.startswith("create") or "factory" in lower_symbol or "factory" in lower_path or "/test" in lower_path or "tests/" in lower_path:
                score = 0
                if needle:
                    lower_needle = needle.lower()
                    if lower_needle in lower_symbol:
                        score += 100
                    if lower_needle in lower_path:
                        score += 60
                if lower_symbol.startswith("create"):
                    score += 20
                if "factory" in lower_symbol:
                    score += 20
                if "factory" in lower_path:
                    score += 10
                if "/test" in lower_path or "tests/" in lower_path:
                    score += 5
                factory_matches.append({**item, "score": score})
        if len(factory_matches) < limit:
            file_matches = []
            for term in [needle, "Create", "Factory"]:
                if not term:
                    continue
                for item in self.search_code(project_root, term, limit=max(limit * 3, 50)):
                    path = str(item.get("path") or "")
                    lower_path = path.lower()
                    summary = str(item.get("summary") or "").lower()
                    if "factory" in lower_path or "factory" in summary or "tests/" in lower_path or "/test" in lower_path or "create" in summary:
                        score = 0
                        if needle:
                            lower_needle = needle.lower()
                            if lower_needle in lower_path:
                                score += 60
                            if lower_needle in summary:
                                score += 40
                        if "factory" in lower_path or "factory" in summary:
                            score += 20
                        if "create" in summary:
                            score += 10
                        file_matches.append(
                            {
                                "path": path,
                                "symbol": None,
                                "kind": "file_match",
                                "line_number": None,
                                "why": ["factory_file_fallback"],
                                "score": score,
                            }
                        )
            for item in file_matches:
                key = (str(item.get("path")), str(item.get("symbol")), int(item.get("line_number") or 0))
                if key not in seen:
                    seen.add(key)
                    factory_matches.append(item)
        factory_matches.sort(key=lambda item: (-int(item.get("score", 0)), str(item.get("path") or ""), str(item.get("symbol") or "")))
        return {
            "query": query,
            "matches": factory_matches[:limit],
        }

    def find_partial_consumers(self, project_root: Path, partial_name: str, limit: int = 50) -> list[dict[str, object]]:
        """Find all files that reference a partial (via partial_ref outline kind)."""
        self.init_db(project_root)
        needle = partial_name.strip().lstrip("_")
        if not needle:
            return []
        with self.connect(project_root) as conn:
            rows = conn.execute(
                """
                SELECT co.path, co.symbol, co.line_number, cf.role
                FROM code_outlines co
                JOIN code_files cf ON cf.path = co.path
                WHERE co.kind = 'partial_ref' AND co.symbol LIKE ?
                ORDER BY co.path, co.line_number
                LIMIT ?
                """,
                (f"%{needle}%", limit),
            ).fetchall()
        return [{"path": r["path"], "symbol": r["symbol"], "line_number": r["line_number"], "role": r["role"]} for r in rows]

    def find_api_consumers(self, project_root: Path, endpoint: str, limit: int = 50) -> list[dict[str, object]]:
        """Find all files that call an API endpoint (via api_call outline kind)."""
        self.init_db(project_root)
        needle = endpoint.strip()
        if not needle:
            return []
        with self.connect(project_root) as conn:
            rows = conn.execute(
                """
                SELECT co.path, co.symbol, co.line_number, cf.role
                FROM code_outlines co
                JOIN code_files cf ON cf.path = co.path
                WHERE co.kind = 'api_call' AND co.symbol LIKE ?
                ORDER BY co.path, co.line_number
                LIMIT ?
                """,
                (f"%{needle}%", limit),
            ).fetchall()
        return [{"path": r["path"], "endpoint": r["symbol"], "line_number": r["line_number"], "role": r["role"]} for r in rows]

    def trace_css_class_usage(self, project_root: Path, class_name: str, limit: int = 50) -> dict[str, object]:
        """Find CSS class definitions AND HTML/Razor template files that likely use this class."""
        self.init_db(project_root)
        needle = class_name.strip()
        if not needle:
            return {"class_name": class_name, "definitions": [], "usages": []}

        with self.connect(project_root) as conn:
            # Definitions: from CSS outlines
            def_rows = conn.execute(
                "SELECT co.path, co.symbol, co.line_number FROM code_outlines co WHERE co.kind = 'css_class' AND co.symbol = ? ORDER BY co.path LIMIT ?",
                (needle, limit),
            ).fetchall()

            # Usages: search actual file content for the class name in template files
            template_rows = conn.execute(
                """
                SELECT path, language, role
                FROM code_files
                WHERE language IN ('razor', 'html', 'jsx', 'tsx', 'vue', 'svelte', 'javascript', 'typescript')
                ORDER BY path
                """,
            ).fetchall()

        usages: list[dict[str, object]] = []
        class_pattern = re.compile(rf'(?:class(?:Name)?=[\"\'][^\"\']*\b{re.escape(needle)}\b|@class\([^)]*\b{re.escape(needle)}\b|\bAddCssClass\([^)]*{re.escape(needle)})')
        for row in template_rows:
            if len(usages) >= limit:
                break
            abs_path = project_root / row["path"]
            if not abs_path.is_file():
                continue
            try:
                text = abs_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            lines_found: list[int] = []
            for line_num, line in enumerate(text.splitlines(), start=1):
                if needle in line:
                    # Match: class attributes, querySelector, string references, or CSS selectors
                    if (class_pattern.search(line)
                        or f'"{needle}"' in line or f"'{needle}'" in line
                        or f".{needle}" in line or f"#{needle}" in line):
                        lines_found.append(line_num)
                        if len(lines_found) >= 3:
                            break
            if lines_found:
                usages.append({
                    "path": row["path"],
                    "role": row["role"],
                    "language": row["language"],
                    "lines": lines_found,
                    "count": sum(1 for line in text.splitlines() if needle in line),
                })

        # Sort by usage count descending
        usages.sort(key=lambda u: -u.get("count", 0))

        return {
            "class_name": class_name,
            "definitions": [{"path": r["path"], "symbol": r["symbol"], "line_number": r["line_number"]} for r in def_rows],
            "usages": usages[:limit],
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
                    "kind": item.get("kind") or item.get("field_kind", ""),
                    "line_number": item["line_number"],
                }
            )

        cluster.sort(key=lambda item: (self._layer_rank(str(item["layer"])), str(item["path"]), str(item["symbol"] or "")))
        return {
            "concept": concept,
            "cluster": cluster[:limit],
        }

    def find_duplicate_structures(
        self,
        project_root: Path,
        role_filter: str | None = None,
        kind_filter: str | None = None,
        min_shared: int = 3,
        limit: int = 30,
    ) -> dict[str, object]:
        """Find files with overlapping outline symbols — candidates for extraction into shared partials/components.

        Groups files by shared symbol fingerprints (same symbol name + kind appearing in multiple files).
        Returns clusters of files that share enough structure to warrant extraction.

        Args:
            role_filter: Only consider files with this role (e.g., "page-view", "partial-view").
            kind_filter: Only consider outline symbols of this kind (e.g., "translation_key", "partial_ref", "js_function").
            min_shared: Minimum number of files sharing a symbol to be considered duplicate (default 3).
            limit: Maximum number of clusters to return.
        """
        self.init_db(project_root)

        with self.connect(project_root) as conn:
            # Step 1: Find symbols that appear in multiple files
            kind_clause = ""
            params: list[object] = [min_shared]
            if kind_filter:
                kind_clause = "AND co.kind = ?"
                params.insert(0, kind_filter)

            role_clause = ""
            if role_filter:
                role_clause = "AND cf.role = ?"
                params.insert(0, role_filter)

            shared_symbols = conn.execute(
                f"""
                SELECT co.symbol, co.kind, COUNT(DISTINCT co.path) AS file_count,
                       GROUP_CONCAT(DISTINCT co.path) AS files
                FROM code_outlines co
                JOIN code_files cf ON cf.path = co.path
                WHERE 1=1 {role_clause} {kind_clause}
                GROUP BY co.symbol, co.kind
                HAVING COUNT(DISTINCT co.path) >= ?
                ORDER BY file_count DESC
                LIMIT 200
                """,
                params,
            ).fetchall()

            if not shared_symbols:
                return {"clusters": [], "summary": "No duplicate structures found with the given filters."}

            # Step 2: Build file → shared-symbols map for clustering
            file_symbols: dict[str, list[dict[str, object]]] = {}
            for row in shared_symbols:
                files = str(row["files"]).split(",")
                for f in files:
                    f = f.strip()
                    if f not in file_symbols:
                        file_symbols[f] = []
                    file_symbols[f].append({
                        "symbol": row["symbol"],
                        "kind": row["kind"],
                        "shared_with": int(row["file_count"]),
                    })

            # Step 3: Find file pairs/groups with high overlap
            file_list = list(file_symbols.keys())
            pair_scores: dict[tuple[str, str], list[str]] = {}
            for row in shared_symbols:
                files = [f.strip() for f in str(row["files"]).split(",")]
                for i in range(len(files)):
                    for j in range(i + 1, len(files)):
                        pair = (files[i], files[j]) if files[i] < files[j] else (files[j], files[i])
                        if pair not in pair_scores:
                            pair_scores[pair] = []
                        pair_scores[pair].append(f"{row['kind']}:{row['symbol']}")

            # Step 4: Sort pairs by overlap count, build clusters
            sorted_pairs = sorted(pair_scores.items(), key=lambda x: len(x[1]), reverse=True)

            clusters: list[dict[str, object]] = []
            for (file_a, file_b), shared in sorted_pairs[:limit]:
                # Get roles for context
                role_a = conn.execute("SELECT role FROM code_files WHERE path = ?", (file_a,)).fetchone()
                role_b = conn.execute("SELECT role FROM code_files WHERE path = ?", (file_b,)).fetchone()

                # Categorize shared symbols by kind
                by_kind: dict[str, list[str]] = {}
                for s in shared:
                    kind, sym = s.split(":", 1)
                    if kind not in by_kind:
                        by_kind[kind] = []
                    by_kind[kind].append(sym)

                clusters.append({
                    "files": [file_a, file_b],
                    "roles": [role_a["role"] if role_a else None, role_b["role"] if role_b else None],
                    "shared_count": len(shared),
                    "shared_by_kind": {k: v for k, v in sorted(by_kind.items(), key=lambda x: len(x[1]), reverse=True)},
                })

            # Step 5: Also report the most duplicated individual symbols
            top_symbols = [
                {
                    "symbol": row["symbol"],
                    "kind": row["kind"],
                    "file_count": int(row["file_count"]),
                    "files": [f.strip() for f in str(row["files"]).split(",")],
                }
                for row in shared_symbols[:20]
            ]

        return {
            "clusters": clusters,
            "top_shared_symbols": top_symbols,
            "summary": f"Found {len(clusters)} file pairs with shared structures, {len(shared_symbols)} symbols appearing in {min_shared}+ files.",
        }

    def find_transition_points(self, project_root: Path, concept: str | None = None, limit: int = 50) -> dict[str, object]:
        self.init_db(project_root)
        needle = (concept or "").strip()
        dot_parts = [part.strip() for part in needle.split(".") if part.strip()] if needle else []
        compound_terms = dot_parts if len(dot_parts) >= 2 else []
        seed_query = dot_parts[0] if compound_terms else (needle or "legacy")
        if needle:
            self._ensure_parsed_candidates(project_root, needle, limit=limit * 4)

        code_matches = self.search_code(project_root, seed_query, limit=max(limit * 3, 100))
        symbol_matches = self.search_symbols(project_root, seed_query, limit=max(limit * 3, 100))

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
            if compound_terms:
                term_hits = 0
                for term in compound_terms:
                    lower_term = term.lower()
                    if lower_term in lower_symbol or lower_term in lower_path or lower_term in str(item.get("container") or "").lower():
                        term_hits += 1
                if term_hits:
                    score += term_hits * 35
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
                    "container": item.get("container"),
                    "snippet": snippet["snippet"] if snippet else None,
                }
            )

        broad_single = bool(needle and len([p for p in re.split(r"\s+", needle) if p.strip()]) == 1)
        for item in code_matches:
            path = str(item["path"])
            lower_path = path.lower()
            lower_summary = str(item["summary"] or "").lower()
            score = 0
            if needle:
                score += self._score_text_match(needle, path, exact=50, prefix=30, contains=20)
            if compound_terms:
                term_hits = 0
                for term in compound_terms:
                    lower_term = term.lower()
                    if lower_term in lower_path or lower_term in lower_summary:
                        term_hits += 1
                if term_hits:
                    score += term_hits * 25
            for token in transition_tokens:
                if token in lower_path:
                    score += 30
                if token in lower_summary:
                    score += 15
            if score <= 0:
                continue
            if broad_single and score < 45:
                continue
            if compound_terms and not any(term.lower() in lower_path or term.lower() in lower_summary for term in compound_terms):
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
                **({"container": row["container"]} if row["container"] else {}),
                **({"is_partial": True} if row["is_partial"] else {}),
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
                **({"container": row["container"]} if row["container"] else {}),
                **({"is_partial": True} if row["is_partial"] else {}),
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
            WHERE kind IN ('class', 'record', 'struct', 'interface', 'type_alias', 'enum', 'property', 'field', 'enum_member')
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
                **({"container": row["container"]} if row["container"] else {}),
                **({"is_partial": True} if row["is_partial"] else {}),
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
        # Use smaller per-category limits for a concise summary
        cat_limit = min(limit, 8)

        domain_cluster = self.find_domain_clusters(project_root, concept=concept, limit=cat_limit)
        touchpoints = self.find_ui_backend_touchpoints(project_root, concept=concept, limit=cat_limit)
        policy = self.find_policy_surfaces(project_root, concept=concept, limit=cat_limit)
        transitions = self.find_transition_points(project_root, concept=concept, limit=cat_limit)
        data_structures = self.find_data_structures(project_root, query=concept, limit=cat_limit)
        entrypoints = self.find_entrypoints(project_root, concept=concept, limit=cat_limit)

        # Strip verbose snippets and low-relevance results
        min_score = 40
        def slim(matches: list[dict[str, object]], require_score: bool = True) -> list[dict[str, object]]:
            return [
                {k: v for k, v in m.items() if k != "snippet"}
                for m in (matches or [])
                if not require_score or int(m.get("score", 0)) >= min_score
            ]

        return {
            "concept": concept,
            "domain_cluster": slim(domain_cluster.get("cluster", []), require_score=False),
            "touchpoints": slim(touchpoints.get("matches", [])),
            "policy_surfaces": slim(policy.get("matches", [])),
            "transition_points": slim(transitions.get("matches", [])),
            "data_structures": slim(data_structures if isinstance(data_structures, list) else data_structures.get("result", []), require_score=False),
            "entrypoints": slim(entrypoints.get("matches", [])),
        }

    def investigate(
        self,
        project_root: Path,
        concept: str,
        limit: int = 5,
        depth: str = "standard",
        focus: str = "general",
    ) -> dict[str, object]:
        """High-level investigation entry point. Returns a navigation guide, not full data.

        Runs quick probes across symbols, code files, schema, CSS, and modules,
        then returns a ranked summary of what was found and which tools to call next.
        """
        self.init_db(project_root)
        needle = concept.strip()
        if not needle:
            return {"concept": concept, "findings": [], "next_tools": []}

        depth_value = depth.strip().lower()
        focus_value = focus.strip().lower()
        if depth_value not in {"shallow", "standard", "deep"}:
            depth_value = "standard"
        if focus_value not in {"general", "workflow", "service", "schema", "ui", "backend"}:
            focus_value = "general"

        symbol_limit = limit if depth_value == "shallow" else (limit * 2 if depth_value == "deep" else limit)
        code_limit = limit if depth_value == "shallow" else (limit * 2 if depth_value == "deep" else limit)

        findings: list[dict[str, object]] = []
        next_tools: list[dict[str, str]] = []

        # 1. Symbol search — are there named symbols?
        symbols = self.search_symbols(project_root, needle, limit=symbol_limit)
        if symbols:
            top_kinds = list(dict.fromkeys(s["kind"] for s in symbols))[:3]
            preview_symbols: list[dict[str, object]] = []
            preview_symbols.extend(symbols[:3])
            for preferred_kind in ("method", "enum", "record", "class"):
                extra = next((item for item in symbols if item["kind"] == preferred_kind and item not in preview_symbols), None)
                if extra is not None:
                    preview_symbols.append(extra)
            top = []
            for s in preview_symbols[:6]:
                item = {"symbol": s["symbol"], "kind": s["kind"], "path": s["path"]}
                if s.get("namespace"):
                    item["namespace"] = s["namespace"]
                if s["kind"] == "method":
                    signature = self._extract_method_signature(project_root, str(s["path"]), int(s["line_number"]))
                    if signature.get("signature"):
                        item["signature"] = signature["signature"]
                elif s["kind"] == "enum":
                    item["enum_values"] = self._enum_members_for_container(project_root, str(s["path"]), str(s["symbol"]))[:8]
                top.append(item)
            findings.append({
                "area": "symbols",
                "source": "outline_index",
                "count": len(symbols),
                "top": top,
                "kinds_found": top_kinds,
            })
            next_tools.append({"tool": "code_search_symbols", "why": f"Found {len(symbols)} symbols — search for specific names/kinds"})
            if any(s["kind"] in ("class", "interface", "struct", "record") for s in symbols):
                next_tools.append({"tool": "code_find_references", "why": "Trace where these types are used"})
            if any(s["kind"] == "method" for s in symbols):
                next_tools.append({"tool": "code_get_method_signature", "why": "Read exact method params/returns before calling methods"})
            if any(str(s["symbol"]).endswith("Service") and s["kind"] == "class" for s in symbols):
                next_tools.append({"tool": "code_get_service_api", "why": "Read all public method signatures for a service before writing workflow-heavy code or tests"})
            if any(s["kind"] == "enum" for s in symbols):
                next_tools.append({"tool": "code_get_enum_values", "why": "Read exact enum members before using enum values"})

            service_candidates = [s for s in symbols if s["kind"] == "class" and str(s["symbol"]).endswith("Service")]
            if service_candidates:
                findings.append(
                    {
                        "area": "service_api_candidates",
                        "source": "outline_index",
                        "count": len(service_candidates),
                        "top": [
                            {
                                "service": item["symbol"],
                                "path": item["path"],
                                **({"namespace": item["namespace"]} if item.get("namespace") else {}),
                            }
                            for item in service_candidates[:4]
                        ],
                    }
                )

        # 2. Code files — which files mention this concept?
        code_files = self.search_code(project_root, needle, limit=code_limit)
        if code_files:
            roles = list(dict.fromkeys(f["role"] for f in code_files))[:4]
            findings.append({
                "area": "files",
                "source": "file_index",
                "count": len(code_files),
                "top": [
                    {
                        "path": f["path"],
                        "role": f["role"],
                        "language": f.get("language"),
                        "language_tier": f.get("language_tier"),
                        "language_source": f.get("language_source"),
                    }
                    for f in code_files[:3]
                ],
                "roles_found": roles,
            })
            next_tools.append({"tool": "code_get_outline", "why": "Understand structure of the top files"})

        # 3. Schema — any entities/fields?
        try:
            from .schema_index_store import SchemaIndexStore
            schema = SchemaIndexStore()
            entities = schema.find_schema_entities(project_root, query=needle, limit=limit if depth_value != "deep" else limit * 2)
            fields = schema.find_schema_field(project_root, needle, limit=limit if depth_value != "deep" else limit * 2)
            if entities:
                findings.append({
                    "area": "schema_entities",
                    "source": "schema_index",
                    "count": len(entities),
                    "top": [{"entity": e["entity_name"], "source": e.get("source_path", "").split("/")[-1]} for e in entities[:3]],
                })
                next_tools.append({"tool": "schema_get_entity", "why": "Get fields and relationships for matched entities"})
                next_tools.append({"tool": "code_get_entity_properties", "why": "Read a lightweight property list for matched entities or DTOs"})
            if fields:
                findings.append({
                    "area": "schema_fields",
                    "source": "schema_index",
                    "count": len(fields),
                    "top": [{"field": f["field_name"], "entity": f["entity_name"]} for f in fields[:3]],
                })
                next_tools.append({"tool": "schema_trace_relationship_path", "why": "Trace FK paths between entities"})
        except Exception:
            pass

        # 4. CSS — any style definitions?
        with self.connect(project_root) as conn:
            css_rows = conn.execute(
                "SELECT symbol, path FROM code_outlines WHERE kind = 'css_class' AND symbol LIKE ? LIMIT ?",
                (f"%{needle}%", limit),
            ).fetchall()
        if css_rows and focus_value in {"general", "ui"}:
            findings.append({
                "area": "css",
                "source": "outline_index",
                "count": len(css_rows),
                "top": [{"class": r["symbol"], "path": r["path"]} for r in css_rows[:3]],
            })
            next_tools.append({"tool": "code_trace_css_class", "why": "Find CSS definitions + HTML/Razor template usages"})

        # 5. Modules — which module owns this?
        modules = self.get_modules(project_root)
        matching_modules = [m for m in modules if needle.lower() in m["name"].lower() or needle.lower() in (m.get("description") or "").lower()]
        if matching_modules:
            findings.append({
                "area": "modules",
                "source": "module_index",
                "count": len(matching_modules),
                "top": [{"module": m["module_path"], "kind": m["kind"], "files": m["file_count"]} for m in matching_modules[:3]],
            })
            next_tools.append({"tool": "code_get_module_files", "why": "List files in the matching module"})

        multi_word = len([part for part in re.split(r"\s+", needle) if part.strip()]) >= 2
        if multi_word or focus_value in {"workflow", "backend"} or depth_value == "deep":
            try:
                touchpoints = self.find_ui_backend_touchpoints(project_root, concept=concept, limit=limit)
                tp_matches = touchpoints.get("matches", []) if isinstance(touchpoints, dict) else []
                if tp_matches:
                    if focus_value == "backend":
                        tp_matches = [item for item in tp_matches if item.get("layer") in {"api", "logic", "data"}]
                    elif focus_value == "ui":
                        tp_matches = [item for item in tp_matches if item.get("layer") == "ui"]
                    findings.append(
                        {
                            "area": "workflow_touchpoints",
                            "source": "outline_index",
                            "count": len(tp_matches),
                            "top": [
                                {
                                    "path": item["path"],
                                    "layer": item.get("layer"),
                                    "symbol": item.get("symbol"),
                                    "kind": item.get("kind"),
                                }
                                for item in tp_matches[:4]
                            ],
                        }
                    )
                    next_tools.append({"tool": "code_trace_api_to_ui", "why": "Trace the workflow across UI, logic, API, and backend ownership points"})
            except Exception:
                pass
            try:
                routes = self.find_routes(project_root, query=concept, limit=limit)
                route_matches = routes.get("matches", []) if isinstance(routes, dict) else []
                if route_matches and focus_value in {"general", "workflow", "backend"}:
                    findings.append(
                        {
                            "area": "routes",
                            "source": "outline_index",
                            "count": len(route_matches),
                            "top": [{"path": item["path"], "layer": item.get("layer")} for item in route_matches[:3]],
                        }
                    )
            except Exception:
                pass
            try:
                policy = self.find_policy_surfaces(project_root, concept=concept, limit=limit)
                policy_matches = policy.get("matches", []) if isinstance(policy, dict) else []
                if policy_matches and focus_value in {"general", "workflow", "backend", "service"}:
                    findings.append(
                        {
                            "area": "policy_surfaces",
                            "source": "outline_index",
                            "count": len(policy_matches),
                            "top": [{"path": item["path"], "layer": item.get("layer"), "symbol": item.get("symbol")} for item in policy_matches[:3]],
                        }
                    )
            except Exception:
                pass

        # Deduplicate next_tools by tool name
        seen_tools: set[str] = set()
        unique_tools: list[dict[str, str]] = []
        for t in next_tools:
            if t["tool"] not in seen_tools:
                seen_tools.add(t["tool"])
                unique_tools.append(t)

        return {
            "concept": concept,
            "depth": depth_value,
            "focus": focus_value,
            "findings": findings,
            "next_tools": unique_tools,
            "summary": ("Found: " + ", ".join(str(f["area"]) + "(" + str(f["count"]) + ")" for f in findings)) if findings else "No matches found.",
        }

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
        base = cls._MODULE_HINT_DIRS_BASE | INDEX_EXTRA_MODULE_HINTS
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
        lines = [line.strip() for line in text.splitlines() if line.strip()][:max_lines]
        if not lines:
            return file_name
        return " | ".join(lines)[:400]

    def _extract_outline(self, project_root: Path, text: str, code_language: str) -> list[tuple[str, str, int, str | None, bool]]:
        outlines: list[tuple[str, str, int, str | None, bool]] = []
        patterns: list[tuple[str, str]] = []
        line_patterns = line_patterns_for_language(project_root, code_language)
        extractor_family = extractor_family_for_language(project_root, code_language)
        if extractor_family == "python_ast":
            outlines.extend(extract_python_outline(text))
        elif extractor_family in {"javascript_ast", "typescript_ast", "jsx_ast", "tsx_ast"}:
            ast_outline = self.frontend_ast.extract_outline(text, code_language)
            if ast_outline is not None:
                outlines.extend(ast_outline)
                for line_number, line in enumerate(text.splitlines(), start=1):
                    initializer = self._extract_js_initializer(line)
                    if initializer is not None:
                        outlines.append((initializer, "initializer", line_number, None, False))
            else:
                patterns = [
                    (r"^\s*(?:export\s+)?class\s+([A-Za-z_][A-Za-z0-9_]*)", "class"),
                    (r"^\s*(?:export\s+)?interface\s+([A-Za-z_][A-Za-z0-9_]*)", "interface"),
                    (r"^\s*(?:export\s+)?type\s+([A-Za-z_][A-Za-z0-9_]*)\s*=", "type_alias"),
                    (r"^\s*(?:export\s+)?enum\s+([A-Za-z_][A-Za-z0-9_]*)", "enum"),
                    (r"^\s*(?:export\s+)?(?:default\s+)?function\s+([A-Za-z_][A-Za-z0-9_]*)", "function"),
                    (r"^\s*function\s+([A-Za-z_][A-Za-z0-9_]*)", "function"),
                    (r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:async\s*)?\(", "function"),
                    (r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:async\s*)?[A-Za-z_][A-Za-z0-9_]*\s*=>", "function"),
                    (r"^\s*window\.([A-Za-z_][A-Za-z0-9_]*)\s*=", "namespace"),
                    (r"^\s*([A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:async\s+)?function", "method"),
                    (r"""fetch\(\s*['"`](/api/[^'"`]+)['"`]""", "api_call"),
                ]
        elif extractor_family == "csharp_rich":
            outlines.extend(extract_csharp_outline(text))
        elif extractor_family == "resx_rich":
            outlines.extend(extract_resx_outline(text))
        elif extractor_family == "css_rich":
            outlines.extend(extract_css_outline(text))
        else:
            patterns = outline_patterns_for_language(project_root, code_language)
            if not patterns:
                family = outline_family_for_language(project_root, code_language)
                if family:
                    patterns = outline_family_patterns(family)
            if not patterns:
                patterns = generic_outline_patterns(code_language)

        for symbol, kind, line_number, container, is_partial in extract_generic_outline(text, patterns):
            js_kind = kind
            if code_language in {"javascript", "typescript", "jsx", "tsx"}:
                if symbol.startswith("use") and len(symbol) > 3 and symbol[3:4].isupper():
                    js_kind = "hook"
                elif symbol[:1].isupper() and symbol.endswith("Provider"):
                    js_kind = "context_provider"
                elif symbol[:1].isupper():
                    js_kind = "component"
            outlines.append((symbol, js_kind, line_number, container, is_partial))
        for symbol, kind, line_number, container, is_partial in extract_line_patterns(text, line_patterns):
            outlines.append((symbol, kind, line_number, container, is_partial))
        if code_language in {"javascript", "typescript", "jsx", "tsx"}:
            for line_number, line in enumerate(text.splitlines(), start=1):
                initializer = self._extract_js_initializer(line)
                if initializer is not None:
                    outlines.append((initializer, "initializer", line_number, None, False))
        return list(dict.fromkeys(outlines))

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

        # Attribute patterns
        http_attr_pattern = re.compile(r'\[(Http(?:Get|Post|Put|Delete|Patch))(?:\(\s*"([^"]*)"\s*\))?\]')
        route_attr_pattern = re.compile(r'\[Route\(\s*"([^"]*)"\s*\)\]')
        authorize_attr_pattern = re.compile(r'\[Authorize(?:\(\s*(?:Roles\s*=\s*"([^"]*)")?(?:Policy\s*=\s*"([^"]*)")?\s*\))?\]')
        allow_anon_pattern = re.compile(r"\[AllowAnonymous\]")
        validation_attr_pattern = re.compile(r"\[(Required|MaxLength|MinLength|StringLength|Range|RegularExpression|EmailAddress|Phone|Url|Compare|CreditCard)(?:\(\s*([^)]*)\s*\))?\]")
        hub_method_pattern = re.compile(r"^\s*(?:public|private|protected)\s+(?:async\s+)?(?:Task|Task<[^>]+>|void)\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")

        current_type: str | None = None
        current_kind: str | None = None
        brace_depth = 0
        type_depth: int | None = None
        inside_enum = False
        pending_attrs: list[tuple[str, str, int]] = []  # (symbol, kind, line)
        is_hub_class = False

        for line_number, line in enumerate(text.splitlines(), start=1):
            opens = line.count("{")
            closes = line.count("}")

            ns_match = namespace_pattern.match(line)
            if ns_match:
                namespace_name = ns_match.group(1)

            # Collect attributes before the method/class they decorate
            for m in http_attr_pattern.finditer(line):
                verb = m.group(1)  # HttpGet, HttpPost, etc.
                route = m.group(2) or ""
                endpoint = f"{verb}:{route}" if route else verb
                pending_attrs.append((endpoint, "http_endpoint", line_number))

            m = route_attr_pattern.search(line)
            if m:
                pending_attrs.append((m.group(1), "route", line_number))

            m = authorize_attr_pattern.search(line)
            if m:
                role = m.group(1)
                policy = m.group(2)
                auth_detail = role or policy or "default"
                pending_attrs.append((auth_detail, "authorize", line_number))

            if allow_anon_pattern.search(line):
                pending_attrs.append(("AllowAnonymous", "authorize", line_number))

            for m in validation_attr_pattern.finditer(line):
                attr_name = m.group(1)
                attr_args = m.group(2) or ""
                val_symbol = f"{attr_name}({attr_args})" if attr_args else attr_name
                pending_attrs.append((val_symbol, "validation", line_number))

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
                is_hub_class = ": Hub" in line or ":Hub" in line

                # Attach pending attributes (route, authorize) to the type
                for attr_sym, attr_kind, attr_line in pending_attrs:
                    outlines.append((attr_sym, attr_kind, attr_line, symbol, False))
                pending_attrs.clear()

            method_match = method_pattern.match(line)
            if method_match and current_type is not None:
                symbol = method_match.group(1)
                method_kind = "method"
                if is_hub_class and symbol not in {"OnConnectedAsync", "OnDisconnectedAsync"}:
                    method_kind = "hub_method"
                outlines.append((symbol, method_kind, line_number, current_type, False))
                # Attach pending attributes (http_endpoint, authorize, validation) to the method
                for attr_sym, attr_kind, attr_line in pending_attrs:
                    outlines.append((attr_sym, attr_kind, attr_line, current_type, False))
                pending_attrs.clear()

            property_match = property_pattern.match(line)
            if property_match and current_type is not None and current_kind != "enum":
                symbol = property_match.group(1)
                outlines.append((symbol, "property", line_number, current_type, False))
                # Attach validation attributes to the property
                for attr_sym, attr_kind, attr_line in pending_attrs:
                    if attr_kind == "validation":
                        outlines.append((f"{symbol}:{attr_sym}", "validation", attr_line, current_type, False))
                pending_attrs = [(s, k, l) for s, k, l in pending_attrs if k != "validation"]

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

            # Clear stale pending attrs if we hit a blank line or brace-only line
            stripped = line.strip()
            if not stripped or stripped in {"{", "}"}:
                pending_attrs.clear()

            brace_depth += opens
            brace_depth -= closes
            if type_depth is not None and brace_depth < type_depth - 1:
                current_type = None
                current_kind = None
                type_depth = None
                inside_enum = False
                is_hub_class = False
                pending_attrs.clear()

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

    def _extract_razor_outline(self, text: str) -> list[tuple[str, str, int, str | None, bool]]:
        """Extract symbols from Razor .cshtml files: sections, partials, model, forms, components."""
        outlines: list[tuple[str, str, int, str | None, bool]] = []

        # Regex patterns for razor constructs
        model_pattern = re.compile(r"^\s*@model\s+([A-Za-z_][A-Za-z0-9_\.]*)")
        section_pattern = re.compile(r"@section\s+([A-Za-z_][A-Za-z0-9_]*)")
        partial_tag = re.compile(r'<partial\s+name="([^"]+)"', re.IGNORECASE)
        partial_async = re.compile(r'Html\.PartialAsync\(\s*"([^"]+)"')
        inject_pattern = re.compile(r"^\s*@inject\s+([A-Za-z_][A-Za-z0-9_<>,\.\s]*?)\s+([A-Za-z_][A-Za-z0-9_]*)\s*$")
        lang_t_pattern = re.compile(r'Lang\.T\(\s*"([^"]+)"\s*\)')
        form_tag = re.compile(r"<form[^>]*(?:asp-page-handler|asp-action|asp-page)\s*=\s*\"([^\"]+)\"", re.IGNORECASE)
        component_pattern = re.compile(r"@(?:await\s+)?Component\.InvokeAsync\(\s*\"([^\"]+)\"")
        render_section = re.compile(r"@RenderSection\(\s*\"([^\"]+)\"")
        layout_pattern = re.compile(r'^\s*Layout\s*=\s*"([^"]+)"')
        page_directive = re.compile(r'^\s*@page(?:\s+"?([^"\s]*)"?)?')
        functions_block = re.compile(r"^\s*@functions\s*\{|^\s*@code\s*\{")

        # asp-for bindings (view↔model contract)
        asp_for_pattern = re.compile(r'asp-for="([^"]+)"')
        # data-* attributes (HTML↔JS contract)
        data_attr_pattern = re.compile(r'\bdata-([a-z][a-z0-9-]*)')
        # Permission/auth checks in views
        perm_check_patterns = [
            re.compile(r"@if\s*\(\s*Model\.([A-Za-z_]*(?:Can|Has|Is|Allow|Enable|Show|Permission)[A-Za-z_]*)"),
            re.compile(r"@if\s*\(\s*(?:User|Context\.User)\.IsInRole\(\s*\"([^\"]+)\""),
            re.compile(r"@if\s*\(\s*ViewData\[\"([A-Za-z_]*(?:Can|Has|Is)[A-Za-z_]*)\""),
        ]

        seen_translations: set[str] = set()
        seen_asp_for: set[str] = set()
        seen_data_attrs: set[str] = set()

        for line_number, line in enumerate(text.splitlines(), start=1):
            # @model directive
            m = model_pattern.match(line)
            if m:
                outlines.append((m.group(1), "model_binding", line_number, None, False))
                continue

            # @page directive (route)
            m = page_directive.match(line)
            if m:
                route = (m.group(1) or "").strip() or "@page"
                outlines.append((route, "page_route", line_number, None, False))
                continue

            # Layout assignment
            m = layout_pattern.search(line)
            if m:
                outlines.append((m.group(1), "layout_ref", line_number, None, False))
                continue

            # @inject directives
            m = inject_pattern.match(line)
            if m:
                outlines.append((m.group(2), "inject", line_number, m.group(1).strip(), False))
                continue

            # @section definitions
            for m in section_pattern.finditer(line):
                outlines.append((m.group(1), "section", line_number, None, False))

            # <partial> tag helper references
            for m in partial_tag.finditer(line):
                outlines.append((m.group(1), "partial_ref", line_number, None, False))

            # Html.PartialAsync references
            for m in partial_async.finditer(line):
                outlines.append((m.group(1), "partial_ref", line_number, None, False))

            # @await Component.InvokeAsync
            for m in component_pattern.finditer(line):
                outlines.append((m.group(1), "component_ref", line_number, None, False))

            # @RenderSection
            for m in render_section.finditer(line):
                outlines.append((m.group(1), "render_section", line_number, None, False))

            # Form handlers (asp-page-handler, asp-action, asp-page)
            for m in form_tag.finditer(line):
                outlines.append((m.group(1), "form_handler", line_number, None, False))

            # @functions/@code blocks
            if functions_block.match(line):
                outlines.append(("@functions", "code_block", line_number, None, False))

            # Translation keys — Lang.T("...")
            for m in lang_t_pattern.finditer(line):
                key = m.group(1)
                if key not in seen_translations:
                    seen_translations.add(key)
                    outlines.append((key, "translation_key", line_number, None, False))

            # Inline JS: function declarations inside <script> blocks
            func_match = re.match(r"\s*(?:async\s+)?function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", line)
            if func_match:
                outlines.append((func_match.group(1), "js_function", line_number, None, False))

            # Inline JS: fetch/AJAX endpoint calls
            for m in re.finditer(r"""fetch\(\s*[`'"](/api/[^`'"]+)[`'"]""", line):
                outlines.append((m.group(1), "api_call", line_number, None, False))

            # Inline JS: $.ajax, $.get, $.post URL patterns
            for m in re.finditer(r"""\$\.(?:ajax|get|post|getJSON)\(\s*[`'"](/api/[^`'"]+)[`'"]""", line):
                outlines.append((m.group(1), "api_call", line_number, None, False))

            # asp-for bindings (view↔model property contract)
            for m in asp_for_pattern.finditer(line):
                binding = m.group(1)
                if binding not in seen_asp_for:
                    seen_asp_for.add(binding)
                    outlines.append((binding, "asp_for_binding", line_number, None, False))

            # data-* attributes (HTML↔JS contract)
            for m in data_attr_pattern.finditer(line):
                attr = m.group(1)
                if attr not in seen_data_attrs and attr not in {"toggle", "bs-toggle", "bs-target", "bs-dismiss"}:
                    seen_data_attrs.add(attr)
                    outlines.append((f"data-{attr}", "data_attribute", line_number, None, False))

            # Permission/auth checks
            for perm_re in perm_check_patterns:
                for m in perm_re.finditer(line):
                    outlines.append((m.group(1), "permission_check", line_number, None, False))

        return outlines

    def _extract_resx_outline(self, text: str) -> list[tuple[str, str, int, str | None, bool]]:
        """Extract translation key-value pairs from .resx XML files."""
        outlines: list[tuple[str, str, int, str | None, bool]] = []
        # Match <data name="Key" ...> <value>Value</value> </data>
        data_pattern = re.compile(r'<data\s+name="([^"]+)"')
        value_pattern = re.compile(r"<value>(.*?)</value>", re.DOTALL)

        in_data = False
        current_name: str | None = None
        current_line = 1

        for line_number, line in enumerate(text.splitlines(), start=1):
            m = data_pattern.search(line)
            if m:
                current_name = m.group(1)
                current_line = line_number
                in_data = True
            if in_data and current_name is not None:
                vm = value_pattern.search(line)
                if vm:
                    value = vm.group(1).strip()
                    container = value[:80] if value else None
                    outlines.append((current_name, "translation", current_line, container, False))
                    in_data = False
                    current_name = None
                elif "</data>" in line:
                    # Data block closed without a value — reset state
                    in_data = False
                    current_name = None

        return outlines

    def _extract_css_outline(self, text: str) -> list[tuple[str, str, int, str | None, bool]]:
        """Extract symbols from CSS files: custom classes, CSS variables, @theme vars, @keyframes, @layer."""
        outlines: list[tuple[str, str, int, str | None, bool]] = []

        # Patterns for CSS constructs
        # Match ALL class names in a selector line, not just the first
        class_pattern = re.compile(r"\.([a-zA-Z_][a-zA-Z0-9_-]*)")
        var_pattern = re.compile(r"--([a-zA-Z][a-zA-Z0-9_-]*)\s*:")
        keyframes_pattern = re.compile(r"@keyframes\s+([a-zA-Z_][a-zA-Z0-9_-]*)")
        layer_pattern = re.compile(r"@layer\s+([a-zA-Z_][a-zA-Z0-9_-]*)")
        theme_pattern = re.compile(r"@theme\s*\{")
        variant_pattern = re.compile(r"@variant\s+([a-zA-Z_][a-zA-Z0-9_-]*)")

        in_theme = False
        brace_depth = 0
        theme_depth: int | None = None

        for line_number, line in enumerate(text.splitlines(), start=1):
            opens = line.count("{")
            closes = line.count("}")

            # @theme block
            if theme_pattern.search(line):
                in_theme = True
                theme_depth = brace_depth + opens
                outlines.append(("@theme", "theme_block", line_number, None, False))

            # CSS variables (--color-primary, etc.)
            for m in var_pattern.finditer(line):
                var_name = m.group(1)
                context = "theme" if in_theme else None
                outlines.append((f"--{var_name}", "css_variable", line_number, context, False))

            # Custom classes — extract all class names from selector lines
            # Only match lines that look like selectors (contain { or , or start with .)
            stripped = line.strip()
            if stripped and not stripped.startswith("/*") and not stripped.startswith("*") and not stripped.startswith("//"):
                if "{" in stripped or "," in stripped or stripped.startswith(".") or stripped.startswith("&"):
                    seen_classes: set[str] = set()
                    for m in class_pattern.finditer(stripped.split("{")[0]):
                        cls_name = m.group(1)
                        if cls_name not in seen_classes:
                            seen_classes.add(cls_name)
                            outlines.append((cls_name, "css_class", line_number, None, False))

            # @keyframes
            m = keyframes_pattern.search(line)
            if m:
                outlines.append((m.group(1), "keyframes", line_number, None, False))

            # @layer
            m = layer_pattern.search(line)
            if m:
                outlines.append((m.group(1), "css_layer", line_number, None, False))

            # @variant
            m = variant_pattern.search(line)
            if m:
                outlines.append((m.group(1), "css_variant", line_number, None, False))

            brace_depth += opens
            brace_depth -= closes
            if in_theme and theme_depth is not None and brace_depth < theme_depth:
                in_theme = False
                theme_depth = None

        return outlines

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
            elif language == "razor":
                # @using directives
                m = re.match(r"^@using\s+([A-Za-z_][A-Za-z0-9_\.]*)", stripped)
                if m:
                    edges.append((m.group(1), "using"))
                # @model binding → edge to the model type
                m = re.match(r"^@model\s+([A-Za-z_][A-Za-z0-9_\.]*)", stripped)
                if m:
                    edges.append((m.group(1), "model_binding"))
                # @inject → edge to the injected service type
                m = re.match(r"^@inject\s+([A-Za-z_][A-Za-z0-9_<>,\.\s]*?)\s+[A-Za-z_][A-Za-z0-9_]*\s*$", stripped)
                if m:
                    edges.append((m.group(1).strip(), "inject"))
                # <partial name="..."> references
                for pm in re.finditer(r'<partial\s+name="([^"]+)"', stripped, re.IGNORECASE):
                    edges.append((pm.group(1), "partial_ref"))
                # Html.PartialAsync("...")
                for pm in re.finditer(r'Html\.PartialAsync\(\s*"([^"]+)"', stripped):
                    edges.append((pm.group(1), "partial_ref"))
                # Component.InvokeAsync("...")
                for pm in re.finditer(r'Component\.InvokeAsync\(\s*"([^"]+)"', stripped):
                    edges.append((pm.group(1), "component_ref"))
                # Layout reference
                m = re.search(r'Layout\s*=\s*"([^"]+)"', stripped)
                if m:
                    edges.append((m.group(1), "layout_ref"))
                # JS function calls: onclick="funcName(..." or funcName( in script blocks
                for fm in re.finditer(r'onclick="([A-Za-z_][A-Za-z0-9_.]*)\s*\(', stripped):
                    edges.append((fm.group(1), "js_call"))
                # Script src references
                m = re.search(r'<script\s+src="([^"]+)"', stripped)
                if m:
                    edges.append((m.group(1), "script_ref"))
            elif language == "resx":
                pass  # resx files have no outgoing edges
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

    @staticmethod
    def _outline_row_to_dict(row: sqlite3.Row) -> dict[str, object]:
        """Convert an outline row to a dict, omitting falsy optional fields to reduce noise."""
        result: dict[str, object] = {
            "symbol": row["symbol"],
            "kind": row["kind"],
            "line_number": int(row["line_number"]),
        }
        if row["container"]:
            result["container"] = row["container"]
        if row["is_partial"]:
            result["is_partial"] = True
        return result

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

        variants: set[str] = set()
        suffixes = ("Dto", "Model", "ViewModel", "Entity", "Service", "Controller", "Settings", "Options", "Request", "Response", "Id")

        # Split multi-word concepts into individual words and generate variants for each
        words = raw.split()
        tokens = [raw] if len(words) <= 1 else words + [raw]

        # For multi-word queries, generate CamelCase/PascalCase/snake_case joins
        if len(words) > 1:
            # PascalCase: "create sql package" -> "CreateSqlPackage"
            pascal = "".join(w.capitalize() for w in words)
            variants.add(pascal)
            # camelCase: "create sql package" -> "createSqlPackage"
            camel = words[0].lower() + "".join(w.capitalize() for w in words[1:])
            variants.add(camel)
            # snake_case: "create sql package" -> "create_sql_package"
            snake = "_".join(w.lower() for w in words)
            variants.add(snake)
            # kebab-case: "create sql package" -> "create-sql-package"
            kebab = "-".join(w.lower() for w in words)
            variants.add(kebab)
            # Also add partial CamelCase combos for subsets
            for i in range(len(words)):
                for j in range(i + 2, len(words) + 1):
                    sub = "".join(w.capitalize() for w in words[i:j])
                    variants.add(sub)

        # For CamelCase input, also split into words for broader matching
        if len(words) == 1 and any(c.isupper() for c in raw[1:]):
            # Split CamelCase: "CreateSqlPackage" -> ["Create", "Sql", "Package"]
            camel_words = re.sub(r'([a-z])([A-Z])', r'\1 \2', raw).split()
            if len(camel_words) > 1:
                for cw in camel_words:
                    variants.add(cw)
                    variants.add(cw.lower())
                # Also add partial CamelCase combos
                for i in range(len(camel_words)):
                    for j in range(i + 2, len(camel_words) + 1):
                        sub = "".join(camel_words[i:j])
                        variants.add(sub)
                        variants.add(sub.lower())

        for token in tokens:
            variants.add(token)
            variants.add(token.lower())

            if token.endswith("s") and len(token) > 3:
                variants.add(token[:-1])
            else:
                variants.add(token + "s")

            for suffix in suffixes:
                if token.endswith(suffix) and len(token) > len(suffix):
                    variants.add(token[: -len(suffix)])
                variants.add(token + suffix)

            if token.startswith("Is") and len(token) > 2:
                variants.add(token[2:])
            elif len(token) > 1:
                variants.add("Is" + token[:1].upper() + token[1:])

            if token.startswith("Has") and len(token) > 3:
                variants.add(token[3:])
            elif len(token) > 1:
                variants.add("Has" + token[:1].upper() + token[1:])

        return [item for item in variants if item]

    def _path_weight(self, project_root: Path, path: str) -> int:
        lower = path.lower()
        score = 0
        config = load_index_config()
        positive_tokens = config.get("path_weight_positive", (
            "/src/", "/app/", "/web/", "/components/", "/services/",
            "/controllers/", "/models/", "/domain/", "/infrastructure/", "/application/",
        ))
        negative_tokens = config.get("path_weight_negative", (
            "/test/", "/tests/", "/fixture/", "/fixtures/",
            "/mock/", "/mocks/", "/example/", "/examples/",
            "/template/", "/templates/", "/generated/",
            "/snapshot/", "/assets/", "/pwaassets/", "/wwwroot/lib/", "/static/",
        ))
        pos_score = config.get("path_weight_positive_score", 20)
        neg_score = config.get("path_weight_negative_score", -35)
        for token in positive_tokens:
            if token in lower:
                score += pos_score
        for token in negative_tokens:
            if token in lower:
                score += neg_score
        hints = self._load_indexing_hints(project_root)
        for root in hints["preferred_roots"]:
            if lower.startswith(root):
                score += 40
        for root in hints["avoid_roots"]:
            if lower.startswith(root):
                score -= 60
        return score

    _ROLE_RELEVANCE_DEFAULT: dict[str, int] = {
        "service": 25, "controller": 20, "page-model": 18, "page-view": 15,
        "data-model": 15, "policy": 15, "partial-view": 12, "abstraction": 10,
        "configuration": 8, "utility": 5, "script": 3, "resource": 2,
        "asset-style": 1, "asset-style-source": 1,
    }

    @property
    def _ROLE_RELEVANCE(self) -> dict[str, int]:
        config = load_index_config()
        config_relevance = config.get("role_relevance")
        if config_relevance and isinstance(config_relevance, dict):
            return {k: int(v) for k, v in config_relevance.items()}
        return self._ROLE_RELEVANCE_DEFAULT

    def _role_relevance_boost(self, project_root: Path, path: str) -> int:
        """Score boost based on file role — services and controllers rank highest."""
        with self.connect(project_root) as conn:
            row = conn.execute("SELECT role FROM code_files WHERE path = ? LIMIT 1", (path,)).fetchone()
        if not row or not row["role"]:
            return 0
        return self._ROLE_RELEVANCE.get(row["role"], 0)

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
        # Split multi-word queries so each word matches independently
        words = needle.split()
        if len(words) > 1:
            clauses = " OR ".join(["(path LIKE ? OR summary LIKE ?)" for _ in words])
            params: list[object] = []
            for word in words:
                pattern = f"%{word}%"
                params.extend([pattern, pattern])
            params.append(limit)
            sql = f"SELECT path FROM code_files WHERE parsed = 0 AND ({clauses}) ORDER BY path ASC LIMIT ?"
        else:
            pattern = f"%{needle}%"
            params = [pattern, pattern, limit]
            sql = "SELECT path FROM code_files WHERE parsed = 0 AND (path LIKE ? OR summary LIKE ?) ORDER BY path ASC LIMIT ?"
        with self.connect(project_root) as conn:
            rows = conn.execute(sql, params).fetchall()
        paths = [row["path"] for row in rows]
        if not paths:
            return 0
        return self.sync_code_files(project_root, paths=paths)

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
