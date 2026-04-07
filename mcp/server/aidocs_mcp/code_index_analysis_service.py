from __future__ import annotations

from pathlib import Path
from typing import Any


class CodeIndexAnalysisService:
    def __init__(self, store: Any) -> None:
        self.store = store

    def preview_extraction_deps(
        self,
        project_root: Path,
        path: str,
        start_line: int,
        end_line: int,
    ) -> dict[str, object]:
        """Scan a code block and find names it depends on that are defined outside the block."""
        abs_path = (project_root / path.replace("\\", "/")).resolve()
        if not abs_path.is_file():
            return {"error": f"File not found: {path}"}
        try:
            text = abs_path.read_text(encoding="utf-8")
        except Exception as exc:
            return {"error": str(exc)}

        lines = text.splitlines()
        if start_line < 1 or end_line > len(lines) or start_line > end_line:
            return {"error": f"Invalid line range {start_line}-{end_line} (file has {len(lines)} lines)"}

        block_lines = lines[start_line - 1 : end_line]
        block_text = "\n".join(block_lines)
        outside_before = "\n".join(lines[: start_line - 1])
        outside_after = "\n".join(lines[end_line:])
        outside_text = outside_before + "\n" + outside_after

        ext = abs_path.suffix.lower()
        if ext == ".py":
            return self._preview_deps_python(path, text, block_text, outside_text, start_line, end_line)
        if ext in {".js", ".ts", ".jsx", ".tsx"}:
            return self._preview_deps_js(path, text, block_text, outside_text, start_line, end_line)
        return {"path": path, "start": start_line, "end": end_line, "deps": [], "imports_needed": []}

    @staticmethod
    def _preview_deps_python(
        path: str, full_text: str, block_text: str, outside_text: str,
        start_line: int, end_line: int,
    ) -> dict[str, object]:
        import ast
        import re as _re

        # Parse the block to find all Name references and self.* accesses
        block_names: set[str] = set()
        self_attrs: set[str] = set()
        try:
            block_tree = ast.parse(block_text)
            for node in ast.walk(block_tree):
                if isinstance(node, ast.Name):
                    block_names.add(node.id)
                elif isinstance(node, ast.Attribute):
                    if isinstance(node.value, ast.Name):
                        if node.value.id == "self":
                            self_attrs.add(node.attr)
                        else:
                            block_names.add(node.value.id)
        except SyntaxError:
            block_names = set(_re.findall(r'\b([A-Za-z_]\w*)\b', block_text))
            self_attrs = set(_re.findall(r'self\.([A-Za-z_]\w*)', block_text))

        # Find what's defined in the block itself
        block_defined: set[str] = set()
        try:
            for node in ast.walk(ast.parse(block_text)):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    block_defined.add(node.name)
                    for arg in node.args.args + node.args.posonlyargs + node.args.kwonlyargs:
                        block_defined.add(arg.arg)
                elif isinstance(node, ast.ClassDef):
                    block_defined.add(node.name)
                elif isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            block_defined.add(target.id)
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    for alias in node.names:
                        name = alias.asname or alias.name.split(".")[0]
                        block_defined.add(name)
        except SyntaxError:
            pass

        external_names = block_names - block_defined - {"self", "cls", "True", "False", "None"}

        # Categorize external names
        imports_needed: list[str] = []
        helpers_needed: list[str] = []
        try:
            full_tree = ast.parse(full_text)
            imported_names: set[str] = set()
            defined_names: set[str] = set()
            for node in full_tree.body:
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    for alias in node.names:
                        imported_names.add(alias.asname or alias.name.split(".")[0])
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    defined_names.add(node.name)
                elif isinstance(node, ast.ClassDef):
                    defined_names.add(node.name)
                elif isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            defined_names.add(target.id)

            for name in sorted(external_names):
                if name in imported_names:
                    imports_needed.append(name)
                elif name in defined_names:
                    helpers_needed.append(name)
        except SyntaxError:
            pass

        # Classify self.* references — find which class methods/properties are used
        self_methods: list[str] = []
        self_properties: list[str] = []
        if self_attrs:
            try:
                # Find the containing class and its method/property names
                class_methods: set[str] = set()
                class_properties: set[str] = set()
                for node in ast.walk(ast.parse(full_text)):
                    if isinstance(node, ast.ClassDef):
                        for child in node.body:
                            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                                class_methods.add(child.name)
                            elif isinstance(child, ast.Assign):
                                for target in child.targets:
                                    if isinstance(target, ast.Name):
                                        class_properties.add(target.id)

                for attr in sorted(self_attrs):
                    if attr in class_methods:
                        self_methods.append(attr)
                    else:
                        self_properties.append(attr)
            except SyntaxError:
                self_methods = sorted(self_attrs)

        result: dict[str, object] = {
            "path": path,
            "start": start_line,
            "end": end_line,
            "imports_needed": imports_needed,
            "helpers_needed": helpers_needed,
            "total_external_deps": len(imports_needed) + len(helpers_needed),
        }
        if self_methods or self_properties:
            result["self_methods_used"] = self_methods
            result["self_properties_used"] = self_properties
            result["ownership_warning"] = (
                f"Block uses {len(self_methods)} self.method() calls and "
                f"{len(self_properties)} self.property references that need "
                f"rewiring after extraction to a new class."
            )
        return result

    @staticmethod
    def _preview_deps_js(
        path: str, full_text: str, block_text: str, outside_text: str,
        start_line: int, end_line: int,
    ) -> dict[str, object]:
        import re as _re

        # Extract all identifiers used in block
        block_names = set(_re.findall(r'\b([A-Za-z_$]\w*)\b', block_text))
        # Remove JS keywords
        js_keywords = {
            "const", "let", "var", "function", "return", "if", "else", "for", "while",
            "do", "switch", "case", "break", "continue", "try", "catch", "finally",
            "throw", "new", "delete", "typeof", "instanceof", "void", "in", "of",
            "class", "extends", "super", "this", "import", "export", "from", "default",
            "async", "await", "yield", "true", "false", "null", "undefined",
        }
        block_names -= js_keywords

        # Find imports in the full file
        import_pattern = _re.compile(r'import\s+(?:type\s+)?(?:\{([^}]+)\}|(\w+)).*?from', _re.MULTILINE)
        imported: set[str] = set()
        for m in import_pattern.finditer(full_text):
            if m.group(1):
                for name in m.group(1).split(","):
                    imported.add(name.strip().split(" as ")[-1].strip())
            elif m.group(2):
                imported.add(m.group(2))

        imports_needed = sorted(name for name in block_names if name in imported)

        return {
            "path": path,
            "start": start_line,
            "end": end_line,
            "imports_needed": imports_needed,
            "helpers_needed": [],
            "total_external_deps": len(imports_needed),
        }


    def find_symbol_range(
        self,
        project_root: Path,
        path: str,
        symbol: str,
        kind: str | None = None,
        line_number: int | None = None,
    ) -> dict[str, object]:
        """Find the start and end line of a symbol using indexed outlines + block extraction."""
        self.store.init_db(project_root)
        with self.store.connect(project_root) as conn:
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
            snippet = self.store._extract_indent_block(lines, start_idx)
        elif lang in {"javascript", "typescript", "jsx", "tsx", "csharp"}:
            snippet = self.store._extract_brace_block(lines, start_idx)
        else:
            snippet = "\n".join(lines[start_idx:min(len(lines), start_idx + 20)])

        end_line = int(row["line_number"]) + snippet.rstrip("\n").count("\n")

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
        self.store.init_db(project_root)
        abs_path = (project_root / path.replace("\\", "/")).resolve()
        if not abs_path.is_file():
            return []
        text = abs_path.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()

        with self.store.connect(project_root) as conn:
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
                snippet = self.store._extract_indent_block(lines, start_idx)
            elif language in {"javascript", "typescript", "jsx", "tsx", "csharp"}:
                snippet = self.store._extract_brace_block(lines, start_idx)
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
            matches = self.store.search_text(
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




