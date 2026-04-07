from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


@dataclass(slots=True)
class FrontendAstSupport:
    available: bool
    reason: str | None = None


class FrontendAstExtractor:
    """Optional parser-backed frontend extraction with safe fallback."""

    def __init__(self) -> None:
        self._parser_cache: dict[str, Any] = {}
        self._support = self._detect_support()

    @property
    def support(self) -> FrontendAstSupport:
        return self._support

    def extract_outline(self, text: str, language: str) -> list[tuple[str, str, int, str | None, bool]] | None:
        if language not in {"javascript", "typescript", "jsx", "tsx"}:
            return None
        if not self._support.available:
            return None

        parser = self._get_parser(language)
        if parser is None:
            return None

        try:
            tree = parser.parse(text.encode("utf-8"))
        except Exception:
            return None

        results: list[tuple[str, str, int, str | None, bool]] = []
        self._walk(tree.root_node, text, results)
        return results or None

    def _detect_support(self) -> FrontendAstSupport:
        try:
            import tree_sitter  # noqa: F401
            import tree_sitter_javascript  # noqa: F401
            import tree_sitter_typescript  # noqa: F401
        except Exception as exc:
            return FrontendAstSupport(False, str(exc))
        return FrontendAstSupport(True, None)

    def _get_parser(self, language: str):
        if language in self._parser_cache:
            return self._parser_cache[language]
        try:
            from tree_sitter import Language, Parser
            import tree_sitter_javascript
            import tree_sitter_typescript
        except Exception:
            self._parser_cache[language] = None
            return None

        try:
            parser = Parser()
            if language in {"javascript", "jsx"}:
                lang = Language(tree_sitter_javascript.language())
            else:
                lang = Language(tree_sitter_typescript.language_typescript())
            parser.language = lang
        except Exception:
            self._parser_cache[language] = None
            return None

        self._parser_cache[language] = parser
        return parser

    def _walk(
        self,
        node,
        text: str,
        results: list[tuple[str, str, int, str | None, bool]],
        container: str | None = None,
    ) -> None:
        node_type = node.type
        next_container = container

        if node_type == "function_declaration":
            name = self._child_text(node, text, "name")
            if self._is_valid_symbol(name):
                results.append((name, self._frontend_kind(name), node.start_point[0] + 1, None, False))
        elif node_type == "class_declaration":
            name = self._child_text(node, text, "name")
            if self._is_valid_symbol(name):
                results.append((name, "class", node.start_point[0] + 1, None, False))
                next_container = name
        elif node_type == "interface_declaration":
            name = self._type_declaration_name(node, text)
            if self._is_valid_symbol(name):
                results.append((name, "interface", node.start_point[0] + 1, None, False))
                next_container = name
        elif node_type == "type_alias_declaration":
            name = self._type_declaration_name(node, text)
            if self._is_valid_symbol(name):
                results.append((name, "type_alias", node.start_point[0] + 1, None, False))
                next_container = name
        elif node_type == "enum_declaration":
            name = self._type_declaration_name(node, text)
            if self._is_valid_symbol(name):
                results.append((name, "enum", node.start_point[0] + 1, None, False))
                next_container = name
        elif node_type == "property_signature" and container is not None:
            name = self._property_signature_name(node, text)
            if self._is_valid_symbol(name):
                results.append((name, "property", node.start_point[0] + 1, container, False))
        elif node_type == "property_identifier" and container is not None and node.parent is not None and node.parent.type == "enum_body":
            name = text[node.start_byte:node.end_byte]
            if self._is_valid_symbol(name):
                results.append((name, "enum_member", node.start_point[0] + 1, container, False))
        elif node_type in {"lexical_declaration", "variable_declaration"}:
            for child in node.children:
                if child.type != "variable_declarator":
                    continue
                name = self._child_text(child, text, "name")
                value = child.child_by_field_name("value")
                if not self._is_valid_symbol(name) or value is None:
                    continue
                if value.type in {"arrow_function", "function", "function_expression"}:
                    results.append((name, self._frontend_kind(name), child.start_point[0] + 1, None, False))

        for child in node.children:
            self._walk(child, text, results, next_container)

    def _type_declaration_name(self, node, text: str) -> str | None:
        name = self._child_text(node, text, "name")
        if name is not None:
            return name
        for child in node.children:
            if child.type in {"type_identifier", "identifier"}:
                return text[child.start_byte:child.end_byte]
        return None

    def _property_signature_name(self, node, text: str) -> str | None:
        for child in node.children:
            if child.type in {"property_identifier", "identifier"}:
                return text[child.start_byte:child.end_byte]
        return None

    def _child_text(self, node, text: str, field_name: str) -> str | None:
        child = node.child_by_field_name(field_name)
        if child is None:
            return None
        return text[child.start_byte:child.end_byte]

    def _frontend_kind(self, symbol: str) -> str:
        if symbol.startswith("use") and len(symbol) > 3 and symbol[3:4].isupper():
            return "hook"
        if symbol[:1].isupper() and symbol.endswith("Provider"):
            return "context_provider"
        if symbol[:1].isupper():
            return "component"
        return "function"

    def _is_valid_symbol(self, symbol: str | None) -> bool:
        if not symbol:
            return False
        return re.match(r"^[A-Za-z_$][A-Za-z0-9_$]*$", symbol) is not None
