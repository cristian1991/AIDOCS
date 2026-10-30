from __future__ import annotations

import ast
import builtins
import re
import symtable
from collections import Counter
from pathlib import Path
from typing import Any


def _bound_name(node: ast.Import | ast.ImportFrom, alias: ast.alias) -> str:
    if alias.asname:
        return alias.asname
    if isinstance(node, ast.Import):
        return alias.name.split(".")[0]
    return alias.name


def _module_imports(text: str) -> list[tuple[set[str], str, int]]:
    """Top-level imports with bound names, source text, and relative level."""
    tree = ast.parse(text)
    lines = text.splitlines()
    records: list[tuple[set[str], str, int]] = []
    for node in tree.body:
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        names = {_bound_name(node, alias) for alias in node.names}
        statement = "\n".join(lines[node.lineno - 1 : node.end_lineno])
        level = node.level if isinstance(node, ast.ImportFrom) else 0
        records.append((names, statement, level))
    return records



def _module_definition_counts(text: str) -> Counter[str]:
    tree = ast.parse(text)
    return Counter(
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    )



def _added_top_level_nodes(old_text: str, new_text: str) -> list[ast.stmt]:
    """Return top-level nodes added to ``new_text`` using AST multiset subtraction."""
    old_counts = Counter(
        ast.dump(node, include_attributes=False) for node in ast.parse(old_text).body
    )
    added: list[ast.stmt] = []
    for node in ast.parse(new_text).body:
        key = ast.dump(node, include_attributes=False)
        if old_counts[key]:
            old_counts[key] -= 1
        else:
            added.append(node)
    return added


def _external_names(nodes: list[ast.stmt]) -> set[str]:
    if not nodes:
        return set()
    text = ast.unparse(ast.Module(body=nodes, type_ignores=[]))
    special = {
        "__name__",
        "__file__",
        "__doc__",
        "__package__",
        "__spec__",
        "__loader__",
        "__builtins__",
        "__debug__",
    }
    return (
        _global_references(text)
        - _module_bound_names(text)
        - set(dir(builtins))
        - special
    )


def _global_references(text: str) -> set[str]:
    """Names referenced through module/global resolution across nested scopes."""
    root = symtable.symtable(text, "<deslop-module>", "exec")
    referenced: set[str] = set()

    def visit(table: symtable.SymbolTable) -> None:
        for identifier in table.get_identifiers():
            symbol = table.lookup(identifier)
            if symbol.is_referenced() and symbol.is_global():
                referenced.add(identifier)
        for child in table.get_children():
            visit(child)

    visit(root)
    return referenced


def _module_bound_names(text: str) -> set[str]:
    table = symtable.symtable(text, "<deslop-module>", "exec")
    return {
        identifier
        for identifier in table.get_identifiers()
        if (
            table.lookup(identifier).is_imported()
            or table.lookup(identifier).is_assigned()
            or table.lookup(identifier).is_namespace()
        )
    }


def _module_non_import_bound_names(text: str) -> set[str]:
    table = symtable.symtable(text, "<deslop-module>", "exec")
    return {
        identifier
        for identifier in table.get_identifiers()
        if (
            table.lookup(identifier).is_assigned()
            or table.lookup(identifier).is_namespace()
        )
        and not table.lookup(identifier).is_imported()
    }


def _insert_index(lines: list[str]) -> int:
    if not lines:
        return 0
    index = 1 if lines[0].startswith("#!") else 0
    coding = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*[-_.a-zA-Z0-9]+")
    for offset, line in enumerate(lines[:2], start=1):
        if coding.match(line):
            index = max(index, offset)

    tree = ast.parse("\n".join(lines))
    body = list(tree.body)
    cursor = 0
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        if isinstance(body[0].value.value, str):
            index = max(index, int(body[0].end_lineno or body[0].lineno))
            cursor = 1
    while cursor < len(body) and isinstance(body[cursor], (ast.Import, ast.ImportFrom)):
        index = max(index, int(body[cursor].end_lineno or body[cursor].lineno))
        cursor += 1
    return index


def _insert_imports(text: str, statements: list[str]) -> str:
    existing = {statement for _, statement, _ in _module_imports(text)}
    unique = [
        statement
        for statement in dict.fromkeys(statements)
        if statement.strip() and statement not in existing
    ]
    if not unique:
        return text
    lines = text.splitlines()
    index = _insert_index(lines)
    payload: list[str] = []
    for statement in unique:
        payload.extend(statement.splitlines())
    if index < len(lines) and lines[index].strip():
        payload.append("")
    managed = lines[:index] + payload + lines[index:]
    return "\n".join(managed) + ("\n" if text.endswith("\n") else "")


def _package_parts(project_root: Path, directory: Path) -> list[str]:
    """Return the contiguous Python-package suffix below the project root."""
    parts: list[str] = []
    cursor = directory.resolve()
    root = project_root.resolve()
    while cursor != root and cursor.is_relative_to(root):
        if not (cursor / "__init__.py").is_file():
            break
        parts.append(cursor.name)
        cursor = cursor.parent
    parts.reverse()
    return parts


def _back_import_module(
    project_root: Path,
    source_path: str,
    target_path: str,
) -> str:
    """Derive a safe import route from source module to extracted target."""
    source_file = (project_root / source_path).resolve()
    target_file = (project_root / target_path).resolve()
    target_name = target_file.stem

    if source_file.parent == target_file.parent:
        if (source_file.parent / "__init__.py").is_file():
            return "." if target_name == "__init__" else f".{target_name}"
        return "" if target_name == "__init__" else target_name

    source_package = _package_parts(project_root, source_file.parent)
    target_package = _package_parts(project_root, target_file.parent)
    if not source_package or not target_package:
        return ""
    if source_package[0] != target_package[0]:
        return ""

    target_module = target_package + ([] if target_name == "__init__" else [target_name])
    common = 0
    for source_part, target_part in zip(source_package, target_module):
        if source_part != target_part:
            break
        common += 1
    up = len(source_package) - common
    remainder = target_module[common:]
    return "." * (up + 1) + ".".join(remainder)


