from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


class CodeIndexUtilityService:
    def __init__(self, store: Any) -> None:
        self.store = store

    def _extract_snippet(self, text: str, language: str, start_line: int) -> str:
        lines = text.splitlines()
        index = max(0, start_line - 1)
        if index >= len(lines):
            return ""

        if language == "python":
            return self._extract_indent_block(lines, index)
        if language in {"javascript", "typescript", "jsx", "tsx", "csharp"}:
            return self._extract_brace_block(lines, index)
        return "\n".join(lines[index : min(len(lines), index + 20)]).rstrip() + "\n"

    def _extract_indent_block(self, lines: list[str], index: int) -> str:
        start = index
        base_indent = len(lines[start]) - len(lines[start].lstrip())
        end = start + 1
        # For multi-line signatures (def/class), don't stop until we've seen the body
        seen_body = lines[start].rstrip().endswith(":")
        while end < len(lines):
            line = lines[end]
            if not line.strip():
                end += 1
                continue
            if not seen_body:
                # Still in the signature — look for the colon
                if line.rstrip().endswith(":"):
                    seen_body = True
                end += 1
                continue
            indent = len(line) - len(line.lstrip())
            if indent <= base_indent:
                break
            end += 1
        return "\n".join(lines[start:end]).rstrip() + "\n"

    def _extract_brace_block(self, lines: list[str], index: int) -> str:
        start = index
        brace_balance = 0
        seen_open = False
        end = start
        while end < len(lines):
            line = lines[end]
            brace_balance += line.count("{")
            brace_balance -= line.count("}")
            if line.count("{") > 0:
                seen_open = True
            if seen_open and brace_balance <= 0:
                end += 1
                break
            if not seen_open and end > start and line.strip().endswith(";"):
                end += 1
                break
            end += 1
        return "\n".join(lines[start:end]).rstrip() + "\n"
    def _get_file_stub(self, project_root: Path, path: str) -> dict[str, str | int] | None:
        with self.store.connect(project_root) as conn:
            row = conn.execute(
                "SELECT path, language, line_count, summary, role FROM code_files WHERE path = ? LIMIT 1",
                (path,),
            ).fetchone()
        if row is None:
            return None
        return {
            "path": row["path"],
            "language": row["language"],
            "line_count": int(row["line_count"]),
            "summary": row["summary"],
            "role": row["role"] or "unknown",
        }

    def _is_indexed_file(self, project_root: Path, path: str) -> bool:
        return self._get_file_stub(project_root, path) is not None

    def _file_exists(self, project_root: Path, path: str) -> bool:
        return (project_root / path).is_file()

    def _extract_relevant_files(self, sections: dict[str, list[str]]) -> list[str]:
        results: list[str] = []
        for line in sections.get("Relevant Files", []):
            stripped = line.strip()
            if not stripped.startswith("-"):
                continue
            candidate = stripped[1:].strip().strip('`')
            if not candidate or candidate.startswith('/.MEMORY/'):
                continue
            results.append(candidate)
        seen = set()
        ordered = []
        for item in results:
            if item not in seen:
                seen.add(item)
                ordered.append(item)
        return ordered

    @staticmethod
    def _outline_row_to_dict(row: sqlite3.Row) -> dict[str, object]:
        """Convert an outline row to a dict, omitting falsy optional fields to reduce noise."""
        result: dict[str, object] = {
            "symbol": row["symbol"],
            "kind": row["kind"],
            "line_number": int(row["line_number"]),
        }
        if row["container"]:
            result["container"] = row["container"]
        if row["is_partial"]:
            result["is_partial"] = True
        return result
