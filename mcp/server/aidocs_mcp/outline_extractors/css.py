from __future__ import annotations

import re


def extract_css_outline(text: str) -> list[tuple[str, str, int, str | None, bool]]:
    outlines: list[tuple[str, str, int, str | None, bool]] = []
    media_pattern = re.compile(r"@media\s+([^\{]+)")
    class_pattern = re.compile(r"\.([a-zA-Z_][a-zA-Z0-9_-]*)")

    brace_depth = 0
    media_stack: list[tuple[int, str]] = []

    for line_number, line in enumerate(text.splitlines(), start=1):
        opens = line.count("{")
        closes = line.count("}")
        m = media_pattern.search(line)
        if m:
            media_stack.append((brace_depth + opens, m.group(1).strip()))
        stripped = line.strip()
        if stripped and not stripped.startswith(("/*", "*", "//")):
            if "{" in stripped or "," in stripped or stripped.startswith((".", "&")):
                current_media = media_stack[-1][1] if media_stack else None
                seen_classes: set[str] = set()
                for m in class_pattern.finditer(stripped):
                    cls_name = m.group(1)
                    if cls_name not in seen_classes:
                        seen_classes.add(cls_name)
                        outlines.append((cls_name, "css_class", line_number, current_media, False))
        brace_depth += opens
        brace_depth -= closes
        while media_stack and brace_depth < media_stack[-1][0]:
            media_stack.pop()
    return outlines
