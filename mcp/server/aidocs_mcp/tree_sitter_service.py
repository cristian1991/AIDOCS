"""Tree-sitter integration for syntax validation and AST-based outline extraction.

Optional dependency — falls back gracefully when tree-sitter is not installed.
Install: pip install aidocs_mcp[ast]
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .tree_sitter_runtime import create_tree_sitter_parser, tree_sitter_support_reason

logger = logging.getLogger(__name__)

# Grammar loaders — lazy-loaded on first use
_GRAMMAR_LOADERS: dict[str, tuple[str, str]] = {
    ".py": ("tree_sitter_python", "language"),
    ".js": ("tree_sitter_javascript", "language"),
    ".mjs": ("tree_sitter_javascript", "language"),
    ".cjs": ("tree_sitter_javascript", "language"),
    ".jsx": ("tree_sitter_typescript", "language_tsx"),
    ".ts": ("tree_sitter_typescript", "language_typescript"),
    ".tsx": ("tree_sitter_typescript", "language_tsx"),
    ".cs": ("tree_sitter_c_sharp", "language"),
    ".go": ("tree_sitter_go", "language"),
    ".rs": ("tree_sitter_rust", "language"),
    ".java": ("tree_sitter_java", "language"),
    ".html": ("tree_sitter_html", "language"),
    ".css": ("tree_sitter_css", "language"),
    ".scss": ("tree_sitter_css", "language"),
    ".dart": ("tree_sitter_dart", "language"),
}

_SYNTAX_VALIDATION_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".py",
        ".js",
        ".mjs",
        ".cjs",
        ".jsx",
        ".ts",
        ".tsx",
    },
)

# Node types that represent outline symbols per language family
_OUTLINE_NODE_TYPES: dict[str, dict[str, str]] = {
    "python": {
        "function_definition": "function",
        "class_definition": "class",
        "decorated_definition": "_decorated",
    },
    "javascript": {
        "function_declaration": "function",
        "class_declaration": "class",
        "method_definition": "method",
        "arrow_function": "function",
        "export_statement": "_export",
        "lexical_declaration": "_declaration",
        "variable_declaration": "_declaration",
    },
    "typescript": {
        "function_declaration": "function",
        "class_declaration": "class",
        "method_definition": "method",
        "interface_declaration": "interface",
        "type_alias_declaration": "type_alias",
        "enum_declaration": "enum",
        "arrow_function": "function",
        "export_statement": "_export",
        "lexical_declaration": "_declaration",
        "variable_declaration": "_declaration",
    },
    "c_sharp": {
        "class_declaration": "class",
        "interface_declaration": "interface",
        "struct_declaration": "struct",
        "enum_declaration": "enum",
        "record_declaration": "record",
        "method_declaration": "method",
        "constructor_declaration": "initializer",
        "property_declaration": "property",
        "field_declaration": "field",
    },
    "go": {
        "function_declaration": "function",
        "method_declaration": "method",
        "type_declaration": "type_alias",
        "type_spec": "_type_spec",
    },
    "rust": {
        "function_item": "function",
        "struct_item": "struct",
        "enum_item": "enum",
        "trait_item": "interface",
        "impl_item": "_impl",
        "type_item": "type_alias",
    },
    "java": {
        "class_declaration": "class",
        "interface_declaration": "interface",
        "enum_declaration": "enum",
        "method_declaration": "method",
        "constructor_declaration": "initializer",
        "field_declaration": "field",
        "record_declaration": "record",
    },
    "dart": {
        "class_definition": "class",
        "enum_declaration": "enum",
        "function_signature": "function",
        "method_signature": "method",
        "constructor_signature": "initializer",
    },
}

# Map file extensions to language families for outline extraction
_EXTENSION_TO_FAMILY: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "typescript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".cs": "c_sharp",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".dart": "dart",
}


def _available() -> bool:
    """Check if tree-sitter is installed."""
    return tree_sitter_support_reason() is None


_parser_cache: dict[str, Any] = {}


def _get_parser(suffix: str) -> Any | None:
    """Get or create a tree-sitter parser for the given file extension."""
    if suffix in _parser_cache:
        return _parser_cache[suffix]
    loader = _GRAMMAR_LOADERS.get(suffix)
    if not loader:
        return None
    module_name, func_name = loader
    try:
        parser = create_tree_sitter_parser(module_name, func_name)
        _parser_cache[suffix] = parser
        return parser
    except (ImportError, AttributeError, Exception) as exc:
        logger.debug("tree-sitter parser not available for %s: %s", suffix, exc)
        _parser_cache[suffix] = None
        return None


def check_syntax(path: Path, text: str) -> str | None:
    """Validate syntax using tree-sitter. Returns error message or None.

    Contract (#57, 2026-04-27):
    - Returns ``None`` ONLY when the file's syntax is provably valid OR
      when the file extension is intentionally outside this validator's
      scope (_SYNTAX_VALIDATION_EXTENSIONS). The two cases are
      indistinguishable to callers — that's by design, because "not in
      our scope" means "we have nothing to say," same caller semantics
      as "valid."
    - Raises ``RuntimeError`` when the validator should have been able
      to decide but couldn't (tree-sitter unavailable, parser failed
      to load for a scope-listed extension). Pre-fix these branches
      silently returned None — a fail-open #57 violation. Tree-sitter
      is a hard dep now, so the unavailable branch is dead code; the
      raise stays as a defense against future regressions.

    Only languages in _SYNTAX_VALIDATION_EXTENSIONS are checked. Other grammars
    (C#, Go, Rust, Java, etc.) are available via _GRAMMAR_LOADERS for outline
    extraction but are not used for pre-edit validation, because grammar versions
    can lag the host language and reject modern-but-valid code on unrelated lines.
    """
    if not _available():
        raise RuntimeError(
            "tree-sitter unavailable but is a hard dep — install is broken. "
            "Refusing to fail-open per security-gates.md §0.5 #57.",
        )
    suffix = path.suffix.lower()
    if suffix not in _SYNTAX_VALIDATION_EXTENSIONS:
        return None
    parser = _get_parser(suffix)
    if parser is None:
        raise RuntimeError(
            f"tree-sitter parser failed to load for {suffix!r} despite being "
            f"in the syntax-validation scope. Refusing to fail-open per #57.",
        )
    tree = parser.parse(text.encode("utf-8"))
    if not tree.root_node.has_error:
        return None
    # Find the MOST SPECIFIC error node for a useful message (#476c, War BA):
    # on hard parse failures tree-sitter wraps huge spans (often the whole
    # file) in an outer ERROR node whose start is the file's FIRST token —
    # reporting that start blamed a valid /** header comment 140 lines away
    # from the offending edit. Descend the first-error chain to the deepest
    # nested ERROR so the reported line points at the actual break site.
    error_node = _find_most_specific_error(tree.root_node)
    if error_node:
        line = error_node.start_point[0] + 1
        col = error_node.start_point[1] + 1
        snippet = (
            text.splitlines()[error_node.start_point[0]][max(0, col - 20) : col + 20]
            if error_node.start_point[0] < len(text.splitlines())
            else ""
        )
        return f"Syntax error at line {line}, col {col}: unexpected `{snippet.strip()}`"
    return "Syntax error detected in edited file"


def _find_most_specific_error(node: Any) -> Any | None:
    """First error node in document order, descended to its deepest nested
    error along the first-child chain. A plain localized error (no nested
    ERROR children) is returned unchanged; a whole-file ERROR wrapper is
    unwrapped to the inner node that actually broke (#476c)."""
    current = _find_first_error(node)
    while current is not None:
        nested = None
        for child in current.children:
            found = _find_first_error(child)
            if found is not None and found is not current:
                nested = found
                break
        if nested is None:
            return current
        current = nested
    return current


def _find_first_error(node: Any) -> Any | None:
    """Recursively find the first ERROR or MISSING node in the tree."""
    if node.type == "ERROR" or node.is_missing:
        return node
    for child in node.children:
        found = _find_first_error(child)
        if found:
            return found
    return None


def extract_outline(path: Path, text: str) -> list[tuple[str, str, int, str | None, bool]]:
    """Extract code outline using tree-sitter AST.

    Returns list of (symbol, kind, line_number, container, is_partial).
    Falls back to empty list if tree-sitter unavailable.
    """
    if not _available():
        return []
    suffix = path.suffix.lower()
    parser = _get_parser(suffix)
    if parser is None:
        return []
    family = _EXTENSION_TO_FAMILY.get(suffix)
    if not family:
        return []
    node_types = _OUTLINE_NODE_TYPES.get(family, {})
    if not node_types:
        return []

    tree = parser.parse(text.encode("utf-8"))
    outlines: list[tuple[str, str, int, str | None, bool]] = []
    _walk_outline(tree.root_node, node_types, family, outlines, container=None)
    return outlines


def find_symbol_span(
    path: Path,
    text: str,
    symbol_name: str,
    start_line: int,
) -> tuple[int, int] | None:
    """Find (start_line, end_line) for a declaration whose name matches
    symbol_name and whose declaration line is start_line.

    Backs ai_replace(mode='symbol') for all 12 tree-sitter languages
    (python, js/ts/jsx/tsx, c#, go, rust, java, html, css, scss, dart).
    Returns None if tree-sitter unavailable for the language, or if no
    matching declaration found at start_line.

    Match heuristics:
    1. Exact: node's start row+1 == start_line AND name matches.
    2. Range-overlap fallback: node contains start_line AND name matches
       (handles decorators that shift the index's recorded line).
    """
    if not _available():
        return None
    suffix = path.suffix.lower()
    parser = _get_parser(suffix)
    if parser is None:
        return None
    family = _EXTENSION_TO_FAMILY.get(suffix)
    if not family:
        return None
    node_types = _OUTLINE_NODE_TYPES.get(family, {})
    if not node_types:
        return None
    tree = parser.parse(text.encode("utf-8"))
    # Pass 1: exact start_line match.
    span = _find_span_walk(
        tree.root_node,
        node_types,
        family,
        symbol_name,
        start_line,
        exact=True,
    )
    if span is not None:
        return span
    # Pass 2: range-overlap (decorator shift / multi-line declarations).
    return _find_span_walk(
        tree.root_node,
        node_types,
        family,
        symbol_name,
        start_line,
        exact=False,
    )


def _find_span_walk(
    node: Any,
    node_types: dict[str, str],
    family: str,
    symbol_name: str,
    start_line: int,
    exact: bool,
) -> tuple[int, int] | None:
    """Recursive walker for find_symbol_span."""
    if node.type in node_types:
        node_start = int(node.start_point[0]) + 1
        node_end = int(node.end_point[0]) + 1
        match = False
        if exact:
            if node_start == start_line:
                name = _get_name(node, family)
                if name == symbol_name:
                    match = True
        elif node_start <= start_line <= node_end:
            name = _get_name(node, family)
            if name == symbol_name:
                match = True
        if match:
            return (node_start, node_end)
    for child in node.children:
        result = _find_span_walk(
            child,
            node_types,
            family,
            symbol_name,
            start_line,
            exact,
        )
        if result is not None:
            return result
    return None


def _get_name(node: Any, family: str) -> str | None:
    """Extract the name from an AST node."""
    # Prefer plain 'identifier' over 'type_identifier' (type_identifier is often a return type in Dart/Go)
    for child in node.children:
        if child.type in ("identifier", "name", "property_identifier"):
            return child.text.decode("utf-8")
    # Fallback to type_identifier for class/struct/enum names
    for child in node.children:
        if child.type == "type_identifier":
            return child.text.decode("utf-8")
    # Python decorated_definition — get the inner function/class name
    if node.type == "decorated_definition":
        for child in node.children:
            if child.type in ("function_definition", "class_definition"):
                return _get_name(child, family)
    # JS/TS variable declarations — const Foo = ...
    if node.type in ("lexical_declaration", "variable_declaration"):
        for child in node.children:
            if child.type == "variable_declarator":
                return _get_name(child, family)
    # JS/TS export — export function/class/const
    if node.type == "export_statement":
        for child in node.children:
            if child.type not in ("export", "default", "comment"):
                name = _get_name(child, family)
                if name:
                    return name
    # C# field/property declarations: name lives inside a nested
    # variable_declaration → variable_declarator → identifier chain
    # (fields) or directly as identifier (properties). Descend into
    # the declarator so class-level fields surface in the outline.
    if node.type == "field_declaration":
        for child in node.children:
            if child.type == "variable_declaration":
                for gc in child.children:
                    if gc.type == "variable_declarator":
                        return _get_name(gc, family)
    return None


def _walk_outline(
    node: Any,
    node_types: dict[str, str],
    family: str,
    outlines: list[tuple[str, str, int, str | None, bool]],
    container: str | None,
) -> None:
    """Walk the AST and extract outline symbols."""
    kind = node_types.get(node.type)

    if kind:
        name = _get_name(node, family)
        line = node.start_point[0] + 1

        if kind == "_decorated":
            # Python: unwrap decorated_definition
            for child in node.children:
                if child.type in ("function_definition", "class_definition"):
                    inner_kind = "function" if child.type == "function_definition" else "class"
                    inner_name = _get_name(child, family)
                    if inner_name:
                        outlines.append((inner_name, inner_kind, line, container, False))
                        if inner_kind == "class":
                            _walk_children(child, node_types, family, outlines, inner_name)
            return

        if kind == "_export":
            # JS/TS: unwrap export statement
            for child in node.children:
                if child.type not in ("export", "default", "comment"):
                    _walk_outline(child, node_types, family, outlines, container)
            return

        if kind == "_declaration":
            # JS/TS: const Foo = () => {} or const Foo = class {}
            if name:
                # Check if the value is a function/class
                actual_kind = _infer_declaration_kind(node, family)
                outlines.append((name, actual_kind, line, container, False))
            return

        if kind == "_impl":
            # Rust: impl Foo { ... } — container for methods
            impl_name = _get_impl_name(node)
            if impl_name:
                _walk_children(node, node_types, family, outlines, impl_name)
            return

        if kind == "_type_spec":
            # Go: type Foo struct/interface
            if name:
                actual_kind = _infer_go_type_kind(node)
                outlines.append((name, actual_kind, line, container, False))
            return

        if name:
            # C#: a field with the `const` modifier is a named compile-
            # time constant — surface it as "constant" so ai_find can
            # filter for it distinctly from instance fields. Regular
            # (non-const) fields keep kind="field".
            if family == "c_sharp" and kind == "field" and _csharp_has_const_modifier(node):
                kind = "constant"
            outlines.append((name, kind, line, container, False))
            # Recurse into class/struct/interface bodies for methods
            if kind in ("class", "struct", "interface", "record", "enum"):
                _walk_children(node, node_types, family, outlines, name)
                return

    # For non-outline nodes, recurse into children
    _walk_children(node, node_types, family, outlines, container)


def _walk_children(
    node: Any,
    node_types: dict[str, str],
    family: str,
    outlines: list[tuple[str, str, int, str | None, bool]],
    container: str | None,
) -> None:
    """Recurse into child nodes."""
    for child in node.children:
        # Skip method/function bodies to avoid nested function noise
        if child.type == "block" and node.type in (
            "function_definition",
            "method_definition",
            "function_declaration",
            "method_declaration",
        ):
            continue
        _walk_outline(child, node_types, family, outlines, container)


def _csharp_has_const_modifier(node: Any) -> bool:
    """Return True if a C# field_declaration node carries the `const` modifier.

    Tree-sitter c-sharp emits modifiers as child `modifier` nodes whose
    text is the keyword (`public`, `static`, `const`, `readonly`, etc.).
    We scan the direct children; a class-level `const` field is always
    syntactically at the top of the field_declaration.
    """
    try:
        for child in node.children:
            if child.type == "modifier":
                try:
                    text = (
                        child.text.decode("utf-8")
                        if isinstance(child.text, bytes)
                        else str(child.text or "")
                    )
                except Exception:
                    text = ""
                if text.strip() == "const":
                    return True
    except Exception:
        return False
    return False


def _infer_declaration_kind(node: Any, family: str) -> str:
    """Infer kind from a JS/TS variable declaration (const Foo = ...).

    Callable-shape assignments (arrow, function expression, class, factory
    call) map to function/class/component. Plain value assignments map to
    'constant' so module-level configuration/enum-like tables show up as
    constants rather than being misclassified as functions.
    """
    for child in node.children:
        if child.type == "variable_declarator":
            for val in child.children:
                if val.type == "arrow_function":
                    return "function"
                if val.type in ("function", "function_expression"):
                    return "function"
                if val.type in ("class", "class_expression"):
                    return "class"
                if val.type == "call_expression":
                    # React.memo(), forwardRef(), etc.
                    return "component"
    return "constant"


def _get_impl_name(node: Any) -> str | None:
    """Get the type name from a Rust impl block."""
    for child in node.children:
        if child.type == "type_identifier":
            return child.text.decode("utf-8")
    return None


def _infer_go_type_kind(node: Any) -> str:
    """Infer Go type kind (struct, interface, etc.)."""
    for child in node.children:
        if child.type == "struct_type":
            return "struct"
        if child.type == "interface_type":
            return "interface"
    return "type_alias"


def _iter_nodes(node: Any) -> Any:
    """Iterate all nodes in the tree."""
    yield node
    for child in node.children:
        yield from _iter_nodes(child)
