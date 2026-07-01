from __future__ import annotations

import ast
import re

# Skip rules for module-level constants. Inverted-blocklist: index
# everything assigned at module scope, except obvious noise. Catches
# UPPER_SNAKE config tables, TypeVar/NewType aliases, dataclass-style
# bare assigns, single-letter type vars (T, K), without the curating
# cost of a positive list.
_USELESS_NAME = re.compile(r"^_+\d*$")  # _, __, _1


def _is_useful_constant_name(name: str) -> bool:
    """Skip names that are almost certainly noise."""
    if not name or _USELESS_NAME.fullmatch(name):
        return False
    # Lowercase 1-2 char names (i, j, fp, id) — likely locals.
    # Single uppercase letters (T, K, V) are legitimate TypeVars; keep.
    if len(name) <= 2 and name.islower():
        return False
    return True


def extract_python_outline(text: str) -> list[tuple[str, str, int, str | None, bool]]:
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
                elif isinstance(child, ast.AnnAssign):
                    # Typed class-body assign: dataclass field, Pydantic
                    # model field, or plain `name: Type = default`.
                    # Always indexed — typed declarations carry intent.
                    if isinstance(child.target, ast.Name):
                        nm = child.target.id
                        if _is_useful_constant_name(nm):
                            outlines.append((nm, "field", child.lineno, node.name, False))
                elif isinstance(child, ast.Assign):
                    # Bare class-body assign: UPPER_SNAKE class constants,
                    # dispatch tables, event-kind tuples. Same noise rule
                    # as module-level assigns.
                    for tgt in child.targets:
                        for nm in _collect_assign_names(tgt):
                            if nm == "_":
                                continue
                            if _is_useful_constant_name(nm):
                                outlines.append((nm, "field", child.lineno, node.name, False))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "function"
            if node.name.startswith("use") and len(node.name) > 3 and node.name[3:4].isupper():
                kind = "hook"
            outlines.append((node.name, kind, node.lineno, None, False))
            # Index nested functions as first-class symbols with parent as container
            _extract_nested_functions(node, node.name, outlines)
        elif isinstance(node, ast.Assign):
            # Bare assigns: `X = ...`, `X = Y = ...`, `(a, b) = ...`.
            # Tuple/list-unpacking with `_` placeholder: skip the row entirely
            # (likely throwaway destructuring like `result, _ = foo()`).
            for target in node.targets:
                names = _collect_assign_names(target)
                if any(n == "_" for n in names):
                    continue
                for name in names:
                    if _is_useful_constant_name(name):
                        outlines.append((name, "constant", node.lineno, None, False))
        elif isinstance(node, ast.AnnAssign):
            # Typed module-level assigns: `X: int = 5`, `Final[str]`, `T: TypeAlias = ...`.
            # Always informative — typed declarations carry intent. Index even
            # if the name is short.
            if isinstance(node.target, ast.Name):
                name = node.target.id
                if _is_useful_constant_name(name):
                    outlines.append((name, "constant", node.lineno, None, False))

    return outlines


def _collect_assign_names(target: ast.expr) -> list[str]:
    """Walk an Assign target; return all bound Name ids. Handles tuple
    and list unpacking. Attribute / subscript targets are skipped
    (those mutate existing objects, not module-scope declarations).
    """
    names: list[str] = []
    if isinstance(target, ast.Name):
        names.append(target.id)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            names.extend(_collect_assign_names(elt))
    elif isinstance(target, ast.Starred):
        names.extend(_collect_assign_names(target.value))
    return names


def _extract_nested_functions(
    parent: ast.FunctionDef | ast.AsyncFunctionDef,
    container: str,
    outlines: list[tuple[str, str, int, str | None, bool]],
) -> None:
    """Walk function body for nested def/async def — makes factory-pattern locals discoverable."""
    for child in ast.iter_child_nodes(parent):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            outlines.append((child.name, "function", child.lineno, container, False))
            # Recurse one more level for deeply nested functions
            _extract_nested_functions(child, f"{container}.{child.name}", outlines)