def manage_python_extraction_imports(
    project_root: Path,
    source_path: str,
    target_path: str,
    *,
    before_source: bytes,
    before_target: bytes,
) -> dict[str, Any]:
    """Repair Python dependencies after a text extraction, or demand rollback."""
    if not source_path.endswith(".py") or not target_path.endswith(".py"):
        return {
            "ok": True,
            "changed": False,
            "target_imports": [],
            "source_imports": [],
        }

    def refuse(blocked_by: str, error: str, **extra: Any) -> dict[str, Any]:
        return {
            "ok": False,
            "changed": False,
            "rollback_required": True,
            "blocked_by": blocked_by,
            "error": error,
            **extra,
        }

    source_file = project_root / source_path
    target_file = project_root / target_path
    try:
        after_source = source_file.read_text(encoding="utf-8")
        after_target = target_file.read_text(encoding="utf-8")
        old_source = before_source.decode("utf-8")
        old_target = before_target.decode("utf-8")

        source_imports = _module_imports(old_source)
        source_import_names = {
            name
            for names, _, _ in source_imports
            for name in names
        }
        source_non_import_bound = _module_non_import_bound_names(old_source)
        old_target_imports = _module_imports(old_target)
        old_target_import_statements = {
            statement for _, statement, _ in old_target_imports
        }
        old_target_bound = _module_bound_names(old_target)
        old_target_non_import_bound = _module_non_import_bound_names(old_target)

        old_definition_counts = _module_definition_counts(old_target)
        new_definition_counts = _module_definition_counts(after_target)
        added_nodes = _added_top_level_nodes(old_target, after_target)
        added_external = _external_names(added_nodes)
        moved = {
            node.name
            for node in added_nodes
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        return refuse("import_management_failed", f"import management failed: {exc}")

    target_symbol_collisions = sorted(
        name
        for name, count in new_definition_counts.items()
        if old_definition_counts[name] and count > old_definition_counts[name]
    )
    if target_symbol_collisions:
        return refuse(
            "target_symbol_collision",
            "target already defines extracted top-level symbol(s): "
            + ", ".join(target_symbol_collisions),
            collisions=target_symbol_collisions,
        )

    source_local_dependencies = sorted(added_external & source_non_import_bound)
    if source_local_dependencies:
        return refuse(
            "source_local_dependency_requires_refactor",
            "extracted code depends on source-local definitions that cannot be "
            "safely converted into imports: " + ", ".join(source_local_dependencies),
            dependencies=source_local_dependencies,
        )

    required_import_names = added_external & source_import_names
    copied: list[str] = []
    for names, statement, relative_level in source_imports:
        needed = names & required_import_names
        if not needed:
            continue
        binding_collisions = sorted(needed & old_target_non_import_bound)
        if binding_collisions:
            return refuse(
                "target_import_binding_collision",
                "target binds required import name(s) as non-import values: "
                + ", ".join(binding_collisions),
                collisions=binding_collisions,
            )
        if statement in old_target_import_statements:
            continue
        existing_bindings = sorted(names & old_target_bound)
        if existing_bindings:
            return refuse(
                "target_import_binding_collision",
                "target already binds name(s) from a different import shape: "
                + ", ".join(existing_bindings),
                collisions=existing_bindings,
            )
        if relative_level and source_file.parent.resolve() != target_file.parent.resolve():
            return refuse(
                "relative_import_relocation_unsafe",
                "required relative import cannot be copied across package "
                f"directories safely: {statement!r}",
            )
        copied.append(statement)
        old_target_bound.update(names)
    managed_target = _insert_imports(after_target, copied)

    try:
        referenced_moved = moved & _global_references(after_source)
        remaining_source_bound = _module_bound_names(after_source)
    except SyntaxError as exc:
        return refuse("import_management_failed", f"source analysis failed: {exc}")
    source_back_collisions = sorted(referenced_moved & remaining_source_bound)
    if source_back_collisions:
        return refuse(
            "source_back_import_collision",
            "source still binds moved symbol name(s), so an automatic back-import "
            "would be ambiguous: " + ", ".join(source_back_collisions),
            collisions=source_back_collisions,
        )

    added: list[str] = []
    managed_source = after_source
    needed_back = sorted(referenced_moved)
    if needed_back:
        module = _back_import_module(project_root, source_path, target_path)
        if not module:
            return refuse(
                "back_import_route_unsafe",
                "cannot derive a safe Python import route from "
                f"{source_path!r} to {target_path!r}",
            )
        statement = f"from {module} import {', '.join(needed_back)}"
        managed_source = _insert_imports(after_source, [statement])
        added.append(statement)

    changed_paths: list[str] = []
    try:
        if managed_target != after_target:
            target_file.write_text(managed_target, encoding="utf-8", newline="")
            changed_paths.append(target_path)
        if managed_source != after_source:
            source_file.write_text(managed_source, encoding="utf-8", newline="")
            changed_paths.append(source_path)
    except OSError as exc:
        return {
            "ok": False,
            "changed": bool(changed_paths),
            "changed_paths": changed_paths,
            "target_imports": copied,
            "source_imports": added,
            "rollback_required": True,
            "blocked_by": "import_management_write_failed",
            "error": f"import management write failed: {exc}",
        }

    return {
        "ok": True,
        "changed": bool(changed_paths),
        "changed_paths": changed_paths,
        "target_imports": copied,
        "source_imports": added,
    }
