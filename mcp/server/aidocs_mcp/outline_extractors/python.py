from __future__ import annotations

import ast


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
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "function"
            if node.name.startswith("use") and len(node.name) > 3 and node.name[3:4].isupper():
                kind = "hook"
            outlines.append((node.name, kind, node.lineno, None, False))

    return outlines
