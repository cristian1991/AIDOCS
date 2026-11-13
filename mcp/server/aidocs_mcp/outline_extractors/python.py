from __future__ import annotations

import ast
import re
from functools import lru_cache

# Skip rules for module-level constants. Inverted-blocklist: index
# everything assigned at module scope, except obvious noise. Catches
# UPPER_SNAKE config tables, TypeVar/NewType aliases, dataclass-style
# bare assigns, single-letter type vars (T, K), without the curating
# cost of a positive list.
_USELESS_NAME = re.compile(r"^_+\d*$")  # _, __, _1

# Shared parse cache. The sync parse-loop hands the SAME text object to the
# outline pass, the edge pass and the reference pass — three ast.parse calls on
# identical source. maxsize=2 keeps only the file in flight (plus one) so the
# cache never grows with project size, and str.__hash__ is memoized on the
# object, making the lookup free after the first pass.
@lru_cache(maxsize=2)
def _parse_cached(text: str) -> ast.Module | None:
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return None


def parse_python_module(text: str) -> ast.Module | None:
    """Parse `text`, reusing the tree across the index passes. None on syntax error."""
    return _parse_cached(text)


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
    tree = parse_python_module(text)
    if tree is None:
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


# ── AST reference capture (code_references writer) ──────────────────────
#
# WHY AST, NOT REGEX: every row here is real code. A name inside a comment,
# a docstring or any string literal is invisible to the parser, so it can
# never enter the table — that exclusion is most of this table's value over
# the file-content scan it replaces.
#
# NAME-KEYED BY DESIGN: `token` is the called NAME as written, never a
# resolved definition. No receiver-type resolution is attempted. A name-keyed
# graph OVER-approximates the true call graph, so a forbidden-reachability
# query ("this read must not reach a writer") yields false positives and
# never false negatives — the safe direction for a safety property.
#
# Kinds emitted:
#   call        — the callee as written: "run", or dotted "self._store.init_db"
#   attr_call   — the bare tail name of a dotted callee ("init_db"), so a
#                 short-name lookup resolves without a trailing-wildcard scan
#   lazy_import — an import statement INSIDE a function/method. In this
#                 codebase the function-local import IS the call edge, and
#                 code_edges cannot express it (file-level, no line, and its
#                 walk folds locals into plain "import" — see
#                 code_index_edge_service._extract_python_edges).
#
# `enclosing` is the dotted scope the reference sits inside ("Class.method",
# "outer.inner", "" at module level). Without it caller->callee cannot be
# derived at all: code_outlines stores no end-line, so there is no span to
# recover an enclosing symbol from after the fact.

# Bounds a pathological/generated file. Chosen far above the observed
# per-file maximum on this repo so real source is never truncated.
_AST_REF_MAX_ROWS = 50000
_AST_REF_RAW_MAX = 200


def extract_python_references(text: str) -> list[tuple[str, int, str, str, str]]:
    """Return (token, line_number, kind, raw, enclosing) reference rows.

    Attribution rules worth knowing:
      * decorators, default values and annotations on a def are attributed to
        that def (they are part of its declaration, not the outer scope);
      * comprehensions and generator expressions do NOT open a scope here —
        their calls attribute to the enclosing function;
      * a lambda opens a "<lambda>" scope suffix.
    """
    tree = parse_python_module(text)
    if tree is None:
        return []
    lines = text.splitlines()
    rows: list[tuple[str, int, str, str, str]] = []
    _walk_refs(tree, "", lines, rows)
    return rows


def _raw_line(lines: list[str], lineno: int) -> str:
    if 1 <= lineno <= len(lines):
        return lines[lineno - 1].strip()[:_AST_REF_RAW_MAX]
    return ""


def _callee_token(func: ast.expr) -> str | None:
    """Dotted text of a callee, or None when the callee is not a name.

    `f()` -> "f"; `self._store.init_db()` -> "self._store.init_db";
    `factory()[0].run()` -> "run" (the non-name base is dropped rather
    than guessed at).
    """
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        parts: list[str] = []
        node: ast.expr = func
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
        parts.reverse()
        return ".".join(parts) if parts else None
    return None


def _import_targets(node: ast.Import | ast.ImportFrom) -> list[str]:
    """Module targets of an import, normalized exactly like code_edges does
    (relative imports keep their leading dots) so the two tables join.
    """
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names if alias.name]
    module = node.module or ""
    if node.level and module:
        return ["." * node.level + module]
    if node.level:
        return ["." * node.level]
    return [module] if module else []


def _record_ref(
    node: ast.AST,
    scope: str,
    lines: list[str],
    rows: list[tuple[str, int, str, str, str]],
) -> None:
    lineno = getattr(node, "lineno", 0)
    if isinstance(node, ast.Call):
        token = _callee_token(node.func)
        if not token:
            return
        raw = _raw_line(lines, lineno)
        rows.append((token, lineno, "call", raw, scope))
        tail = token.rpartition(".")[2]
        if tail and tail != token:
            rows.append((tail, lineno, "attr_call", raw, scope))
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        # Module-level imports are already the file-level code_edges rows;
        # only the function-local ones add information here.
        if not scope:
            return
        raw = _raw_line(lines, lineno)
        for target in _import_targets(node):
            rows.append((target, lineno, "lazy_import", raw, scope))


def _walk_refs(
    node: ast.AST,
    scope: str,
    lines: list[str],
    rows: list[tuple[str, int, str, str, str]],
) -> None:
    """Single scope-tracking descent. One pass, no ast.walk — the scope stack
    is the whole point and ast.walk discards it.
    """
    for child in ast.iter_child_nodes(node):
        if len(rows) >= _AST_REF_MAX_ROWS:
            return
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            inner = f"{scope}.{child.name}" if scope else child.name
            _walk_refs(child, inner, lines, rows)
            continue
        if isinstance(child, ast.Lambda):
            _walk_refs(child, f"{scope}.<lambda>" if scope else "<lambda>", lines, rows)
            continue
        _record_ref(child, scope, lines, rows)
        _walk_refs(child, scope, lines, rows)
