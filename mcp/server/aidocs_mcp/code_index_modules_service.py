from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .language_descriptors import entry_points_from_descriptors, load_index_config


class CodeIndexModulesService:
    def __init__(self, store: Any) -> None:
        self.store = store

    @property
    def _MODULE_MANIFESTS(self) -> dict[str, str]:
        config = load_index_config()
        manifests = config.get("module_manifests")
        return (
            dict(manifests)
            if manifests and isinstance(manifests, dict)
            else self.store._MODULE_MANIFESTS_DEFAULT
        )

    @property
    def _MODULE_SKIP_DIRS(self) -> set[str]:
        config = load_index_config()
        skip = config.get("skip_dirs")
        return set(skip) if skip and isinstance(skip, set) else self.store._MODULE_SKIP_DIRS_DEFAULT

    def _get_entry_point_patterns(self, project_root: Path) -> list[tuple[str, str]]:
        """Get entry point patterns: descriptor-defined first, then defaults."""
        try:
            eps = entry_points_from_descriptors(project_root)
            if eps:
                return list(eps.items())
        except Exception:
            pass
        return self.store._ENTRY_POINT_PATTERNS_DEFAULT

    # Well-known top-level directory names that strongly suggest a module.
    _MODULE_HINT_DIRS_BASE: set[str] = {
        "app",
        "cli",
        "core",
        "lib",
        "server",
        "web",
        "website",
        "api",
        "frontend",
        "backend",
        "services",
        "packages",
        "plugins",
        "analyzer",
        "worker",
        "workers",
        "gateway",
        "proxy",
        "admin",
        "dashboard",
        "mobile",
        "desktop",
        "console",
        "docs",
        "sdk",
        "engine",
        "database-generators",
        "feature-generators",
        "framework-generators",
        "archetypes",
        "templates",
        "schemas",
        # Common project dirs that hold meaningful source
        "scripts",
        "components",
        "pages",
        "views",
        "layouts",
        "prisma",
        "config",
        "configs",
        "models",
        "entities",
        "middleware",
        "hooks",
        "utils",
        "helpers",
        "tools",
        "examples",
        "demo",
        "assets",
        "public",
        "static",
        "infra",
        "infrastructure",
        "deploy",
        "ci",
        "shared",
        "common",
        "internal",
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
                                    modules.append(
                                        self._build_module_entry(
                                            project_root,
                                            rel,
                                            "workspace",
                                            "javascript",
                                            self._find_entry_point(match_path),
                                        ),
                                    )
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
                                        modules.append(
                                            self._build_module_entry(
                                                project_root,
                                                rel,
                                                "workspace",
                                                "javascript",
                                                self._find_entry_point(match_path),
                                            ),
                                        )
                    elif (
                        in_packages
                        and not stripped.startswith("-")
                        and not stripped.startswith("#")
                    ):
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
                                        modules.append(
                                            self._build_module_entry(
                                                project_root,
                                                rel,
                                                "workspace",
                                                "rust",
                                                self._find_entry_point(match_path),
                                            ),
                                        )
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
        subproject_paths = [
            m["module_path"] for m in modules if m["kind"] in ("subproject", "workspace")
        ]
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
                                        modules.append(
                                            self._build_module_entry(
                                                project_root,
                                                rel,
                                                "workspace",
                                                "javascript",
                                                self._find_entry_point(match_path),
                                            ),
                                        )
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
                                    if (
                                        match_path.is_dir()
                                        and (match_path / "package.json").is_file()
                                    ):
                                        rel = match_path.relative_to(project_root).as_posix()
                                        if rel not in seen:
                                            seen.add(rel)
                                            modules.append(
                                                self._build_module_entry(
                                                    project_root,
                                                    rel,
                                                    "workspace",
                                                    "javascript",
                                                    self._find_entry_point(match_path),
                                                ),
                                            )
                        elif (
                            in_packages
                            and not stripped.startswith("-")
                            and not stripped.startswith("#")
                        ):
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
                if rel not in seen and not any(
                    skip in rel.lower().split("/") for skip in self._MODULE_SKIP_DIRS
                ):
                    seen.add(rel)
                    modules.append(
                        self._build_module_entry(
                            project_root,
                            rel,
                            "project",
                            "csharp",
                            csproj.relative_to(project_root).as_posix(),
                        ),
                    )

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
                    modules.append(
                        self._build_module_entry(
                            project_root,
                            rel,
                            "subproject",
                            stack,
                            self._find_entry_point(child),
                        ),
                    )
                    break

            if rel in seen:
                continue

            # Check for entry point files
            entry = self._find_entry_point(child)
            if entry:
                stack = self._stack_from_entry(entry)
                seen.add(rel)
                modules.append(
                    self._build_module_entry(
                        project_root,
                        rel,
                        "module",
                        stack,
                        entry,
                    ),
                )
                continue

            # Check if this is a well-known module-like directory with source files
            if name_lower in self.store._module_hint_dirs(project_root):
                source_count = sum(
                    1
                    for _ in child.rglob("*")
                    if _.is_file()
                    and self.store._language_for(_, project_root=project_root) is not None
                )
                if source_count > 0:
                    stack = self._guess_stack_from_dir(project_root, child)
                    seen.add(rel)
                    modules.append(
                        self._build_module_entry(
                            project_root,
                            rel,
                            "module",
                            stack,
                            self._find_entry_point(child),
                        ),
                    )
                    continue

            # Fallback: any top-level dir with 2+ source files is an informal module
            source_count = 0
            checked = 0
            for f in child.rglob("*"):
                checked += 1
                if checked > 500:
                    break  # cap walk to avoid deep traversals
                if (
                    f.is_file()
                    and self.store._language_for(f, project_root=project_root) is not None
                ):
                    if not any(
                        skip in f.relative_to(child).as_posix().lower().split("/")
                        for skip in self._MODULE_SKIP_DIRS
                    ):
                        source_count += 1
                        if source_count >= 2:
                            break
            if source_count >= 2:
                stack = self._guess_stack_from_dir(project_root, child)
                seen.add(rel)
                modules.append(
                    self._build_module_entry(
                        project_root,
                        rel,
                        "module",
                        stack,
                        self._find_entry_point(child),
                    ),
                )

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
                if (
                    f.is_file()
                    and self.store._language_for(f, project_root=project_root) is not None
                ):
                    if not self.store._should_skip(project_root, f, include_tests=False):
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
        patterns = self._get_entry_point_patterns(
            getattr(self.store, "_last_project_root", None) or directory,
        )
        for pattern, _ in patterns:
            candidate = directory / pattern
            if candidate.is_file():
                return candidate.name
        return None

    def _stack_from_entry(self, entry: str) -> str | None:
        patterns = self._get_entry_point_patterns(
            getattr(self.store, "_last_project_root", None) or Path(),
        )
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
                    lang = self.store._language_for(f, project_root=project_root)
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
        self.store.init_db(project_root)
        modules = self.detect_modules(project_root)
        with self.store.connect(project_root) as conn:
            conn.execute("DELETE FROM code_modules")
            conn.executemany(
                "INSERT INTO code_modules (module_path, name, kind, stack, entry_point, file_count, description) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        m["module_path"],
                        m["name"],
                        m["kind"],
                        m["stack"],
                        m["entry_point"],
                        m["file_count"],
                        m["description"],
                    )
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
        self.store.init_db(project_root)
        with self.store.connect(project_root) as conn:
            if kind:
                rows = conn.execute(
                    "SELECT * FROM code_modules WHERE kind = ? ORDER BY module_path",
                    (kind,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM code_modules ORDER BY module_path").fetchall()
        return [dict(r) for r in rows]

    def get_module_files(
        self,
        project_root: Path,
        module_path: str,
        limit: int = 200,
        modified_since_ns: int | None = None,
    ) -> list[dict[str, object]]:
        """Get all indexed files belonging to a module.

        Matching is path-prefix based (the same rule sync_modules uses to tag
        files), with the ``module`` column as a secondary match. This keeps
        the listing in agreement with get_modules even when a later reindex
        rewrote code_files rows and dropped the module tags (phoenix bug 1).
        LIKE metacharacters in the module path are escaped so 'my_lib' never
        matches 'myxlib'.
        """
        normalized = str(module_path or "").strip().replace("\\", "/").strip("/")
        if not normalized:
            return []
        self.store.init_db(project_root)
        escaped = (
            normalized.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        )
        where = "(module = ? OR path LIKE ? ESCAPE '!')"
        params: list[object] = [normalized, escaped + "/%"]
        if modified_since_ns is not None:
            where += " AND mtime_ns >= ?"
            params.append(modified_since_ns)
        params.append(limit)
        with self.store.connect(project_root) as conn:
            rows = conn.execute(
                f"SELECT path, language, role, line_count, summary FROM code_files WHERE {where} ORDER BY path LIMIT ?",
                params,
            ).fetchall()
        return [dict(r) for r in rows]
