from __future__ import annotations

import re


def extract_resx_outline(text: str) -> list[tuple[str, str, int, str | None, bool]]:
    outlines: list[tuple[str, str, int, str | None, bool]] = []
    data_pattern = re.compile(r'<data\s+name="([^"]+)"')
    value_pattern = re.compile(r"<value>(.*?)</value>", re.DOTALL)

    in_data = False
    current_name: str | None = None
    current_line = 1

    for line_number, line in enumerate(text.splitlines(), start=1):
        m = data_pattern.search(line)
        if m:
            current_name = m.group(1)
            current_line = line_number
            in_data = True
        if in_data and current_name is not None:
            vm = value_pattern.search(line)
            if vm:
                value = vm.group(1).strip()
                container = value[:80] if value else None
                outlines.append((current_name, "translation", current_line, container, False))
                in_data = False
                current_name = None
            elif "</data>" in line:
                in_data = False
                current_name = None

    return outlines
