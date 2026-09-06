from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from .outline_extractors import parse_python_module


class CodeIndexEdgeService:
    def __init__(self, store: Any) -> None:
        self.store = store

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
                m = re.match(r"^@using\s+([A-Za-z_][A-Za-z0-9_\.]*)", stripped)
                if m:
                    edges.append((m.group(1), "using"))
                m = re.match(r"^@model\s+([A-Za-z_][A-Za-z0-9_\.]*)", stripped)
                if m:
                    edges.append((m.group(1), "model_binding"))
                m = re.match(
                    r"^@inject\s+([A-Za-z_][A-Za-z0-9_<>,\.\s]*?)\s+[A-Za-z_][A-Za-z0-9_]*\s*$",
                    stripped,
                )
                if m:
                    edges.append((m.group(1).strip(), "inject"))
                for pm in re.finditer(r'<partial\s+name="([^"]+)"', stripped, re.IGNORECASE):
                    edges.append((pm.group(1), "partial_ref"))
                for pm in re.finditer(r'Html\.PartialAsync\(\s*"([^"]+)"', stripped):
                    edges.append((pm.group(1), "partial_ref"))
                for pm in re.finditer(r'Component\.InvokeAsync\(\s*"([^"]+)"', stripped):
                    edges.append((pm.group(1), "component_ref"))
                m = re.search(r'Layout\s*=\s*"([^"]+)"', stripped)
                if m:
                    edges.append((m.group(1), "layout_ref"))
                for fm in re.finditer(r'onclick="([A-Za-z_][A-Za-z0-9_.]*)\s*\(', stripped):
                    edges.append((fm.group(1), "js_call"))
                m = re.search(r'<script\s+src="([^"]+)"', stripped)
                if m:
                    edges.append((m.group(1), "script_ref"))
            elif language == "resx":
                pass
        seen = set()
        result: list[tuple[str, str]] = []
        for edge in edges:
            if edge in seen:
                continue
            seen.add(edge)
            result.append(edge)
        return result

    def _extract_python_edges(self, text: str) -> list[tuple[str, str]]:
        """File-level import edges. ast.walk descends into function bodies, so a
        function-local (lazy) import lands here as a plain "import" edge,
        indistinguishable from a module-level one and with no line number —
        code_edges is (source_path, target, kind) and cannot express either.
        That is not a mis-kinding to fix in this shape: line-level lazy imports
        with their enclosing symbol are captured as `lazy_import` rows in
        code_references (see outline_extractors.python.extract_python_references).
        The `dynamic_import` kind is JS `import()` only — it never applied to
        Python.
        """
        edges: list[tuple[str, str]] = []
        tree = parse_python_module(text)
        if tree is None:
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
            candidates.extend(
                self._existing_relative_candidates(project_root, module_base, python_only=True),
            )
        elif kind == "using":
            with self.store.connect(project_root) as conn:
                rows = conn.execute(
                    "SELECT DISTINCT path FROM code_outlines WHERE container = ? ORDER BY path LIMIT ?",
                    (target, limit),
                ).fetchall()
            candidates.extend([row["path"] for row in rows])

        seen = set()
        resolved: list[str] = []
        for item in candidates:
            if item not in seen:
                seen.add(item)
                resolved.append(item)
        return resolved[:limit]

    def _existing_relative_candidates(
        self,
        project_root: Path,
        base: Path,
        python_only: bool = False,
    ) -> list[str]:
        options: list[Path] = []
        if base.suffix:
            options.append(base)
        elif python_only:
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
                ],
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
