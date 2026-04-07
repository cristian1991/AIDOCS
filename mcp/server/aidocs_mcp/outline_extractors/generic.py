from __future__ import annotations

import re


_OUTLINE_FAMILIES: dict[str, list[tuple[str, str]]] = {
    "rust_basic": [
        (r"^\s*(?:pub(?:\(crate\))?\s+)?struct\s+([A-Za-z_][A-Za-z0-9_]*)", "struct"),
        (r"^\s*(?:pub(?:\(crate\))?\s+)?enum\s+([A-Za-z_][A-Za-z0-9_]*)", "enum"),
        (r"^\s*(?:pub(?:\(crate\))?\s+)?trait\s+([A-Za-z_][A-Za-z0-9_]*)", "trait"),
        (r"^\s*(?:pub(?:\(crate\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)", "function"),
        (r"^\s*(?:pub(?:\(crate\))?\s+)?type\s+([A-Za-z_][A-Za-z0-9_]*)", "type_alias"),
        (r"^\s*(?:pub(?:\(crate\))?\s+)?mod\s+([A-Za-z_][A-Za-z0-9_]*)", "module"),
        (r"^\s*impl(?:<[^>]*>)?\s+([A-Za-z_][A-Za-z0-9_:<>]*)", "impl"),
    ],
    "go_basic": [
        (r"^\s*type\s+([A-Za-z_][A-Za-z0-9_]*)\s+struct", "struct"),
        (r"^\s*type\s+([A-Za-z_][A-Za-z0-9_]*)\s+interface", "interface"),
        (r"^\s*func\s+(?:\([^)]*\)\s+)?([A-Za-z_][A-Za-z0-9_]*)", "function"),
        (r"^\s*type\s+([A-Za-z_][A-Za-z0-9_]*)\s+", "type_alias"),
    ],
    "java_basic": [
        (r"^\s*(?:public|private|protected|static|abstract|final|\s)*class\s+([A-Za-z_][A-Za-z0-9_]*)", "class"),
        (r"^\s*(?:public|private|protected|static|abstract|final|\s)*interface\s+([A-Za-z_][A-Za-z0-9_]*)", "interface"),
        (r"^\s*(?:public|private|protected|static|abstract|final|\s)*enum\s+([A-Za-z_][A-Za-z0-9_]*)", "enum"),
        (r"^\s*(?:public|private|protected|static|abstract|final|synchronized|\s)+[A-Za-z_<>,\[\]?]+\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", "method"),
    ],
    "kotlin_basic": [
        (r"^\s*(?:data\s+|sealed\s+|abstract\s+|open\s+)?class\s+([A-Za-z_][A-Za-z0-9_]*)", "class"),
        (r"^\s*(?:fun\s+)([A-Za-z_][A-Za-z0-9_]*)", "function"),
        (r"^\s*interface\s+([A-Za-z_][A-Za-z0-9_]*)", "interface"),
        (r"^\s*(?:enum\s+class|enum)\s+([A-Za-z_][A-Za-z0-9_]*)", "enum"),
        (r"^\s*object\s+([A-Za-z_][A-Za-z0-9_]*)", "object"),
    ],
    "ruby_basic": [
        (r"^\s*class\s+([A-Za-z_][A-Za-z0-9_:]*)", "class"),
        (r"^\s*module\s+([A-Za-z_][A-Za-z0-9_:]*)", "module"),
        (r"^\s*def\s+(?:self\.)?([A-Za-z_][A-Za-z0-9_!?]*)", "method"),
    ],
    "php_basic": [
        (r"^\s*(?:abstract\s+|final\s+)?class\s+([A-Za-z_][A-Za-z0-9_]*)", "class"),
        (r"^\s*interface\s+([A-Za-z_][A-Za-z0-9_]*)", "interface"),
        (r"^\s*trait\s+([A-Za-z_][A-Za-z0-9_]*)", "trait"),
        (r"^\s*(?:public|private|protected|static|\s)*function\s+([A-Za-z_][A-Za-z0-9_]*)", "function"),
    ],
    "elixir_basic": [
        (r"^\s*defmodule\s+([A-Za-z_][A-Za-z0-9_.]*)", "module"),
        (r"^\s*(?:def|defp)\s+([A-Za-z_][A-Za-z0-9_!?]*)", "function"),
    ],
    "frontend_script_basic": [
        (r"^\s*(?:export\s+)?(?:default\s+)?function\s+([A-Za-z_][A-Za-z0-9_]*)", "function"),
        (r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:async\s*)?\(", "function"),
        (r"^\s*(?:export\s+)?class\s+([A-Za-z_][A-Za-z0-9_]*)", "class"),
    ],
    "sql_ddl_basic": [
        (r"(?i)^\s*CREATE\s+(?:OR\s+REPLACE\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:\")?([A-Za-z_][A-Za-z0-9_.]*)(?:\")?", "table"),
        (r"(?i)^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:\")?([A-Za-z_][A-Za-z0-9_.]*)(?:\")?", "index"),
        (r"(?i)^\s*CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:\")?([A-Za-z_][A-Za-z0-9_.]*)(?:\")?", "view"),
        (r"(?i)^\s*CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+(?:\")?([A-Za-z_][A-Za-z0-9_.]*)(?:\")?", "function"),
    ],
}


def generic_outline_patterns(language: str) -> list[tuple[str, str]]:
    aliases = {
        "rust": "rust_basic",
        "go": "go_basic",
        "java": "java_basic",
        "kotlin": "kotlin_basic",
        "ruby": "ruby_basic",
        "php": "php_basic",
        "elixir": "elixir_basic",
        "vue": "frontend_script_basic",
        "svelte": "frontend_script_basic",
        "sql": "sql_ddl_basic",
    }
    family = aliases.get(language)
    return list(_OUTLINE_FAMILIES.get(family or "", []))


def outline_family_patterns(name: str) -> list[tuple[str, str]]:
    return list(_OUTLINE_FAMILIES.get(name, []))


def outline_family_names() -> list[str]:
    return sorted(_OUTLINE_FAMILIES.keys())


def extract_generic_outline(text: str, patterns: list[tuple[str, str]]) -> list[tuple[str, str, int, str | None, bool]]:
    outlines: list[tuple[str, str, int, str | None, bool]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for pattern, kind in patterns:
            match = re.match(pattern, line)
            if match:
                outlines.append((match.group(1), kind, line_number, None, False))
                break
    return outlines


def extract_line_patterns(text: str, patterns: list[tuple[str, str]]) -> list[tuple[str, str, int, str | None, bool]]:
    outlines: list[tuple[str, str, int, str | None, bool]] = []
    seen: set[tuple[str, str, int]] = set()
    for line_number, line in enumerate(text.splitlines(), start=1):
        for pattern, kind in patterns:
            try:
                matches = list(re.finditer(pattern, line))
            except re.error:
                continue
            for match in matches:
                if match.groups():
                    if len(match.groups()) >= 2 and kind == "media_feature":
                        symbol = f"{(match.group(1) or '').strip()}:{(match.group(2) or '').strip()}".strip(":")
                    else:
                        symbol = (match.group(1) or "").strip()
                    if not symbol:
                        symbol = match.group(0).strip()
                else:
                    symbol = match.group(0)
                if not symbol:
                    continue
                if kind == "page_route" and symbol == "@page":
                    symbol = "@page"
                key = (symbol, kind, line_number)
                if key in seen:
                    continue
                seen.add(key)
                outlines.append((symbol, kind, line_number, None, False))
    return outlines
